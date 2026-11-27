from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import imaplib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from gmail_fetcher import (
    GmailRegistrationReader,
    GoogleMailboxLogin,
    HEADER_SECTION,
    ImapAuthError,
    ImapConfig,
    ImapConnectionError,
    ImapMailboxError,
    InboxBaseline,
    NINJA_REGISTRATION_SUBJECT,
    PendingMailRequest,
    STATUS_CHECK_MAIL,
    STATUS_CONFIRMED,
    decode_email_message,
    evaluate_email_for_requests,
    load_gmail_credentials,
    normalize_app_password,
)


def build_ninja_sample_mime(
    *,
    to_addr: str,
    from_addr: str = "忍者ツールズ <info@ninja.co.jp>",
    subject: str = NINJA_REGISTRATION_SUBJECT,
    confirm_url_plain: str = "https://www.ninja.co.jp/register/input?key=dummy_token_001&mode=pre",
    confirm_url_html: str | None = None,
    extra_links: tuple[str, ...] = (
        "https://www.ninja.co.jp/help/faq/918/",
        "https://corp.ninja.co.jp/privacy/",
        "https://www.ninja.co.jp/register/",
        "http://www.ninja.co.jp/register/input?key=insecure_http",
    ),
    charset: str = "utf-8",
    extra_headers: dict[str, str] | None = None,
    body_note: str = "",
) -> bytes:
    html_url = confirm_url_html if confirm_url_html is not None else confirm_url_plain
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    if extra_headers:
        for k, v in extra_headers.items():
            msg[k] = v

    plain_lines = [
        "忍者ツールズユーザー登録のご案内",
        "",
        "仮登録を受け付けました。",
        "以下のURLより本登録を完了してください。",
        confirm_url_plain,
        "",
        body_note,
        "ヘルプ:",
        *extra_links,
    ]
    plain_part = MIMEText("\n".join(plain_lines), "plain", charset)

    html_links = "".join(f'<li><a href="{u}">Link</a></li>' for u in extra_links)
    html_escaped_url = html_url.replace("&", "&amp;")
    html_body = (
        "<html><body>"
        "<p>仮登録を受け付けました。</p>"
        f'<p><a href="{html_escaped_url}">{html_escaped_url}</a></p>'
        f"<p>{body_note}</p>"
        f"<ul>{html_links}</ul>"
        "</body></html>"
    )
    html_part = MIMEText(html_body, "html", charset)

    msg.attach(plain_part)
    msg.attach(html_part)
    return msg.as_bytes()


class FakeImapServerState:
    def __init__(self, uidvalidity: int = 1001, uidnext: int = 10) -> None:
        self.uidvalidity = uidvalidity
        self.uidnext = uidnext
        self.notifications = threading.Event()
        self.supports_idle = True
        self.idle_calls = 0
        self.done_calls = 0
        self.logouts = 0
        self.messages: dict[int, tuple[datetime, bytes, set[str]]] = {}
        self.valid_user = "rootbox@gmail.com"
        self.valid_password = "app-secret-password"
        self.select_fails = False
        self.disconnect_next_polls = 0
        self.disconnect_next_searches = 0
        self.readonly_calls: list[bool] = []
        self.fetch_requests: list[tuple[int, str]] = []
        self.search_queries: list[str] = []

    def add_message(
        self,
        raw_bytes: bytes,
        internal_dt: datetime,
        seen: bool = False,
        uid: int | None = None,
    ) -> int:
        assigned = uid if uid is not None else self.uidnext
        if assigned >= self.uidnext:
            self.uidnext = assigned + 1
        flags = {"\\Seen"} if seen else set()
        self.messages[assigned] = (internal_dt, raw_bytes, flags)
        self.notifications.set()
        return assigned


class FakeImapConnection:
    def __init__(self, state: FakeImapServerState) -> None:
        self.state = state
        self.logged_in = False
        self.selected_readonly = False
        self._last_responses: dict[str, list[bytes]] = {}
        self.sock = None

    def login(self, user: str, password: str):
        if user != self.state.valid_user or password != self.state.valid_password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.logged_in = True
        return "OK", [b"Logged in"]

    def oauth2_login(self, user, token):
        if user != self.state.valid_user or token != "mock-access-token":
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.logged_in = True

    def select_folder(self, mailbox, readonly=False):
        if not self.logged_in or self.state.select_fails or mailbox != "INBOX":
            raise imaplib.IMAP4.error("Cannot select mailbox")
        self.state.readonly_calls.append(readonly)
        return self.folder_status(mailbox, ())

    def folder_status(self, mailbox, names):
        return {b"UIDVALIDITY": self.state.uidvalidity, b"UIDNEXT": self.state.uidnext}

    def search(self, criteria):
        if self.state.disconnect_next_searches:
            self.state.disconnect_next_searches -= 1
            self.state.uidvalidity = getattr(self.state, "uidvalidity_on_disconnect", self.state.uidvalidity)
            raise imaplib.IMAP4.abort("Simulated search disconnect")
        self.state.notifications.clear()
        query = criteria[-1]
        self.state.search_queries.append(query)
        start = int(query.split(":")[0])
        uids = sorted(self.state.messages)
        return [uid for uid in uids if uid >= start] or uids[-1:]

    def has_capability(self, cap):
        return cap == "IDLE" and self.state.supports_idle

    def idle(self):
        self.state.idle_calls += 1

    def idle_check(self, timeout):
        return [(len(self.state.messages), b"EXISTS")] if self.state.notifications.wait(timeout) else []

    def idle_done(self):
        self.state.done_calls += 1

    def fetch(self, uids, fields):
        uid, = uids
        dt, raw, flags = self.state.messages[uid]
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        data, payloads = {}, {}

        def structure(part, number=""):
            disposition = (b"ATTACHMENT", None) if part.get_content_disposition() == "attachment" else None
            if part.is_multipart():
                children = [structure(child, f"{number}.{i}" if number else str(i))
                            for i, child in enumerate(part.iter_parts(), 1)]
                return children, part.get_content_subtype().upper().encode(), None, disposition
            payloads[number or "TEXT"] = part.get_payload().encode("ascii", "surrogateescape")
            return (part.get_content_maintype().upper().encode(), part.get_content_subtype().upper().encode(),
                    (b"CHARSET", (part.get_content_charset() or "ascii").encode()), None, None,
                    part.get("Content-Transfer-Encoding", "7BIT").encode(), 0, 1, None, disposition)

        bodystructure = structure(msg)
        for field in fields:
            self.state.fetch_requests.append((uid, field))
            if field == "INTERNALDATE":
                data[b"INTERNALDATE"] = dt
            elif field == "BODYSTRUCTURE":
                data[b"BODYSTRUCTURE"] = bodystructure
            elif field == f"BODY.PEEK[{HEADER_SECTION}]":
                header = EmailMessage()
                for name in HEADER_SECTION.partition("(")[2].rstrip(")").split():
                    for value in msg.get_all(name, []):
                        header[name] = value
                data[f"BODY[{HEADER_SECTION}]".encode()] = header.as_bytes()
            else:
                if not field.startswith("BODY.PEEK["):
                    raise AssertionError(f"Unexpected body fetch: {field}")
                section = field[len("BODY.PEEK["):-1]
                data[f"BODY[{section}]".encode()] = payloads[section]
        return {uid: data}

    def logout(self):
        self.state.logouts += 1

    def shutdown(self):
        self.logged_in = False


class GmailFetcherTests(unittest.TestCase):
    def test_wrapped_hash_url_and_host_aliases_share_one_confirmation(self):
        state = FakeImapServerState()
        reader = GmailRegistrationReader(ImapConfig(state.valid_user, state.valid_password),
                                         imap_factory=lambda *_: FakeImapConnection(state))
        self.addCleanup(reader.close)
        first, second = "r.ootbox@gmail.com", "ro.otbox@gmail.com"
        pending = [PendingMailRequest(address, address, reader.mark_baseline(address))
                   for address in (first, second)]
        prefix = "https://www.ninja.co.jp/register/input/hash/"
        link = prefix + "ab" * 20
        wrapped = prefix + "ab" * 10 + "\r\n    " + "ab" * 10
        alias = link.replace("www.", "", 1)
        for address, body in [(first, wrapped), (second, alias)]:
            msg = EmailMessage()
            msg["Subject"] = NINJA_REGISTRATION_SUBJECT
            msg["From"] = "oshirase@shinobi.jp"
            msg["To"] = address
            msg.set_content(body)
            state.add_message(msg.as_bytes(), datetime.now(timezone.utc))
            if address == first:
                self.assertEqual(decode_email_message(msg.as_bytes()).confirmation_urls, (link,))
        outcomes = reader.poll_once(pending)
        self.assertEqual({outcome.request_id for outcome in outcomes}, {first, second})
        self.assertTrue(all(outcome.status == STATUS_CHECK_MAIL for outcome in outcomes))
        self.assertTrue(all(outcome.confirmation_url is None for outcome in outcomes))

    def test_idle_error_rechecks_mail_before_poll_delay(self):
        state = FakeImapServerState()
        conn = FakeImapConnection(state)
        reader = GmailRegistrationReader(ImapConfig(state.valid_user, state.valid_password),
                                         imap_factory=Mock(side_effect=[conn, FakeImapConnection(state)]))
        self.addCleanup(reader.close)
        recipient = "ro.otbox@gmail.com"
        pending = [PendingMailRequest("req", recipient, reader.mark_baseline(recipient))]

        def mail_before_idle_ack():
            state.add_message(build_ninja_sample_mime(to_addr=recipient), datetime.now(timezone.utc))
            raise imaplib.IMAP4.error("Unexpected EXISTS before continuation")

        with patch.object(conn, "idle", side_effect=mail_before_idle_ack):
            outcomes = reader.wait_for_requests(lambda: pending, 2, lambda _: pending.clear())
        self.assertEqual([o.status for o in outcomes], [STATUS_CONFIRMED])
        self.assertEqual(len(state.readonly_calls), 2)

    def test_only_exact_invitation_body_is_read_and_text_attachments_stay_on_server(self):
        state = FakeImapServerState()
        reader = GmailRegistrationReader(ImapConfig(state.valid_user, state.valid_password),
                                         imap_factory=lambda *_: FakeImapConnection(state))
        self.addCleanup(reader.close)
        recipient = "root.box@gmail.com"
        baseline = reader.mark_baseline(recipient)
        link = "https://www.ninja.co.jp/register/input/hash/" + "ad" * 20
        for subject in ["忍者ツールズユーザー登録完了メール", "Re: " + NINJA_REGISTRATION_SUBJECT,
                        NINJA_REGISTRATION_SUBJECT]:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = "忍者ツールズ <oshirase@shinobi.jp>"
            msg["To"] = recipient
            msg.set_content("本登録のURL\n" + link)
            msg.add_attachment("https://www.ninja.co.jp/register/input/hash/abcd",
                               subtype="plain", filename="attachment.txt")
            uid = state.add_message(msg.as_bytes(), datetime.now(timezone.utc))
        outcome, = reader.poll_once([PendingMailRequest("req", recipient, baseline)])
        self.assertEqual((outcome.status, outcome.uid, outcome.confirmation_url),
                         (STATUS_CONFIRMED, uid, link))
        bodies = [(i, f) for i, f in state.fetch_requests if f.startswith("BODY.PEEK[") and "HEADER" not in f]
        self.assertEqual(bodies, [(uid, "BODY.PEEK[1]")])

    def test_singlepart_text_body_only_recipient_and_missing_token(self):
        state = FakeImapServerState()
        reader = GmailRegistrationReader(ImapConfig(state.valid_user, state.valid_password),
                                         imap_factory=lambda *_: FakeImapConnection(state))
        self.addCleanup(reader.close)
        recipient = "root.box@gmail.com"
        baseline = reader.mark_baseline(recipient)
        for to in [recipient, "elsewhere@example.org"]:
            with self.subTest(to=to):
                msg = EmailMessage()
                msg["Subject"] = NINJA_REGISTRATION_SUBJECT
                msg["From"] = "oshirase@shinobi.jp"
                msg["To"] = to
                msg.set_content(recipient + "\nhttps://www.ninja.co.jp/register/input")
                uid = state.add_message(msg.as_bytes(), datetime.now(timezone.utc))
                outcome, = reader.poll_once([PendingMailRequest("req", recipient, baseline)])
                self.assertEqual(outcome.status, STATUS_CHECK_MAIL)
                self.assertIsNone(outcome.confirmation_url)
                self.assertIn((uid, "BODY.PEEK[TEXT]"), state.fetch_requests)
                baseline = reader.mark_baseline(recipient)

    def test_idle_wakes_for_mail_and_exits_before_fetch(self):
        state = FakeImapServerState()
        conn = FakeImapConnection(state)
        reader = GmailRegistrationReader(ImapConfig(state.valid_user, state.valid_password),
                                         imap_factory=lambda *_: conn)
        self.addCleanup(reader.close)
        recipient = "ro.otbox@gmail.com"
        pending = [PendingMailRequest("req", recipient, reader.mark_baseline(recipient))]
        send = lambda: state.add_message(build_ninja_sample_mime(to_addr=recipient), datetime.now(timezone.utc))
        with patch.object(conn, "idle", side_effect=send), patch.object(conn, "idle_done", wraps=conn.idle_done) as done:
            outcomes = reader.wait_for_requests(lambda: pending, 3, lambda _: pending.clear())
        self.assertEqual([o.status for o in outcomes], [STATUS_CONFIRMED])
        done.assert_called_once()
        self.assertEqual(len(state.readonly_calls), 1)
        self.assertEqual(len(state.search_queries), 2)

    def test_no_idle_waits_at_least_fifteen_seconds_and_respects_short_deadline(self):
        state = FakeImapServerState()
        state.supports_idle = False
        reader = GmailRegistrationReader(ImapConfig(state.valid_user, state.valid_password, poll_interval=0.1),
                                         imap_factory=lambda *_: FakeImapConnection(state))
        reader.ensure_connected()
        self.addCleanup(reader.close)
        with patch.object(reader._stop_event, "wait", return_value=False) as wait:
            self.assertTrue(reader._wait_for_change(120, lambda: 0))
            wait.assert_called_once_with(15)
            self.assertFalse(reader._wait_for_change(3, lambda: 0))
            wait.assert_called_with(3)

    def test_idle_renews_and_polls_periodically_when_exists_push_is_missed(self):
        state = FakeImapServerState()
        conn = FakeImapConnection(state)
        reader = GmailRegistrationReader(
            ImapConfig(state.valid_user, state.valid_password),
            imap_factory=lambda *_: conn,
        )
        self.addCleanup(reader.close)
        recipient = "n.h.at.sos123@gmail.com"
        pending = [PendingMailRequest("req", recipient, reader.mark_baseline(recipient))]
        now = [0.0]

        def silent_idle_check(timeout):
            now[0] += timeout
            if len(state.messages) == 0 and now[0] >= 5.0:
                state.add_message(build_ninja_sample_mime(to_addr=recipient), datetime.now(timezone.utc))
            return []  # Simulate missed IDLE EXISTS push.

        with patch.object(conn, "idle_check", side_effect=silent_idle_check):
            outcomes = reader.wait_for_requests(
                lambda: pending,
                timeout_seconds=60.0,
                on_outcome=lambda _: pending.clear(),
                clock=lambda: now[0],
            )
        self.assertEqual([o.status for o in outcomes], [STATUS_CONFIRMED])
        self.assertLessEqual(now[0], 20.0)
        self.assertGreaterEqual(state.done_calls, 1)
        self.assertEqual(len(state.search_queries), 2)

    def test_idle_detects_exists_absorbed_or_buffered_during_idle_command(self):
        state = FakeImapServerState()
        conn = FakeImapConnection(state)

        class FakeRawImap:
            def __init__(self):
                self.untagged_responses = {}
                self.file = None
                self.sock = None

        conn._imap = FakeRawImap()
        reader = GmailRegistrationReader(
            ImapConfig(state.valid_user, state.valid_password),
            imap_factory=lambda *_: conn,
        )
        reader.ensure_connected()
        self.addCleanup(reader.close)

        def push_into_untagged():
            conn._imap.untagged_responses["EXISTS"] = [b"1"]

        with patch.object(conn, "idle", side_effect=push_into_untagged), patch.object(
            conn, "idle_check", side_effect=AssertionError("should not block in idle_check")
        ):
            self.assertTrue(reader._wait_for_change(120.0, lambda: 0.0))

        class FakeBufferedSock:
            @staticmethod
            def pending():
                return 16

        conn._imap.untagged_responses.clear()
        conn._imap.sock = FakeBufferedSock()
        with patch.object(
            conn, "idle_check", side_effect=AssertionError("should not block when SSL buffer has data")
        ):
            self.assertTrue(reader._wait_for_change(120.0, lambda: 0.0))

    def test_mark_baseline_accepts_calibrated_server_time_and_default_skew(self):
        state = FakeImapServerState(uidvalidity=90, uidnext=50)
        reader = GmailRegistrationReader(
            ImapConfig(state.valid_user, state.valid_password),
            imap_factory=lambda *_: FakeImapConnection(state),
        )
        self.addCleanup(reader.close)
        recipient = "h.u.y.n.h@gmail.com"
        server_now = datetime(2026, 11, 27, 11, 43, 35, tzinfo=timezone.utc)
        baseline = reader.mark_baseline(recipient, captured_at_utc=server_now)
        self.assertEqual(baseline.captured_at_utc, server_now)
        state.add_message(
            build_ninja_sample_mime(to_addr=recipient),
            internal_dt=server_now + timedelta(seconds=1),
        )
        outcomes = reader.poll_once([PendingMailRequest("req", recipient, baseline)])
        self.assertEqual([o.status for o in outcomes], [STATUS_CONFIRMED])

        # Local clock 5 minutes fast still passes default skew tolerance even without calibration
        uncalibrated = reader.mark_baseline(recipient)
        state.add_message(
            build_ninja_sample_mime(
                to_addr="h.u.yn.h@gmail.com",
                confirm_url_plain="https://www.ninja.co.jp/register/input?key=skew_ok",
            ),
            internal_dt=uncalibrated.captured_at_utc - timedelta(minutes=5),
        )
        outcomes2 = reader.poll_once([PendingMailRequest("req-2", "h.u.yn.h@gmail.com", uncalibrated)])
        self.assertEqual([o.status for o in outcomes2], [STATUS_CONFIRMED])

    def test_oauth_desktop_consent_cached_in_memory_and_used_for_imap(self):
        state = FakeImapServerState()
        creds = Mock(token="mock-access-token", valid=True)
        with TemporaryDirectory() as tmp, patch("gmail_fetcher.InstalledAppFlow.run_local_server", return_value=creds) as consent:
            client_file = Path(tmp) / "desktop.json"
            client_file.write_text('{"installed":{"client_id":"test-id","client_secret":"test-secret"}}')
            auth = GoogleMailboxLogin(state.valid_user, str(client_file))
            cfg = ImapConfig(state.valid_user, oauth_token=auth.token)
            reader = GmailRegistrationReader(cfg, imap_factory=lambda *_: FakeImapConnection(state))
            reader.verify_connection()
            creds.valid = False
            reader.verify_connection()
            consent.assert_called_once()
            self.assertEqual(consent.call_args.kwargs["host"], "127.0.0.1")
            creds.refresh.assert_called_once()
            self.assertEqual(list(Path(tmp).iterdir()), [client_file])
            self.assertEqual(state.logouts, 2)

    def test_oauth_timeout_raises_friendly_auth_error(self):
        class WSGITimeoutError(Exception):
            pass

        with TemporaryDirectory() as tmp, patch(
            "gmail_fetcher.InstalledAppFlow.run_local_server",
            side_effect=WSGITimeoutError("timed out"),
        ):
            client_file = Path(tmp) / "desktop.json"
            client_file.write_text('{"installed":{"client_id":"test-id","client_secret":"test-secret"}}')
            auth = GoogleMailboxLogin("rootbox@gmail.com", str(client_file))
            with self.assertRaises(ImapAuthError) as ctx:
                auth.token()
            self.assertIn("Hết thời gian chờ", str(ctx.exception))

    def test_fetch_candidate_handles_quoted_header_key_and_missing_charset_transfer(self):
        link = "https://www.ninja.co.jp/register/input/hash/" + "ab" * 20
        raw_header = (
            f"Subject: {NINJA_REGISTRATION_SUBJECT}\r\n"
            "From: oshirase@shinobi.jp\r\n"
            "To: r.oot@gmail.com\r\n"
        ).encode("utf-8")
        raw_body = f"本登録URL\n{link}".encode("utf-8")
        naive_dt = datetime(2026, 11, 27, 0, 5, 0)

        class VariantConn:
            def fetch(self, uids, fields):
                uid = uids[0]
                if "BODYSTRUCTURE" in fields:
                    return {
                        uid: {
                            b"INTERNALDATE": naive_dt,
                            b"BODYSTRUCTURE": (b"TEXT", b"PLAIN", (), None, None, None, 10, 1),
                            b'BODY[HEADER.FIELDS ("SUBJECT" "FROM" "TO")]': raw_header,
                        }
                    }
                return {uid: {b"BODY[TEXT]": raw_body}}

        reader = GmailRegistrationReader(ImapConfig("rootbox@gmail.com", "pw"))
        parsed = reader._fetch_candidate(VariantConn(), 99)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.confirmation_urls, (link,))
        self.assertEqual(parsed.internal_date_utc, naive_dt.replace(tzinfo=timezone.utc))

    def test_load_credentials_falls_back_to_env_and_respects_ui(self):
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "GMAIL_INBOX_USER=envuser@gmail.com\nGMAIL_APP_PASSWORD=env-secret-pw\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                user, pw = load_gmail_credentials("", "", env_file=env_path)
                self.assertEqual((user, pw), ("envuser@gmail.com", "env-secret-pw"))

                user2, pw2 = load_gmail_credentials("ui@gmail.com", "ui-pw", env_file=env_path)
                self.assertEqual((user2, pw2), ("ui@gmail.com", "ui-pw"))

                user3, pw3 = load_gmail_credentials("ui@gmail.com", "abcd efgh ijkl mnop", env_file=env_path)
                self.assertEqual((user3, pw3), ("ui@gmail.com", "abcdefghijklmnop"))
                self.assertEqual(normalize_app_password("wxyz abcd efgh ijkl"), "wxyzabcdefghijkl")
                self.assertEqual(normalize_app_password("custom pass"), "custom pass")

    def test_repr_hides_app_password_and_confirmation_url(self):
        cfg = ImapConfig(email="root@gmail.com", app_password="super-secret-password")
        self.assertNotIn("super-secret-password", repr(cfg))

        raw = build_ninja_sample_mime(
            to_addr="r.oot@gmail.com",
            confirm_url_plain="https://www.ninja.co.jp/register/input?key=secret_token_999",
        )
        parsed = decode_email_message(raw, uid=1)
        self.assertNotIn("secret_token_999", repr(parsed))

    def test_decode_iso2022jp_and_deduplicate_html_plain_url_with_help_links(self):
        raw = build_ninja_sample_mime(
            to_addr="r.oot@gmail.com",
            confirm_url_plain="https://www.ninja.co.jp/register/input?id=42&key=token_abc",
            charset="iso-2022-jp",
        )
        parsed = decode_email_message(raw, uid=15, internal_date_utc=datetime(2026, 11, 27, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(parsed.subject, NINJA_REGISTRATION_SUBJECT)
        self.assertEqual(parsed.from_addresses, ("info@ninja.co.jp",))
        self.assertEqual(parsed.to_addresses, ("r.oot@gmail.com",))
        self.assertEqual(
            parsed.confirmation_urls,
            ("https://www.ninja.co.jp/register/input?id=42&key=token_abc",),
        )

    def test_two_distinct_confirmation_links_require_manual_check(self):
        raw = build_ninja_sample_mime(
            to_addr="r.oot@gmail.com",
            confirm_url_plain="https://www.ninja.co.jp/register/input?key=token_one",
            confirm_url_html="https://www.ninja.co.jp/register/input?key=token_two",
        )
        now = datetime(2026, 11, 27, 0, 5, tzinfo=timezone.utc)
        baseline = InboxBaseline(1001, 10, now - timedelta(seconds=5), "r.oot@gmail.com")
        req = PendingMailRequest("req-1", "r.oot@gmail.com", baseline)
        parsed = decode_email_message(raw, uid=10, internal_date_utc=now)
        outcomes = evaluate_email_for_requests(parsed, [req])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, STATUS_CHECK_MAIL)
        self.assertIsNone(outcomes[0].confirmation_url)

    def test_verify_connection_errors_never_leak_password(self):
        state = FakeImapServerState()
        bad_cfg = ImapConfig(email="rootbox@gmail.com", app_password="wrong-secret-password")
        reader = GmailRegistrationReader(bad_cfg, imap_factory=lambda h, p, t: FakeImapConnection(state))
        with self.assertRaises(ImapAuthError) as ctx:
            reader.verify_connection()
        self.assertNotIn("wrong-secret-password", str(ctx.exception))

        state.select_fails = True
        good_cfg = ImapConfig(email="rootbox@gmail.com", app_password="app-secret-password")
        reader2 = GmailRegistrationReader(good_cfg, imap_factory=lambda h, p, t: FakeImapConnection(state))
        with self.assertRaises(ImapMailboxError):
            reader2.verify_connection()

    def test_old_mail_ignored_and_already_read_new_mail_selected_with_body_peek(self):
        state = FakeImapServerState(uidvalidity=777, uidnext=10)
        t0 = datetime(2026, 11, 27, 1, 0, 0, tzinfo=timezone.utc)
        # Thư cũ cùng tiêu đề trước mốc UIDNEXT
        state.add_message(
            build_ninja_sample_mime(
                to_addr="u.ser@gmail.com",
                confirm_url_plain="https://www.ninja.co.jp/register/input?key=old_token",
            ),
            internal_dt=t0 - timedelta(minutes=5),
            uid=10,
        )
        # Thư cũ khác ngay sát mốc nhưng INTERNALDATE quá cũ
        cfg = ImapConfig(email="rootbox@gmail.com", app_password="app-secret-password")
        reader = GmailRegistrationReader(cfg, imap_factory=lambda h, p, t: FakeImapConnection(state))

        baseline = reader.mark_baseline("u.ser@gmail.com")
        self.assertEqual(baseline.uidnext, 11)
        self.assertEqual(baseline.uidvalidity, 777)

        # Thư mới đến ngay sau khi gửi và đã bị người dùng mở (seen=True)
        new_uid = state.add_message(
            build_ninja_sample_mime(
                to_addr="u.ser@gmail.com",
                confirm_url_plain="https://www.ninja.co.jp/register/input?key=fresh_token",
            ),
            internal_dt=baseline.captured_at_utc + timedelta(seconds=1),
            seen=True,
        )
        req = PendingMailRequest("req-1", "u.ser@gmail.com", baseline)
        outcomes = reader.poll_once([req])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, STATUS_CONFIRMED)
        self.assertEqual(
            outcomes[0].confirmation_url,
            "https://www.ninja.co.jp/register/input?key=fresh_token",
        )
        self.assertEqual(outcomes[0].uid, new_uid)
        self.assertTrue(all(ro for ro in state.readonly_calls))
        self.assertIn((new_uid, "BODY.PEEK[1]"), state.fetch_requests)
        self.assertFalse(any(spec in ("BODY[]", "BODY.PEEK[]", "RFC822") for _, spec in state.fetch_requests))
        # Kiểm tra không tải nội dung thư cũ UID 10
        self.assertNotIn(10, [uid for uid, _ in state.fetch_requests])

    def test_two_dot_variants_out_of_order_get_exact_link_once(self):
        state = FakeImapServerState(uidvalidity=55, uidnext=20)
        cfg = ImapConfig(email="rootbox@gmail.com", app_password="app-secret-password")
        reader = GmailRegistrationReader(cfg, imap_factory=lambda h, p, t: FakeImapConnection(state))

        b1 = reader.mark_baseline("a.bc@gmail.com")
        b2 = reader.mark_baseline("ab.c@gmail.com")
        req1 = PendingMailRequest("req-1", "a.bc@gmail.com", b1)
        req2 = PendingMailRequest("req-2", "ab.c@gmail.com", b2)

        # Thư của req2 đến trước, thư của req1 đến sau
        state.add_message(
            build_ninja_sample_mime(
                to_addr="ab.c@gmail.com",
                confirm_url_plain="https://www.ninja.co.jp/register/input?key=link_for_2",
            ),
            internal_dt=b2.captured_at_utc + timedelta(seconds=1),
        )
        state.add_message(
            build_ninja_sample_mime(
                to_addr="a.bc@gmail.com",
                confirm_url_plain="https://www.ninja.co.jp/register/input?key=link_for_1",
            ),
            internal_dt=b1.captured_at_utc + timedelta(seconds=2),
        )

        outcomes = reader.poll_once([req1, req2])
        by_id = {o.request_id: o for o in outcomes}
        self.assertEqual(by_id["req-1"].status, STATUS_CONFIRMED)
        self.assertEqual(by_id["req-1"].confirmation_url, "https://www.ninja.co.jp/register/input?key=link_for_1")
        self.assertEqual(by_id["req-2"].status, STATUS_CONFIRMED)
        self.assertEqual(by_id["req-2"].confirmation_url, "https://www.ninja.co.jp/register/input?key=link_for_2")

        # Gửi lại yêu cầu khác cũng không thể dùng lại link/UID đã cấp
        req3 = PendingMailRequest("req-3", "a.bc@gmail.com", b1)
        self.assertEqual(reader.poll_once([req3]), [])

    def test_recipient_header_mismatch_marks_check_mail(self):
        state = FakeImapServerState(uidvalidity=55, uidnext=20)
        cfg = ImapConfig(email="rootbox@gmail.com", app_password="app-secret-password")
        reader = GmailRegistrationReader(cfg, imap_factory=lambda h, p, t: FakeImapConnection(state))

        b1 = reader.mark_baseline("a.bc@gmail.com")
        b2 = reader.mark_baseline("ab.c@gmail.com")
        req1 = PendingMailRequest("req-1", "a.bc@gmail.com", b1)
        req2 = PendingMailRequest("req-2", "ab.c@gmail.com", b2)

        state.add_message(
            build_ninja_sample_mime(
                to_addr="a.bc@gmail.com",
                confirm_url_plain="https://www.ninja.co.jp/register/input?key=ambiguous",
                extra_headers={"Delivered-To": "ab.c@gmail.com"},
            ),
            internal_dt=b2.captured_at_utc + timedelta(seconds=1),
        )
        outcomes = reader.poll_once([req1, req2])
        self.assertEqual({o.request_id for o in outcomes}, {"req-1", "req-2"})
        self.assertTrue(all(o.status == STATUS_CHECK_MAIL for o in outcomes))

    def test_reconnect_and_uidvalidity_change_marks_check_mail(self):
        state = FakeImapServerState(uidvalidity=100, uidnext=5)
        cfg = ImapConfig(
            email="rootbox@gmail.com",
            app_password="app-secret-password",
            poll_interval=0.02,
            reconnect_delay=0.02,
        )
        reader = GmailRegistrationReader(cfg, imap_factory=lambda h, p, t: FakeImapConnection(state))
        baseline = reader.mark_baseline("a.b@gmail.com")
        req = PendingMailRequest("req-1", "a.b@gmail.com", baseline)

        # Mất kết nối giữa lúc đọc UID SEARCH rồi UIDVALIDITY thay đổi khi kết nối lại
        state.disconnect_next_searches = 1
        state.uidvalidity_on_disconnect = 101
        pending = [req]
        logs: list[str] = []
        outcomes = reader.wait_for_requests(
            lambda: pending,
            timeout_seconds=1.0,
            on_outcome=lambda o: pending.clear(),
            on_log=logs.append,
        )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, STATUS_CHECK_MAIL)
        self.assertTrue(any("kết nối lại" in m for m in logs))

    def test_stop_wakes_waiting_reader_immediately(self):
        state = FakeImapServerState(uidvalidity=100, uidnext=5)
        cfg = ImapConfig(
            email="rootbox@gmail.com",
            app_password="app-secret-password",
            poll_interval=5.0,
        )
        reader = GmailRegistrationReader(cfg, imap_factory=lambda h, p, t: FakeImapConnection(state))
        baseline = reader.mark_baseline("a.b@gmail.com")
        req = PendingMailRequest("req-1", "a.b@gmail.com", baseline)

        timer = threading.Timer(0.05, reader.request_stop)
        timer.start()
        try:
            outcomes = reader.wait_for_requests(lambda: [req], timeout_seconds=10.0)
        finally:
            timer.cancel()
        self.assertEqual(outcomes, [])


if __name__ == "__main__":
    unittest.main()

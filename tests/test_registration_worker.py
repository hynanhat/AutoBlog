import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from playwright.async_api import async_playwright

from gmail_fetcher import ImapAuthError, ImapConfig, ImapMailboxError
from hiragana_hook import OCRHiraganaHook
from ocr_client import HiraganaOCRClient, OCRConfig
from parsers import GmailLine
from registration_form import RegistrationFormConfig
from registration_worker import (
    RegistrationConfig,
    RegistrationWorker,
    RegistrationWorkerController,
    STATUS_CANCELLED,
    STATUS_CHECK_MAIL,
    STATUS_CONFIRMED,
    STATUS_EMAIL_EXISTS,
    STATUS_FORM_HANDOFF,
    STATUS_FORM_READY,
    STATUS_NOT_SUBMITTED,
    STATUS_REGISTERED,
    STATUS_SEND_UNKNOWN,
    STATUS_TIMEOUT,
    STATUS_WAITING_MAIL,
    classify_registration_response,
)
from tests.test_gmail_fetcher import (
    FakeImapConnection,
    FakeImapServerState,
    build_ninja_sample_mime,
)

BUTTON_IMAGE = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


class SimulatedNinjaRegistrationServer:
    """Máy chủ HTTP mô phỏng trang /register/ và /register/input của Ninja."""

    def __init__(self, imap_state: FakeImapServerState) -> None:
        self.imap_state = imap_state
        self.submitted_emails: list[str] = []
        self.existing_emails: set[str] = set()
        self.disconnect_emails: set[str] = set()
        self.server_error_emails: set[str] = set()
        self.blocked_emails: set[str] = set()
        self.form_action = "/register/"
        self.deliver_immediately_for: dict[str, str] = {}
        self.mark_seen_on_deliver: bool = False
        self.confirmed_tokens_opened: list[str] = []
        self.use_clipped_sprite_button: bool = False
        self.server_clock_offset: timedelta = timedelta(0)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        addr = self._server.server_address
        host = addr[0] if isinstance(addr, tuple) and addr else "127.0.0.1"
        return f"http://{host}:{self._server.server_port}"

    @property
    def register_url(self) -> str:
        return f"{self.base_url}/register/"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _make_handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/register/input"):
                    qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                    token = qs.get("key", [""])[0]
                    parent.confirmed_tokens_opened.append(token)
                    body = (
                        "<html><body><h1>本登録フォーム</h1>"
                        f"<form id='complete_form'><input name='token' value='{token}'></form>"
                        "</body></html>"
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                sprite_css = (
                    "<style>"
                    ".btn-registMailSend { margin: 0 auto; width: 140px; height: 51px; overflow: hidden; }"
                    ".btn-registMailSend:hover input { margin-top: -51px; }"
                    "</style>"
                    if parent.use_clipped_sprite_button else ""
                )
                submit_markup = (
                    "<div class='btn-registMailSend' id='registTopSend'>"
                    f"<input type='image' src='{BUTTON_IMAGE}' alt='この内容を送信' width='140' height='102' />"
                    "</div>"
                    if parent.use_clipped_sprite_button else
                    "<div id='registTopSend'>"
                    f"<input type='image' src='{BUTTON_IMAGE}' alt='テスト送信' width='100' height='28'"
                    " onclick='return false' />"
                    f"<input type='image' src='{BUTTON_IMAGE}' alt='この内容を送信' width='100' height='28' />"
                    "</div>"
                )
                body = (
                    f"<html><head>{sprite_css}</head><body>"
                    "<form action='/support/' method='post'>"
                    "<input type='email' name='contact' />"
                    "<button type='submit'>送信</button>"
                    "</form>"
                    f"<form id='register_form' method='post' action='{parent.form_action}'>"
                    "<div class='errorArea'></div>"
                    "<input type='hidden' name='hash' value='dummyhash' />"
                    "<input type='text' name='email' id='email' />"
                    f"{submit_markup}"
                    "</form>"
                    "<p>仮登録完了後にお送りするメールについて</p>"
                    "<script>"
                    f"const blockedEmails = {json.dumps(sorted(parent.blocked_emails))};"
                    "document.querySelector('#register_form').addEventListener('submit', (event) => {"
                    "  if (blockedEmails.includes(event.target.elements.email.value)) {"
                    "    event.preventDefault();"
                    "    event.target.querySelector('.errorArea').textContent = 'メールアドレスを確認してください。';"
                    "  }"
                    "});"
                    "</script>"
                    "</body></html>"
                ).encode("utf-8")
                if parent.server_clock_offset:
                    self.send_response_only(200, "OK")
                    self.send_header("Server", self.version_string())
                    self.send_header(
                        "Date",
                        format_datetime(
                            datetime.now(timezone.utc) + parent.server_clock_offset, usegmt=True
                        ),
                    )
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(length).decode("utf-8", errors="replace")
                qs = parse_qs(payload)
                email = (qs.get("email", [""])[0]).strip()
                parent.submitted_emails.append(email)

                if email in parent.disconnect_emails:
                    # Đóng socket đột ngột ngay sau khi nhận POST để mô phỏng mất kết nối sau khi gửi
                    self.connection.close()
                    return

                if email in parent.server_error_emails:
                    time.sleep(0.25)
                    self.send_response(500)
                    self.end_headers()
                    return

                if email in parent.existing_emails:
                    body = (
                        "<html><body>"
                        "<form id='register_form' method='post' action='/register/'>"
                        "<div class='errorArea'><p>入力されたメールアドレスはすでに登録されています。</p></div>"
                        "<input type='text' name='email' id='email' />"
                        "<div id='registTopSend'><input type='submit' value='この内容を送信' /></div>"
                        "</form>"
                        "</body></html>"
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # Nếu cấu hình gửi thư ngay lập tức (thư tới trước cả khi trình duyệt hiện xong trang thành công)
                if email in parent.deliver_immediately_for:
                    confirm_url = parent.deliver_immediately_for[email]
                    parent.imap_state.add_message(
                        build_ninja_sample_mime(to_addr=email, confirm_url_plain=confirm_url),
                        internal_dt=datetime.now(timezone.utc) + parent.server_clock_offset,
                        seen=parent.mark_seen_on_deliver,
                    )

                body = (
                    "<html><body>"
                    "<h2>仮登録が完了しました</h2>"
                    "<p>ご登録のメールアドレス宛に案内メールを送信しました。</p>"
                    "</body></html>"
                ).encode("utf-8")
                if parent.server_clock_offset:
                    self.send_response_only(200, "OK")
                    self.send_header("Server", self.version_string())
                    self.send_header(
                        "Date",
                        format_datetime(
                            datetime.now(timezone.utc) + parent.server_clock_offset, usegmt=True
                        ),
                    )
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

        return Handler


class RegistrationWorkerUnitTests(unittest.TestCase):
    def test_response_classification(self):
        self.assertEqual(
            classify_registration_response("仮登録が完了しました。メールを送信しました。", "")[0],
            STATUS_WAITING_MAIL,
        )
        self.assertEqual(
            classify_registration_response("", "入力されたメールアドレスはすでに登録されています。")[0],
            STATUS_EMAIL_EXISTS,
        )
        self.assertEqual(
            classify_registration_response("メンテナンス中です", "")[0],
            STATUS_SEND_UNKNOWN,
        )


class RegistrationWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.imap_state = FakeImapServerState(uidvalidity=2026, uidnext=10)
        self.server = SimulatedNinjaRegistrationServer(self.imap_state)
        self.server.start()
        self.addCleanup(self.server.stop)

        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)

    def _make_config(
        self,
        emails: list[tuple[int, str]],
        password: str = "app-secret-password",
        wait_timeout: float = 2.0,
        submit_timeout: float = 5.0,
    ) -> RegistrationConfig:
        return RegistrationConfig(
            entries=[GmailLine(line_no=ln, email=em) for ln, em in emails],
            imap_config=ImapConfig(
                email="rootbox@gmail.com",
                app_password=password,
                poll_interval=0.05,
                reconnect_delay=0.05,
            ),
            wait_timeout_seconds=wait_timeout,
            register_url=self.server.register_url,
            headless=True,
            submit_timeout_seconds=submit_timeout,
        )

    async def test_wrong_app_password_or_unreadable_inbox_sends_zero_times(self):
        for password, select_fails, expected_exc in [
            ("wrong-app-pw", False, ImapAuthError),
            ("app-secret-password", True, ImapMailboxError),
        ]:
            with self.subTest(password=password, select_fails=select_fails):
                self.imap_state.select_fails = select_fails
                status_events = []
                logs = []
                cfg = self._make_config([(1, "a.b@gmail.com")], password=password)
                worker = RegistrationWorker(
                    cfg,
                    on_status=lambda *args: status_events.append(args),
                    on_log=logs.append,
                    imap_factory=lambda h, p, t: FakeImapConnection(self.imap_state),
                    browser=self.browser,
                )
                with self.assertRaises(expected_exc):
                    await worker.run()
                self.assertEqual(self.server.submitted_emails, [])
                self.assertEqual(worker.records[0].send_attempts, 0)
                self.assertNotIn("wrong-app-pw", " ".join(logs))

    async def test_old_mail_ignored_and_fast_seen_mail_keeps_session_and_hides_token(self):
        # Thư cũ cùng tiêu đề trước mốc gửi
        self.imap_state.add_message(
            build_ninja_sample_mime(
                to_addr="u.ser@gmail.com",
                confirm_url_plain="https://www.ninja.co.jp/register/input?key=old_tok",
            ),
            internal_dt=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        real_confirm_url = "https://www.ninja.co.jp/register/input?key=fast_secret_tok_123"
        self.server.deliver_immediately_for["u.ser@gmail.com"] = real_confirm_url
        self.server.mark_seen_on_deliver = True

        cfg = self._make_config([(1, "u.ser@gmail.com")], wait_timeout=2.0)

        status_events = []
        logs = []
        worker = RegistrationWorker(
            cfg,
            on_status=lambda *args: status_events.append(args),
            on_log=logs.append,
            imap_factory=lambda h, p, t: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        try:
            await worker.run()
            rec = worker.records[0]
            self.assertEqual(rec.status, STATUS_CONFIRMED)
            self.assertEqual(self.server.submitted_emails, ["u.ser@gmail.com"])
            self.assertEqual(rec.send_attempts, 1)
            self.assertEqual(rec.confirmation_url, real_confirm_url)
            self.assertEqual(self.imap_state.logouts, 1)
            self.assertIsNotNone(rec.page)
            self.assertFalse(rec.page.is_closed())

            # Bảo vệ link: event và log không chứa URL hay token hay App Password
            for stt, email, status, ts in status_events:
                self.assertEqual(len((stt, email, status, ts)), 4)
                self.assertNotIn("fast_secret_tok_123", status)
                self.assertNotIn("https://", status)
            joined_logs = "\n".join(logs)
            self.assertNotIn("fast_secret_tok_123", joined_logs)
            self.assertNotIn("app-secret-password", joined_logs)
            milestones = ["Đã điền email", "Đã bấm Gửi", "Đã thấy POST", "Đã nhận phản hồi Ninja"]
            observed = [milestone for line in logs for milestone in milestones if milestone in line]
            self.assertEqual(observed, milestones)

            # Chặn route trên trang gốc để kiểm chứng mở đúng form trong phiên trình duyệt ban đầu
            await rec.page.route(
                "https://www.ninja.co.jp/register/input**",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body="<form id='complete_form'><h1>OK</h1></form>",
                ),
            )
            opened_page = await worker.open_confirmation_page(1)
            self.assertIs(opened_page, rec.page)
            self.assertIn("fast_secret_tok_123", opened_page.url)
            self.assertEqual(await opened_page.locator("#complete_form").count(), 1)
            await rec.page.route("**/register/input**", lambda route: route.fulfill(status=410))
            with self.assertRaisesRegex(RuntimeError, "còn hiệu lực"):
                await worker.open_confirmation_page(1)
            await rec.page.route("**/*", lambda route: route.abort())
            with self.assertRaises(RuntimeError) as failure:
                await worker.open_confirmation_page(1)
            self.assertNotIn(real_confirm_url, str(failure.exception))
            self.assertNotIn("fast_secret_tok_123", str(failure.exception))
        finally:
            await worker.close()

    async def test_already_exists_does_not_wait_for_mail(self):
        self.server.existing_emails.add("dup.user@gmail.com")
        cfg = self._make_config([(1, "dup.user@gmail.com")], wait_timeout=1.0)
        worker = RegistrationWorker(
            cfg,
            on_status=lambda *args: None,
            on_log=lambda msg: None,
            imap_factory=lambda h, p, t: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        try:
            await worker.run()
            self.assertEqual(worker.records[0].status, STATUS_EMAIL_EXISTS)
            self.assertEqual(worker.records[0].send_attempts, 1)
        finally:
            await worker.close()

    async def test_pre_submit_text_does_not_mask_http_500(self):
        address = "bad.request@gmail.com"
        self.server.server_error_emails.add(address)
        self.server.form_action = "/register/submit/"
        statuses = []
        logs = []
        worker = RegistrationWorker(
            self._make_config([(1, address)], wait_timeout=0.15),
            on_status=lambda _i, _email, status, _time: statuses.append(status),
            on_log=logs.append,
            imap_factory=lambda *_: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        try:
            await worker.run()
            self.assertEqual(self.server.submitted_emails, [address])
            self.assertNotIn(STATUS_WAITING_MAIL, statuses)
            self.assertEqual(worker.records[0].status, STATUS_SEND_UNKNOWN)
            self.assertTrue(any("Đã thấy POST /register/submit/" in line for line in logs))
        finally:
            await worker.close()

    async def test_click_with_inline_validation_sends_no_post(self):
        address = "invalid.registration@gmail.com"
        self.server.blocked_emails.add(address)
        statuses = []
        logs = []
        worker = RegistrationWorker(
            self._make_config([(1, address)], submit_timeout=1.0),
            on_status=lambda _i, _email, status, _time: statuses.append(status),
            on_log=logs.append,
            imap_factory=lambda *_: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        try:
            await worker.run()
            self.assertEqual(self.server.submitted_emails, [])
            self.assertEqual(worker.records[0].status, STATUS_NOT_SUBMITTED)
            self.assertEqual(worker.records[0].send_attempts, 0)
            self.assertNotIn(STATUS_WAITING_MAIL, statuses)
            self.assertEqual(await worker.retry_mail_check(), [])
            joined_logs = "\n".join(logs)
            self.assertIn("Đã điền email", joined_logs)
            self.assertIn("Đã bấm Gửi", joined_logs)
            self.assertNotIn("Đã thấy POST", joined_logs)
            self.assertNotIn("Đã nhận phản hồi Ninja", joined_logs)
        finally:
            await worker.close()

    async def test_timeout_and_disconnect_retry_mail_check_without_resending(self):
        # Dòng 1: thành công gửi nhưng hết giờ chờ thư
        # Dòng 2: mất kết nối trình duyệt ngay sau khi gửi -> Chưa xác định kết quả gửi
        self.server.disconnect_emails.add("drop.user@gmail.com")
        cfg = self._make_config(
            [(1, "late.user@gmail.com"), (2, "drop.user@gmail.com")],
            wait_timeout=0.15,
        )
        logs = []
        worker = RegistrationWorker(
            cfg,
            on_status=lambda *args: None,
            on_log=logs.append,
            imap_factory=lambda h, p, t: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        try:
            await worker.run()
            self.assertEqual(worker.records[0].status, STATUS_TIMEOUT)
            self.assertEqual(worker.records[1].status, STATUS_SEND_UNKNOWN)
            self.assertEqual([r.send_attempts for r in worker.records], [1, 1])
            self.assertEqual(len(self.server.submitted_emails), 2)
            self.assertEqual(sum("Đã thấy POST" in line for line in logs), 2)
            self.assertEqual(sum("Đã nhận phản hồi Ninja" in line for line in logs), 1)

            # Thư đến trễ sau khi đã hết giờ / mất kết nối
            self.imap_state.add_message(
                build_ninja_sample_mime(
                    to_addr="drop.user@gmail.com",
                    confirm_url_plain="https://www.ninja.co.jp/register/input?key=late_drop_tok",
                ),
                internal_dt=datetime.now(timezone.utc),
            )
            self.imap_state.add_message(
                build_ninja_sample_mime(
                    to_addr="late.user@gmail.com",
                    confirm_url_plain="https://www.ninja.co.jp/register/input?key=late_wait_tok",
                ),
                internal_dt=datetime.now(timezone.utc),
            )

            changed = await worker.retry_mail_check()
            self.assertEqual(set(changed), {1, 2})
            self.assertEqual(worker.records[0].status, STATUS_CONFIRMED)
            self.assertEqual(
                worker.records[0].confirmation_url,
                "https://www.ninja.co.jp/register/input?key=late_wait_tok",
            )
            self.assertEqual(worker.records[1].status, STATUS_CONFIRMED)
            self.assertEqual(
                worker.records[1].confirmation_url,
                "https://www.ninja.co.jp/register/input?key=late_drop_tok",
            )
            # Tổng số lần gửi đăng ký vẫn bằng 1 cho mỗi dòng
            self.assertEqual([r.send_attempts for r in worker.records], [1, 1])
            self.assertEqual(len(self.server.submitted_emails), 2)
        finally:
            await worker.close()

    async def test_stop_cancels_running_wait_immediately(self):
        cfg = self._make_config([(1, "wait.user@gmail.com")], wait_timeout=10.0)
        worker = RegistrationWorker(
            cfg,
            on_status=lambda *args: None,
            on_log=lambda msg: None,
            imap_factory=lambda h, p, t: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        try:
            task = asyncio.create_task(worker.run())
            for _ in range(50):
                if worker.records[0].status == STATUS_WAITING_MAIL:
                    break
                await asyncio.sleep(0.05)
            worker.request_stop()
            await asyncio.wait_for(task, timeout=2.0)
            self.assertEqual(worker.records[0].status, STATUS_CANCELLED)
        finally:
            await worker.close()

    async def test_two_dot_variants_playwright_out_of_order_keep_distinct_sessions(self):
        first, second = "a.bc@gmail.com", "ab.c@gmail.com"
        url1 = "https://www.ninja.co.jp/register/input/hash/" + "bc" * 20
        url2 = "https://www.ninja.co.jp/register/input/hash/" + "de" * 20
        cfg = self._make_config(
            [(1, first), (2, second)],
            wait_timeout=2.0,
        )
        worker = RegistrationWorker(
            cfg,
            on_status=lambda *args: None,
            on_log=lambda msg: None,
            imap_factory=lambda h, p, t: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        try:
            task = asyncio.create_task(worker.run())
            for _ in range(100):
                if all(r.status == STATUS_WAITING_MAIL for r in worker.records):
                    break
                await asyncio.sleep(0.05)

            # Thư của dòng 2 đến trước, dòng 1 đến sau
            self.imap_state.add_message(
                build_ninja_sample_mime(
                    to_addr=first, from_addr="oshirase@shinobi.jp",
                    subject="忍者ツールズユーザー登録完了メール",
                ),
                internal_dt=datetime.now(timezone.utc),
            )
            self.imap_state.add_message(
                build_ninja_sample_mime(to_addr=second, from_addr="oshirase@shinobi.jp", confirm_url_plain=url2),
                internal_dt=datetime.now(timezone.utc),
            )
            self.imap_state.add_message(
                build_ninja_sample_mime(to_addr=first, from_addr="oshirase@shinobi.jp", confirm_url_plain=url1),
                internal_dt=datetime.now(timezone.utc),
            )
            await asyncio.wait_for(task, timeout=3.0)

            self.assertEqual(self.server.submitted_emails, [first, second])
            self.assertIsNot(worker.records[0].page, worker.records[1].page)
            self.assertIsNot(worker.records[0].browser_context, worker.records[1].browser_context)
            for rec, expected in zip(worker.records, (url1, url2)):
                self.assertEqual(rec.status, STATUS_CONFIRMED)
                self.assertEqual(rec.confirmation_url, expected)
                await rec.page.route("**/*", lambda route: route.abort())
                await rec.page.route(expected, lambda route: route.fulfill(body="mock confirmation form"))
                opened = await worker.open_confirmation_page(rec.stt)
                self.assertIs(opened, rec.page)
                self.assertEqual(opened.url, expected)
        finally:
            await worker.close()

    async def test_form_handoff_releases_slot_and_stop_keeps_ready_tabs(self):
        addresses = ["r.ootbox@gmail.com", "ro.otbox@gmail.com", "roo.tbox@gmail.com", "root.box@gmail.com"]
        links = ["https://www.ninja.co.jp/register/input/hash/" + str(i) * 40 for i in range(1, 5)]
        self.server.deliver_immediately_for.update(zip(addresses, links))
        cfg = self._make_config(list(enumerate(addresses, start=1)))
        cfg.form = RegistrationFormConfig(password="Ninja_test1")
        cfg.max_sessions = 2
        cfg.headless = False
        ready = {i: asyncio.Event() for i in range(1, 5)}
        cancelled = asyncio.Event()
        navigation_requests = []
        new_context = self.browser.new_context

        async def intercept_confirmation(route):
            navigation_requests.append((route.request.method, route.request.url))
            await route.fulfill(
                status=200, content_type="text/html", body="<form><input name='manual-entry'></form>"
            )

        async def route_context(*args, **kwargs):
            context = await new_context(*args, **kwargs)
            await context.route("https://www.ninja.co.jp/**", intercept_confirmation)
            return context

        def on_status(stt, _address, status, _updated_at):
            if status == STATUS_FORM_READY:
                ready[stt].set()
            if stt == 4 and status == STATUS_CANCELLED:
                cancelled.set()

        worker = RegistrationWorker(
            cfg, on_status=on_status, on_log=lambda _msg: None,
            imap_factory=lambda *_: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        with patch.object(self.browser, "new_context", side_effect=route_context), patch(
            "registration_worker.fill_confirmation_form",
        ) as fill_form:
            task = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(asyncio.gather(ready[1].wait(), ready[2].wait()), timeout=8)
                self.assertCountEqual(self.server.submitted_emails, addresses[:2])
                self.assertEqual(worker.records[2].send_attempts, 0)
                first_page, second_page = (rec.page for rec in worker.records[:2])
                self.assertEqual((first_page.url, second_page.url), tuple(links[:2]))
                self.assertIsNot(first_page.context, second_page.context)
                manual_field = first_page.locator('input[name="manual-entry"]')
                await manual_field.fill("operator draft", timeout=3000)
                await worker.focus_form(1)
                self.assertEqual(await manual_field.input_value(), "operator draft")
                await worker.acknowledge_form(1)
                await asyncio.wait_for(ready[3].wait(), timeout=8)
                self.assertTrue(first_page.is_closed())
                self.assertFalse(second_page.is_closed())
                self.assertEqual(worker.records[0].status, STATUS_FORM_HANDOFF)
                self.assertEqual(worker.records[2].page.url, links[2])
                form_calls = [(args[0], args[1]) for args, _ in fill_form.await_args_list]
                self.assertCountEqual(
                    form_calls, [(first_page, addresses[0]), (second_page, addresses[1]),
                                 (worker.records[2].page, addresses[2])],
                )
                worker.request_stop()
                await asyncio.wait_for(cancelled.wait(), timeout=5)
                self.assertCountEqual(navigation_requests, [("GET", link) for link in links[:3]])
                self.assertCountEqual(self.server.submitted_emails, addresses[:3])
                self.assertEqual(worker.records[3].send_attempts, 0)
                for rec in worker.records[1:3]:
                    self.assertEqual(rec.status, STATUS_FORM_READY)
                    self.assertFalse(rec.page.is_closed())
                await second_page.set_content('<h1 class="ttl-registFinish">登録完了</h1>')
                await worker.acknowledge_form(2)
                await worker.acknowledge_form(3)
                await asyncio.wait_for(task, timeout=5)
                self.assertEqual(worker.records[1].status, STATUS_REGISTERED)
                self.assertEqual(worker.records[2].status, STATUS_FORM_HANDOFF)
            finally:
                if not task.done():
                    worker._discard_forms = True
                    worker.request_stop()
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await worker.close()

    async def test_shared_password_and_checkboxes_filled_end_to_end_across_batch(self):
        addresses = ["r.ootbox@gmail.com", "ro.otbox@gmail.com", "roo.tbox@gmail.com"]
        hashes = ["1" * 40, "2" * 40, "3" * 40]
        links = [f"https://www.ninja.co.jp/register/input/hash/{h}" for h in hashes]
        link_to_email = dict(zip(links, addresses))
        self.server.deliver_immediately_for.update(zip(addresses, links))

        cfg = self._make_config(list(enumerate(addresses, start=1)))
        cfg.form = RegistrationFormConfig(
            password="Shared.Pass_9",
            newsletter=True,
            accept_terms=True,
            accept_privacy=True,
        )
        cfg.max_sessions = 2
        cfg.headless = False

        ready = {i: asyncio.Event() for i in range(1, 4)}
        new_context = self.browser.new_context

        async def intercept_confirmation(route):
            email = link_to_email[route.request.url]
            html = f"""
            <html lang="ja"><body>
            <script>window.submissions = 0;</script>
            <form action="/register/confirm" method="post" onsubmit="window.submissions++; return false;">
                <table>
                    <tr><th>ID（メールアドレス）</th><td>{email}<br>※仮登録時のアドレス</td></tr>
                    <tr><th><img src="{BUTTON_IMAGE}" alt="必須" class="img-hissu" /> パスワード</th><td><input type="password" id="pw"></td></tr>
                    <tr><th><img src="{BUTTON_IMAGE}" alt="必須" class="img-hissu" /> パスワードの確認</th><td><input type="password" id="pw2"></td></tr>
                    <tr><th><img src="{BUTTON_IMAGE}" alt="必須" class="img-hissu" /> メールマガジン</th><td>
                        <input type="radio" name="mag" value="1" id="mag1"><label>受信する</label>
                        <input type="radio" name="mag" value="0" id="mag0" checked><label>受信しない</label>
                    </td></tr>
                    <tr><th><img src="{BUTTON_IMAGE}" alt="必須" class="img-hissu" /> 画像認証</th><td>
                        <img src="{BUTTON_IMAGE}" width="120" height="40"><br>
                        文字の入力 <input type="text" id="captcha">
                    </td></tr>
                </table>
                <div><input type="checkbox" id="terms"><a href="/terms">利用規約</a>に同意します</div>
                <div><input type="checkbox" id="privacy"><label>個人情報の取扱いに同意します</label></div>
                <button type="submit">上記の二項目に同意して、本登録</button>
            </form>
            </body></html>
            """
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

        async def route_context(*args, **kwargs):
            context = await new_context(*args, **kwargs)
            await context.route("https://www.ninja.co.jp/register/input/hash/**", intercept_confirmation)
            return context

        def on_status(stt, _address, status, _updated_at):
            if status == STATUS_FORM_READY:
                ready[stt].set()

        worker = RegistrationWorker(
            cfg,
            on_status=on_status,
            on_log=lambda _msg: None,
            imap_factory=lambda *_: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        with patch.object(self.browser, "new_context", side_effect=route_context):
            task = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(asyncio.gather(ready[1].wait(), ready[2].wait()), timeout=8)
                for idx in (0, 1):
                    page = worker.records[idx].page
                    self.assertEqual(await page.locator("#pw").input_value(), "Shared.Pass_9")
                    self.assertEqual(await page.locator("#pw2").input_value(), "Shared.Pass_9")
                    self.assertTrue(await page.locator("#mag1").is_checked())
                    self.assertTrue(await page.locator("#terms").is_checked())
                    self.assertTrue(await page.locator("#privacy").is_checked())
                    self.assertEqual(await page.locator("#captcha").input_value(), "")
                    self.assertEqual(await page.evaluate("document.activeElement.id"), "captcha")
                    self.assertEqual(await page.evaluate("window.submissions"), 0)

                await worker.acknowledge_form(1)
                await asyncio.wait_for(ready[3].wait(), timeout=8)
                page3 = worker.records[2].page
                self.assertEqual(await page3.locator("#pw").input_value(), "Shared.Pass_9")
                self.assertEqual(await page3.locator("#pw2").input_value(), "Shared.Pass_9")
                self.assertTrue(await page3.locator("#terms").is_checked())
                self.assertTrue(await page3.locator("#privacy").is_checked())

                await worker.acknowledge_form(2)
                await worker.acknowledge_form(3)
                await asyncio.wait_for(task, timeout=5)
                self.assertTrue(all(r.status == STATUS_FORM_HANDOFF for r in worker.records))
            finally:
                if not task.done():
                    worker._discard_forms = True
                    worker.request_stop()
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await worker.close()

    async def test_clipped_hover_sprite_submit_button_sends_post(self):
        address = "nhatsos.1.2.3@gmail.com"
        confirm_url = "https://www.ninja.co.jp/register/input/hash/" + "ab" * 20
        self.server.use_clipped_sprite_button = True
        self.server.deliver_immediately_for[address] = confirm_url
        cfg = self._make_config([(1, address)], wait_timeout=2.0, submit_timeout=5.0)
        logs = []
        worker = RegistrationWorker(
            cfg,
            on_status=lambda *args: None,
            on_log=logs.append,
            imap_factory=lambda *_: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        try:
            await worker.run()
            self.assertEqual(self.server.submitted_emails, [address])
            self.assertEqual(worker.records[0].status, STATUS_CONFIRMED)
            self.assertEqual(worker.records[0].confirmation_url, confirm_url)
            self.assertTrue(any("Đã thấy POST /register/" in line for line in logs))
        finally:
            await worker.close()

    async def test_clock_skew_against_server_date_still_matches_confirmation_mail(self):
        address = "h.u.y.n.hnhat3135@gmail.com"
        confirm_url = "https://www.ninja.co.jp/register/input/hash/" + "cd" * 20
        # Simulate local PC clock 30 minutes ahead of real internet UTC.
        self.server.server_clock_offset = timedelta(minutes=-30)
        self.server.deliver_immediately_for[address] = confirm_url
        cfg = self._make_config([(1, address)], wait_timeout=2.0, submit_timeout=5.0)
        cfg.imap_config = ImapConfig(
            email="rootbox@gmail.com",
            app_password="app-secret-password",
            poll_interval=0.05,
            clock_skew_seconds=60.0,
        )
        worker = RegistrationWorker(
            cfg,
            on_status=lambda *args: None,
            on_log=lambda _msg: None,
            imap_factory=lambda *_: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        try:
            await worker.run()
            self.assertEqual(worker.records[0].status, STATUS_CONFIRMED)
            self.assertEqual(worker.records[0].confirmation_url, confirm_url)
        finally:
            await worker.close()


class RegistrationWorkerControllerTests(unittest.TestCase):
    def test_controller_preserves_session_between_start_retry_and_open(self):
        imap_state = FakeImapServerState(uidvalidity=88, uidnext=1)
        server = SimulatedNinjaRegistrationServer(imap_state)
        server.start()
        self.addCleanup(server.stop)

        ctrl = RegistrationWorkerController()
        self.addCleanup(ctrl.shutdown)

        cfg = RegistrationConfig(
            entries=[GmailLine(line_no=1, email="c.trl@gmail.com")],
            imap_config=ImapConfig(
                email="rootbox@gmail.com",
                app_password="app-secret-password",
                poll_interval=0.05,
            ),
            wait_timeout_seconds=0.15,
            register_url=server.register_url,
            headless=True,
            submit_timeout_seconds=5.0,
        )

        done_evt = threading.Event()
        errors = []
        ctrl.start_batch(
            cfg,
            on_status=lambda *args: None,
            on_log=lambda msg: None,
            on_finished=lambda err: (errors.append(err), done_evt.set()),
            imap_factory=lambda h, p, t: FakeImapConnection(imap_state),
        )
        self.assertTrue(done_evt.wait(timeout=15.0))
        self.assertEqual(errors, [None])
        self.assertEqual(ctrl.worker.records[0].status, STATUS_TIMEOUT)

        # Thư đến trễ, bấm Kiểm tra thư lại trên cùng controller -> dùng lại cùng phiên trình duyệt
        imap_state.add_message(
            build_ninja_sample_mime(
                to_addr="c.trl@gmail.com",
                confirm_url_plain="https://www.ninja.co.jp/register/input?key=ctrl_tok_1",
            ),
            internal_dt=datetime.now(timezone.utc),
        )
        retry_evt = threading.Event()
        retry_res = []
        ctrl.retry_mail_check(lambda err, changed: (retry_res.append((err, changed)), retry_evt.set()))
        self.assertTrue(retry_evt.wait(timeout=5.0))
        self.assertEqual(retry_res, [(None, [1])])
        self.assertEqual(ctrl.worker.records[0].status, STATUS_CONFIRMED)
        self.assertEqual(ctrl.worker.records[0].send_attempts, 1)
        self.assertEqual(len(server.submitted_emails), 1)


class FakeOCRServer:
    """Máy chủ HTTP mô phỏng /health và /results theo giao thức ocr/server.py."""

    def __init__(self, inbox_dir: Path, normalized_text: str = "あいうえおか") -> None:
        self.inbox_dir = inbox_dir
        self.normalized_text = normalized_text
        self.seen_files: list[str] = []
        self.cursor = 0
        self.items: list[dict] = []
        self.lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        addr = self._server.server_address
        host = addr[0] if isinstance(addr, tuple) and addr else "127.0.0.1"
        return f"http://{host}:{self._server.server_port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _scan_inbox(self) -> None:
        with self.lock:
            for path in sorted(self.inbox_dir.glob("*.png")):
                if path.name in self.seen_files:
                    continue
                self.seen_files.append(path.name)
                self.cursor += 1
                self.items.append(
                    {
                        "id": self.cursor,
                        "cursor": self.cursor,
                        "filename": path.name,
                        "status": "done",
                        "result": {
                            "text": self.normalized_text,
                            "normalized_text": self.normalized_text,
                            "warnings": [],
                        },
                        "error": None,
                    }
                )

    def _make_handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/health"):
                    with parent.lock:
                        latest = parent.cursor
                    body = json.dumps(
                        {
                            "status": "ok",
                            "inbox": str(parent.inbox_dir),
                            "model": "mock-gemini",
                            "latest_cursor": latest,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if self.path.startswith("/results"):
                    parent._scan_inbox()
                    query_str = self.path.split("?", 1)[1] if "?" in self.path else ""
                    qs = parse_qs(query_str)
                    after = int(qs.get("after", ["0"])[0])
                    limit = int(qs.get("limit", ["100"])[0])
                    with parent.lock:
                        matched = [item for item in parent.items if item["cursor"] > after][:limit]
                        next_cursor = matched[-1]["cursor"] if matched else after
                        latest = parent.cursor
                    body = json.dumps(
                        {
                            "items": matched,
                            "next_cursor": next_cursor,
                            "latest_cursor": latest,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_response(404)
                self.end_headers()

        return Handler


class RegistrationWorkerOCRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.imap_state = FakeImapServerState(uidvalidity=2027, uidnext=1)
        self.server = SimulatedNinjaRegistrationServer(self.imap_state)
        self.server.start()
        self.addCleanup(self.server.stop)

        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)

    def _make_config(
        self,
        emails: list[tuple[int, str]],
        *,
        use_ocr: bool = True,
        ocr_url: str | None = None,
        max_attempts: int = 5,
        max_sessions: int = 2,
    ) -> RegistrationConfig:
        return RegistrationConfig(
            entries=[GmailLine(line_no=line_no, email=addr) for line_no, addr in emails],
            imap_config=ImapConfig(
                email="rootbox@gmail.com",
                app_password="app-secret-password",
                poll_interval=0.05,
            ),
            wait_timeout_seconds=3.0,
            register_url=self.server.register_url,
            headless=False,
            submit_timeout_seconds=5.0,
            form=RegistrationFormConfig(
                password="Shared.Pass_9",
                newsletter=False,
                accept_terms=True,
                accept_privacy=True,
            ),
            max_sessions=max_sessions,
            use_ocr=use_ocr,
            ocr_url=ocr_url,
            ocr_max_attempts=max_attempts,
            ocr_timeout_seconds=10.0,
        )

    @staticmethod
    def _build_confirmation_html(
        email: str,
        *,
        error_html: str = "",
        newsletter_yes_checked: bool = True,
    ) -> str:
        yes_checked = "checked" if newsletter_yes_checked else ""
        no_checked = "" if newsletter_yes_checked else "checked"
        return f"""
        <html lang="ja"><head><style>
            .btn-regist {{ margin: 0 auto; width: 300px; height: 51px; overflow: hidden; }}
            .btn-regist:hover input {{ margin-top: -51px; }}
        </style></head><body>
        {error_html}
        <form action="/register/confirm" method="post">
            <table>
                <tr><th>ID（メールアドレス）</th><td>{email}<br>※仮登録時のアドレス</td></tr>
                <tr><th><img src="{BUTTON_IMAGE}" alt="必須" class="img-hissu" /> パスワード</th><td><input type="password" name="password" id="pw"></td></tr>
                <tr><th><img src="{BUTTON_IMAGE}" alt="必須" class="img-hissu" /> パスワードの確認</th><td><input type="password" name="password_confirm" id="pw2"></td></tr>
                <tr><th><img src="{BUTTON_IMAGE}" alt="必須" class="img-hissu" /> メールマガジン</th><td>
                    <input type="radio" name="mag" value="1" id="mag1" {yes_checked}><label>受信する</label>
                    <input type="radio" name="mag" value="0" id="mag0" {no_checked}><label>受信しない</label>
                </td></tr>
                <tr><th><img src="{BUTTON_IMAGE}" alt="必須" class="img-hissu" /> 画像認証</th><td>
                    <img id="authimg" src="{BUTTON_IMAGE}" width="120" height="40"><br>
                    <a href="#refresh" onclick="fetch('/register/refresh-ping'); return false;">読みにくいので画像変更</a><br>
                    文字の入力 <input type="text" name="authimg" id="captcha">
                </td></tr>
            </table>
            <div><input type="checkbox" name="terms" value="1" id="terms"><a href="/terms">利用規約</a>に同意します</div>
            <div><input type="checkbox" name="privacy" value="1" id="privacy"><label>個人情報の取扱いに同意します</label></div>
            <div id="registerInputSend" class="btn-regist">
                <input type="image" src="{BUTTON_IMAGE}" alt="上記の二項目に同意して、本登録" width="300" height="102">
            </div>
        </form>
        </body></html>
        """

    async def test_ocr_http_end_to_end_fills_submits_confirms_and_frees_slots(self):
        addresses = ["r.ootbox@gmail.com", "ro.otbox@gmail.com", "roo.tbox@gmail.com"]
        links = [f"https://www.ninja.co.jp/register/input/hash/{str(i) * 40}" for i in range(1, 4)]
        link_to_email = dict(zip(links, addresses))
        self.server.deliver_immediately_for.update(zip(addresses, links))

        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            inbox_dir = tmp_path / "inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)
            cursor_file = tmp_path / "ocr_cursor_register.json"
            ocr_server = FakeOCRServer(inbox_dir, normalized_text="あいうえおか")
            ocr_server.start()
            self.addCleanup(ocr_server.stop)

            cfg = self._make_config(
                list(enumerate(addresses, start=1)),
                use_ocr=True,
                ocr_url=ocr_server.base_url,
                max_sessions=2,
            )
            submitted_payloads = []
            confirm_steps = []
            new_context = self.browser.new_context

            async def intercept_ninja(route):
                req = route.request
                if req.method == "GET" and req.url in link_to_email:
                    email = link_to_email[req.url]
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=self._build_confirmation_html(email),
                    )
                    return
                if req.method == "POST" and req.url.endswith("/register/confirm"):
                    data = parse_qs(req.post_data or "")
                    submitted_payloads.append(data)
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=(
                            "<html lang='ja'><body>"
                            "<form action='/register/finish' method='post'>"
                            "<div id='registerInputSend2'><button type='submit'>本登録する</button></div>"
                            "</form></body></html>"
                        ),
                    )
                    return
                if req.method == "POST" and req.url.endswith("/register/finish"):
                    confirm_steps.append(req.url)
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body="<html lang='ja'><body><h3 class='ttl-registFinish'>新規ユーザー登録完了です！</h3></body></html>",
                    )
                    return
                await route.abort()

            async def route_context(*args, **kwargs):
                context = await new_context(*args, **kwargs)
                await context.route("https://www.ninja.co.jp/**", intercept_ninja)
                return context

            logs: list[str] = []
            worker = RegistrationWorker(
                cfg,
                on_status=lambda *args: None,
                on_log=logs.append,
                imap_factory=lambda *_: FakeImapConnection(self.imap_state),
                browser=self.browser,
            )

            def make_ocr_config(**kwargs):
                kwargs["state_file"] = cursor_file
                kwargs["poll_interval"] = 0.05
                return OCRConfig(**kwargs)

            with patch.object(self.browser, "new_context", side_effect=route_context), \
                 patch("registration_worker.ensure_ocr_server_running", return_value=(True, "Mock OCR sẵn sàng")), \
                 patch("registration_worker.OCRConfig", side_effect=make_ocr_config):
                await asyncio.wait_for(worker.run(), timeout=20.0)
                try:
                    self.assertEqual([rec.status for rec in worker.records], [STATUS_REGISTERED] * 3)
                    self.assertEqual(len(ocr_server.seen_files), 3)
                    self.assertEqual(len(submitted_payloads), 3)
                    self.assertEqual(len(confirm_steps), 3)
                    for payload in submitted_payloads:
                        self.assertEqual(payload.get("password"), ["Shared.Pass_9"])
                        self.assertEqual(payload.get("password_confirm"), ["Shared.Pass_9"])
                        self.assertEqual(payload.get("mag"), ["0"])
                        self.assertEqual(payload.get("authimg"), ["あいうえおか"])
                        self.assertEqual(payload.get("terms"), ["1"])
                        self.assertEqual(payload.get("privacy"), ["1"])
                        self.assertIn("x", payload)
                        self.assertIn("y", payload)
                    self.assertTrue(all(rec.page is None for rec in worker.records))
                finally:
                    await worker.close()

    async def test_ocr_retry_refreshes_and_refills_after_wrong_captcha(self):
        address = "r.etry@gmail.com"
        confirm_url = "https://www.ninja.co.jp/register/input/hash/" + "ee" * 20
        self.server.deliver_immediately_for[address] = confirm_url
        cfg = self._make_config([(1, address)], use_ocr=True, max_attempts=4, max_sessions=1)

        with TemporaryDirectory() as tmp_dir:
            dummy_client = HiraganaOCRClient(
                OCRConfig(base_url="http://127.0.0.1:1", state_file=Path(tmp_dir) / "cursor.json")
            )
            dummy_client.check_health = lambda: (True, {"status": "ok"})  # type: ignore[method-assign]
            hook = OCRHiraganaHook(ocr_client=dummy_client)

            solve_calls = []

            async def fake_solve_ocr(_image_bytes, idx, email):
                solve_calls.append((idx, email))
                return [None, "wrongk", "あいうえおか"][len(solve_calls) - 1]

            hook.solve_ocr = fake_solve_ocr  # type: ignore[method-assign]

            refreshes: list[str] = []
            submissions: list[dict] = []
            new_context = self.browser.new_context

            async def intercept_ninja(route):
                req = route.request
                if req.method == "GET" and req.url == confirm_url:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=self._build_confirmation_html(address),
                    )
                    return
                if req.url.endswith("/register/refresh-ping"):
                    refreshes.append(req.url)
                    await route.fulfill(status=204, body="")
                    return
                if req.method == "POST" and req.url.endswith("/register/confirm"):
                    data = parse_qs(req.post_data or "")
                    submissions.append(data)
                    if len(submissions) == 1:
                        # Lần 1 sai CAPTCHA: Ninja tải lại form, xóa trắng mật khẩu và bỏ tích điều khoản
                        await route.fulfill(
                            status=200,
                            content_type="text/html; charset=utf-8",
                            body=self._build_confirmation_html(
                                address,
                                error_html="<div class='errorArea'>画像認証の文字が一致しません。</div>",
                            ),
                        )
                    else:
                        await route.fulfill(
                            status=200,
                            content_type="text/html; charset=utf-8",
                            body="<html><body><p>登録が完了しました。</p></body></html>",
                        )
                    return
                await route.abort()

            async def route_context(*args, **kwargs):
                context = await new_context(*args, **kwargs)
                await context.route("https://www.ninja.co.jp/**", intercept_ninja)
                return context

            worker = RegistrationWorker(
                cfg,
                on_status=lambda *args: None,
                on_log=lambda _msg: None,
                imap_factory=lambda *_: FakeImapConnection(self.imap_state),
                browser=self.browser,
                hiragana_hook=hook,
            )
            with patch.object(self.browser, "new_context", side_effect=route_context):
                await asyncio.wait_for(worker.run(), timeout=15.0)
                try:
                    self.assertEqual(len(solve_calls), 3)
                    self.assertEqual(len(refreshes), 1)
                    self.assertEqual(len(submissions), 2)
                    self.assertEqual(submissions[0].get("authimg"), ["wrongk"])
                    self.assertEqual(submissions[1].get("authimg"), ["あいうえおか"])
                    self.assertEqual(submissions[1].get("password"), ["Shared.Pass_9"])
                    self.assertEqual(submissions[1].get("password_confirm"), ["Shared.Pass_9"])
                    self.assertEqual(submissions[1].get("mag"), ["0"])
                    self.assertEqual(submissions[1].get("terms"), ["1"])
                    self.assertEqual(submissions[1].get("privacy"), ["1"])
                    self.assertEqual(worker.records[0].status, STATUS_REGISTERED)
                finally:
                    await worker.close()

    async def test_ocr_unavailable_and_exhausted_retries_fall_back_to_manual_handoff(self):
        # 1. OCR server chưa bật -> chuyển thẳng sang Form sẵn sàng – chờ tiếp nhận
        address1 = "f.allback1@gmail.com"
        confirm_url1 = "https://www.ninja.co.jp/register/input/hash/" + "f1" * 20
        self.server.deliver_immediately_for[address1] = confirm_url1
        cfg1 = self._make_config([(1, address1)], use_ocr=True, max_attempts=3, max_sessions=1)

        ready_evt = asyncio.Event()
        new_context = self.browser.new_context

        async def route_manual(route):
            req = route.request
            if req.method == "GET":
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=self._build_confirmation_html(address1),
                )
                return
            await route.abort()

        async def route_context(*args, **kwargs):
            context = await new_context(*args, **kwargs)
            await context.route("https://www.ninja.co.jp/**", route_manual)
            return context

        worker1 = RegistrationWorker(
            cfg1,
            on_status=lambda _s, _e, status, _t: ready_evt.set() if status == STATUS_FORM_READY else None,
            on_log=lambda _msg: None,
            imap_factory=lambda *_: FakeImapConnection(self.imap_state),
            browser=self.browser,
        )
        with patch.object(self.browser, "new_context", side_effect=route_context), \
             patch("registration_worker.ensure_ocr_server_running", return_value=(False, "OCR server offline")):
            task1 = asyncio.create_task(worker1.run())
            try:
                await asyncio.wait_for(ready_evt.wait(), timeout=8.0)
                page = worker1.records[0].page
                self.assertEqual(await page.locator("#pw").input_value(), "Shared.Pass_9")
                self.assertTrue(await page.locator("#mag0").is_checked())
                self.assertTrue(await page.locator("#terms").is_checked())
                self.assertTrue(await page.locator("#privacy").is_checked())
                self.assertEqual(await page.locator("#captcha").input_value(), "")
                await worker1.acknowledge_form(1)
                await asyncio.wait_for(task1, timeout=5.0)
                self.assertEqual(worker1.records[0].status, STATUS_FORM_HANDOFF)
            finally:
                if not task1.done():
                    worker1._discard_forms = True
                    worker1.request_stop()
                    task1.cancel()
                await asyncio.gather(task1, return_exceptions=True)
                await worker1.close()

        # 2. OCR thử hết 2 lần đều bị từ chối -> xóa trống CAPTCHA và bàn giao nhập tay
        address2 = "f.allback2@gmail.com"
        confirm_url2 = "https://www.ninja.co.jp/register/input/hash/" + "f2" * 20
        self.server.deliver_immediately_for[address2] = confirm_url2
        cfg2 = self._make_config([(1, address2)], use_ocr=True, max_attempts=2, max_sessions=1)
        ready_evt2 = asyncio.Event()

        with TemporaryDirectory() as tmp_dir:
            dummy_client = HiraganaOCRClient(
                OCRConfig(base_url="http://127.0.0.1:1", state_file=Path(tmp_dir) / "cursor2.json")
            )
            hook = OCRHiraganaHook(ocr_client=dummy_client)

            async def always_wrong(_img, _idx, _email):
                return "wrongx"

            hook.solve_ocr = always_wrong  # type: ignore[method-assign]

            async def route_reject(route):
                req = route.request
                if req.method == "GET":
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=self._build_confirmation_html(address2),
                    )
                    return
                if req.method == "POST":
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=self._build_confirmation_html(
                            address2,
                            error_html="<div class='errorArea'>画像認証の文字が一致しません。</div>",
                        ),
                    )
                    return
                await route.abort()

            async def route_context2(*args, **kwargs):
                context = await new_context(*args, **kwargs)
                await context.route("https://www.ninja.co.jp/**", route_reject)
                return context

            worker2 = RegistrationWorker(
                cfg2,
                on_status=lambda _s, _e, status, _t: ready_evt2.set() if status == STATUS_FORM_READY else None,
                on_log=lambda _msg: None,
                imap_factory=lambda *_: FakeImapConnection(self.imap_state),
                browser=self.browser,
                hiragana_hook=hook,
            )
            with patch.object(self.browser, "new_context", side_effect=route_context2):
                task2 = asyncio.create_task(worker2.run())
                try:
                    await asyncio.wait_for(ready_evt2.wait(), timeout=10.0)
                    page2 = worker2.records[0].page
                    self.assertEqual(await page2.locator("#captcha").input_value(), "")
                    self.assertEqual(await page2.evaluate("document.activeElement.id"), "captcha")
                    self.assertEqual(await page2.locator("#pw").input_value(), "Shared.Pass_9")
                    self.assertTrue(await page2.locator("#terms").is_checked())
                    self.assertTrue(await page2.locator("#privacy").is_checked())
                    await worker2.acknowledge_form(1)
                    await asyncio.wait_for(task2, timeout=5.0)
                    self.assertEqual(worker2.records[0].status, STATUS_FORM_HANDOFF)
                finally:
                    if not task2.done():
                        worker2._discard_forms = True
                        worker2.request_stop()
                        task2.cancel()
                    await asyncio.gather(task2, return_exceptions=True)
                    await worker2.close()


if __name__ == "__main__":
    unittest.main()

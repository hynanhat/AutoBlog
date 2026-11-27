"""
Kết nối Gmail qua IMAP (SSL/TLS), đọc thư bằng thư viện email của Python,
đối chiếu người nhận theo đúng địa chỉ gốc (giữ nguyên dấu chấm) và trích xuất link xác nhận Ninja.
"""

from __future__ import annotations

import email
import email.header
import email.parser
import email.policy
import email.utils
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import imaplib
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import threading
import time
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from dotenv import dotenv_values
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from imapclient import IMAPClient

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_IMAP_TIMEOUT = 15.0
DEFAULT_POLL_INTERVAL = 15.0
DEFAULT_RECONNECT_DELAY = 15.0
DEFAULT_CLOCK_SKEW_SECONDS = 600.0

NINJA_REGISTRATION_SUBJECT = "忍者ツールズユーザー登録のご案内"
DEFAULT_CONFIRMATION_HOSTS = ("www.ninja.co.jp", "ninja.co.jp")
HEADER_SECTION = "HEADER.FIELDS (SUBJECT FROM TO DELIVERED-TO X-ORIGINAL-TO ENVELOPE-TO CC)"

STATUS_CONFIRMED = "Đã nhận link xác nhận"
STATUS_CHECK_MAIL = "Cần kiểm tra thư"

_PLAIN_URL_REGEX = re.compile(r"https?://[^\s<>\"'\u3000]+", re.IGNORECASE)
_HASH_PATH = re.compile(r"/register/input/hash/[0-9a-fA-F]{40}")
_WRAPPED_HASH_URL = re.compile(
    r"https://(?:www\.)?ninja\.co\.jp/register/input/hash/"
    r"(?:[0-9a-f]*[ \t]*\r?\n[ \t]*)+[0-9a-f]+(?=$|[\s<>\"'」』】、.,!)])",
    re.IGNORECASE,
)
_TRAILING_URL_PUNCT = ".,;:!?)>]}'\"」』】、"
_EMAIL_IN_TEXT_REGEX = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9._%+-])")


class GmailFetcherError(Exception):
    """Lỗi cơ sở của bộ đọc Gmail IMAP."""


class ImapAuthError(GmailFetcherError):
    """Đăng nhập IMAP thất bại (sai Gmail gốc hoặc App Password)."""


class OAuthClientConfigError(ImapAuthError):
    """The installation needs a valid Google Desktop client."""


class ImapMailboxError(GmailFetcherError):
    """Không mở được hộp thư INBOX ở chế độ chỉ đọc hoặc thiếu UIDVALIDITY/UIDNEXT."""


class ImapConnectionError(GmailFetcherError):
    """Lỗi kết nối, socket hoặc mất kết nối IMAP giữa phiên."""


class ImapStoppedError(GmailFetcherError):
    """Bộ đọc IMAP đã được yêu cầu dừng."""


@dataclass(frozen=True)
class ImapConfig:
    """Thông tin kết nối Gmail IMAP."""

    email: str
    app_password: str = field(default="", repr=False)
    host: str = DEFAULT_IMAP_HOST
    port: int = DEFAULT_IMAP_PORT
    mailbox: str = "INBOX"
    timeout: float = DEFAULT_IMAP_TIMEOUT
    poll_interval: float = DEFAULT_POLL_INTERVAL
    reconnect_delay: float = DEFAULT_RECONNECT_DELAY
    clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS
    oauth_token: Optional[Callable[[], str]] = field(default=None, repr=False)


@dataclass(frozen=True)
class NinjaMailFilterConfig:
    """Quy tắc lọc thư đăng ký và URL xác nhận của Ninja."""

    subject_keyword: str = NINJA_REGISTRATION_SUBJECT
    allowed_sender_domains: Tuple[str, ...] = ("ninja.co.jp",)
    allowed_sender_addresses: Tuple[str, ...] = ("oshirase@shinobi.jp",)
    confirmation_hosts: Tuple[str, ...] = DEFAULT_CONFIRMATION_HOSTS


@dataclass(frozen=True)
class InboxBaseline:
    """Mốc hộp thư INBOX lưu ngay trước khi gửi đăng ký."""

    uidvalidity: int
    uidnext: int
    captured_at_utc: datetime
    request_email: str
    mailbox: str = "INBOX"


@dataclass(frozen=True)
class PendingMailRequest:
    """Yêu cầu đăng ký đang cần đối chiếu thư trên IMAP."""

    request_id: str
    email: str
    baseline: InboxBaseline


@dataclass(frozen=True)
class ParsedNinjaEmail:
    """Kết quả giải mã MIME và trích URL từ một thư trong INBOX."""

    uid: int
    internal_date_utc: Optional[datetime]
    subject: str
    from_addresses: Tuple[str, ...]
    to_addresses: Tuple[str, ...]
    other_recipient_addresses: Tuple[str, ...]
    plain_bodies: Tuple[str, ...] = field(repr=False)
    html_bodies: Tuple[str, ...] = field(repr=False)
    confirmation_urls: Tuple[str, ...] = field(default=(), repr=False)
    body_mentioned_emails: Tuple[str, ...] = ()


@dataclass(frozen=True)
class MailMatchOutcome:
    """Kết quả đối chiếu thư cho một mã yêu cầu."""

    request_id: str
    status: str
    confirmation_url: Optional[str] = field(default=None, repr=False)
    reason: str = ""
    uid: Optional[int] = None


def normalize_app_password(raw_password: str) -> str:
    """Loại bỏ khoảng trắng giữa các nhóm 4 ký tự khi dán App Password từ Google."""
    stripped = (raw_password or "").strip()
    compact = "".join(stripped.split())
    if len(compact) == 16 and compact.isascii() and compact.isalpha():
        return compact
    return stripped


def load_gmail_credentials(
    ui_user: str = "",
    ui_password: str = "",
    env_file: Optional[Path] = None,
) -> Tuple[str, str]:
    """
    Lấy Gmail gốc và App Password từ giao diện, fallback sang biến môi trường
    GMAIL_INBOX_USER và GMAIL_APP_PASSWORD (hoặc đọc trực tiếp từ .env mà không ghi đè os.environ).
    """
    target_env = env_file if env_file is not None else Path(__file__).resolve().parent / ".env"
    file_values = dotenv_values(target_env) if target_env.is_file() else {}

    inbox_user = (
        (ui_user or "").strip()
        or os.getenv("GMAIL_INBOX_USER", "").strip()
        or (file_values.get("GMAIL_INBOX_USER") or "").strip()
    )
    app_password = normalize_app_password(
        (ui_password or "").strip()
        or os.getenv("GMAIL_APP_PASSWORD", "").strip()
        or (file_values.get("GMAIL_APP_PASSWORD") or "").strip()
    )
    return inbox_user, app_password


OAUTH_CLIENT_FILENAME = "gmail_oauth_client.json"


def _user_oauth_client_file() -> Path:
    local_data = os.environ.get("LOCALAPPDATA") or Path.home() / ".config"
    return Path(local_data) / "AutoBlog" / OAUTH_CLIENT_FILENAME


def configured_oauth_client() -> Optional[Path]:
    for path in (_user_oauth_client_file(), Path(__file__).with_name(OAUTH_CLIENT_FILENAME)):
        if path.is_file():
            return path
    return None


def _read_desktop_client(path: Path) -> dict:
    try:
        installed = json.loads(path.read_text(encoding="utf-8-sig"))["installed"]
        client_id, secret = installed["client_id"], installed["client_secret"]
        if not all(isinstance(value, str) and value.strip() for value in (client_id, secret)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise OAuthClientConfigError("Chọn tệp JSON OAuth loại Desktop app do Google cấp cho AutoBlog.") from None
    return {"installed": {
        "client_id": client_id,
        "client_secret": secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}


def install_oauth_client(source: Path) -> None:
    config = _read_desktop_client(source)
    target = _user_oauth_client_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
        staged = Path(temp.name)
    try:
        staged.write_text(json.dumps(config), encoding="utf-8")
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)


class GoogleMailboxLogin:
    """Interactive desktop consent. Credentials live only for this app session."""

    def __init__(self, email_address: str, client_file: str | Path):
        self.email = email_address
        self.client_file = client_file
        self._credentials = None
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            try:
                if self._credentials and not self._credentials.valid:
                    self._credentials.refresh(Request())
                if self._credentials is None:
                    flow = InstalledAppFlow.from_client_config(
                        _read_desktop_client(Path(self.client_file)),
                        ["https://mail.google.com/"], autogenerate_code_verifier=True
                    )
                    self._credentials = flow.run_local_server(
                        host="127.0.0.1", port=0, timeout_seconds=300, login_hint=self.email,
                        authorization_prompt_message=None,
                        success_message="Bạn có thể quay lại AutoBlog để xem kết quả kết nối Gmail.",
                    )
                return self._credentials.token
            except OAuthClientConfigError:
                raise
            except Exception as exc:
                self._credentials = None
                if type(exc).__name__ == "WSGITimeoutError":
                    raise ImapAuthError(
                        "Hết thời gian chờ xác thực Google trên trình duyệt. Hãy bấm Đăng nhập lại."
                    ) from None
                raise ImapAuthError("Đăng nhập Google chưa hoàn tất. Hãy đăng nhập lại và cấp quyền Gmail cho AutoBlog.") from None


def _default_imap_factory(host: str, port: int, timeout: float) -> IMAPClient:
    conn = IMAPClient(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)
    conn.normalise_times = False
    return conn


class _HTMLLinkAndTextExtractor(HTMLParser):
    """Trích các href từ thẻ <a>/<area> và văn bản hiển thị trong HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: List[str] = []
        self._text_chunks: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_l = tag.lower()
        if tag_l in ("script", "style", "noscript"):
            self._skip_depth += 1
            return
        if tag_l in ("a", "area"):
            for name, val in attrs:
                if name.lower() == "href" and val:
                    cleaned = val.strip()
                    if cleaned:
                        self.hrefs.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag_l in ("p", "div", "br", "li", "tr", "td"):
            self._text_chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._text_chunks.append(data)

    @property
    def text(self) -> str:
        return "".join(self._text_chunks)


def _decode_header_value(msg: email.message.Message, header_name: str) -> str:
    raw_values = msg.get_all(header_name, [])
    if not raw_values:
        return ""
    parts: List[str] = []
    for raw in raw_values:
        if isinstance(raw, str):
            if any(0xDC80 <= ord(ch) <= 0xDCFF for ch in raw):
                parts.append(
                    _decode_bytes_with_fallbacks(raw.encode("ascii", "surrogateescape"), None)
                )
            else:
                parts.append(raw)
            continue
        try:
            parts.append(str(raw))
        except Exception:
            try:
                decoded_chunks = email.header.decode_header(raw)
                chunk_strs = []
                for chunk, charset in decoded_chunks:
                    if isinstance(chunk, bytes):
                        chunk_strs.append(_decode_bytes_with_fallbacks(chunk, charset))
                    else:
                        chunk_strs.append(str(chunk))
                parts.append("".join(chunk_strs))
            except Exception:
                pass
    return ", ".join(p for p in parts if p)


def _extract_header_addresses(msg: email.message.Message, header_names: Sequence[str]) -> Tuple[str, ...]:
    raw_headers: List[str] = []
    extracted: List[str] = []
    seen = set()

    def _add_addr(addr: str) -> None:
        cleaned = addr.strip().lower()
        if cleaned and "@" in cleaned and not any(ch.isspace() for ch in cleaned):
            if cleaned not in seen:
                seen.add(cleaned)
                extracted.append(cleaned)

    for name in header_names:
        for val in msg.get_all(name, []):
            addresses_attr = getattr(val, "addresses", None)
            if addresses_attr:
                used_structured = False
                for addr_obj in addresses_attr:
                    if getattr(addr_obj, "username", "") and getattr(addr_obj, "domain", ""):
                        _add_addr(f"{addr_obj.username}@{addr_obj.domain}")
                        used_structured = True
                if used_structured:
                    continue
            try:
                raw_str = str(val)
            except Exception:
                continue
            raw_headers.append(raw_str)

    if raw_headers:
        for _display, addr in email.utils.getaddresses(raw_headers):
            if "<" in addr and ">" in addr:
                _d2, inner = email.utils.parseaddr(addr)
                if inner:
                    addr = inner
            _add_addr(addr)

    return tuple(extracted)


def _decode_bytes_with_fallbacks(data: bytes, declared_charset: Optional[str]) -> str:
    candidates: List[str] = []
    if declared_charset:
        candidates.append(declared_charset)
    for fallback in ("utf-8", "iso-2022-jp", "cp932", "shift_jis", "euc_jp", "latin-1"):
        if fallback.lower() not in {c.lower() for c in candidates}:
            candidates.append(fallback)
    for enc in candidates:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _decode_part_text(part: email.message.Message) -> str:
    charset = part.get_content_charset()
    try:
        payload = part.get_payload(decode=True)
        if isinstance(payload, (bytes, bytearray)):
            return _decode_bytes_with_fallbacks(bytes(payload), charset)
    except Exception:
        pass
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            return _decode_bytes_with_fallbacks(content, charset)
    except Exception:
        pass
    raw_payload = part.get_payload()
    return raw_payload if isinstance(raw_payload, str) else ""


def _clean_plain_url(url: str) -> str:
    return url.strip().rstrip(_TRAILING_URL_PUNCT)


def _canonical_url_key(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port and not (scheme == "https" and parsed.port == 443) else ""
    path = parsed.path or "/"
    if hostname in DEFAULT_CONFIRMATION_HOSTS and _HASH_PATH.fullmatch(path):
        return f"https://www.ninja.co.jp{path}"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{hostname}{port}{path}{query}"


def is_confirmation_url(
    url: str, filter_config: Optional[NinjaMailFilterConfig] = None
) -> bool:
    """
    Lọc URL xác nhận hợp lệ của Ninja:
    - Chỉ chấp nhận HTTPS
    - Đúng hostname (ví dụ www.ninja.co.jp / ninja.co.jp)
    - Đường dẫn thuộc luồng đăng ký (/register/...) và mang tham số/token xác nhận,
      loại bỏ trang đăng ký gốc không có token và các link trợ giúp/điều khoản.
    """
    cfg = filter_config or NinjaMailFilterConfig()
    try:
        parsed = urlsplit(url.strip())
        if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            return False
    except ValueError:
        return False

    if parsed.scheme.lower() != "https":
        return False
    hostname = (parsed.hostname or "").lower()
    allowed_hosts = {h.lower() for h in cfg.confirmation_hosts}
    if hostname not in allowed_hosts:
        return False

    if parsed.fragment:
        return False
    if _HASH_PATH.fullmatch(parsed.path):
        return not parsed.query
    params = parse_qs(parsed.query, keep_blank_values=True)
    keys = params.get("key", [])
    return (
        parsed.path == "/register/input"
        and params.keys() <= {"key", "id", "mode"}
        and len(keys) == 1
        and bool(re.fullmatch(r"[\w-]+", keys[0], re.ASCII))
    )


def extract_confirmation_urls(
    plain_bodies: Sequence[str],
    html_bodies: Sequence[str],
    filter_config: Optional[NinjaMailFilterConfig] = None,
) -> List[str]:
    """
    Trích các URL xác nhận hợp lệ từ cả text/plain và text/html,
    gộp URL trùng lặp và giữ nguyên thứ tự xuất hiện.
    """
    candidates: List[str] = []

    for body in plain_bodies:
        if not body:
            continue
        for match in _PLAIN_URL_REGEX.findall(body):
            cleaned = _clean_plain_url(match)
            if cleaned:
                candidates.append(cleaned)
        # Ninja's plain-text invitation can wrap the confirmation hash onto another line.
        candidates.extend("".join(match.split()) for match in _WRAPPED_HASH_URL.findall(body))

    for html_body in html_bodies:
        if not html_body:
            continue
        extractor = _HTMLLinkAndTextExtractor()
        try:
            extractor.feed(html_body)
            extractor.close()
        except Exception:
            pass
        candidates.extend(extractor.hrefs)
        for match in _PLAIN_URL_REGEX.findall(extractor.text):
            cleaned = _clean_plain_url(match)
            if cleaned:
                candidates.append(cleaned)

    unique: List[str] = []
    seen_keys = set()
    for url in candidates:
        if not is_confirmation_url(url, filter_config):
            continue
        key = _canonical_url_key(url)
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(url.strip())
    return unique


def _extract_body_emails(plain_bodies: Sequence[str], html_bodies: Sequence[str]) -> Tuple[str, ...]:
    texts = list(plain_bodies)
    for html_body in html_bodies:
        if not html_body:
            continue
        extractor = _HTMLLinkAndTextExtractor()
        try:
            extractor.feed(html_body)
            extractor.close()
        except Exception:
            pass
        texts.append(extractor.text)

    found: List[str] = []
    seen = set()
    for text in texts:
        for match in _EMAIL_IN_TEXT_REGEX.findall(text):
            lowered = match.lower()
            if lowered not in seen:
                seen.add(lowered)
                found.append(lowered)
    return tuple(found)


def decode_email_message(
    raw_bytes: bytes,
    uid: int = 0,
    internal_date_utc: Optional[datetime] = None,
    filter_config: Optional[NinjaMailFilterConfig] = None,
) -> ParsedNinjaEmail:
    """Giải mã thông điệp RFC822/MIME bằng thư viện email của Python."""
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw_bytes)
    plain_bodies: List[str] = []
    html_bodies: List[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment":
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype == "text/plain":
            text = _decode_part_text(part)
            if text:
                plain_bodies.append(text)
        elif ctype == "text/html":
            text = _decode_part_text(part)
            if text:
                html_bodies.append(text)

    return _parsed_message(msg, uid, internal_date_utc, plain_bodies, html_bodies, filter_config)


def _parsed_message(msg, uid, internal_date_utc, plain_bodies, html_bodies, filter_config):
    subject = unicodedata.normalize("NFC", _decode_header_value(msg, "Subject")).strip()
    from_addrs = _extract_header_addresses(msg, ("From",))
    to_addrs = _extract_header_addresses(msg, ("To",))
    other_recipients = _extract_header_addresses(
        msg, ("Delivered-To", "X-Original-To", "Envelope-To", "Cc")
    )

    confirmation_urls = tuple(extract_confirmation_urls(plain_bodies, html_bodies, filter_config))
    body_emails = _extract_body_emails(plain_bodies, html_bodies)

    return ParsedNinjaEmail(
        uid=uid,
        internal_date_utc=internal_date_utc,
        subject=subject,
        from_addresses=from_addrs,
        to_addresses=to_addrs,
        other_recipient_addresses=other_recipients,
        plain_bodies=tuple(plain_bodies),
        html_bodies=tuple(html_bodies),
        confirmation_urls=confirmation_urls,
        body_mentioned_emails=body_emails,
    )


def _sender_matches(from_addresses: Sequence[str], filter_config: NinjaMailFilterConfig) -> bool:
    allowed_exact = {a.lower() for a in filter_config.allowed_sender_addresses}
    allowed_domains = tuple(d.lower().lstrip(".") for d in filter_config.allowed_sender_domains)
    for addr in from_addresses:
        lowered = addr.lower()
        if lowered in allowed_exact:
            return True
        _, _, domain = lowered.partition("@")
        if any(domain == d or domain.endswith("." + d) for d in allowed_domains):
            return True
    return False


def _subject_matches(subject: str, filter_config: NinjaMailFilterConfig) -> bool:
    expected = unicodedata.normalize("NFC", filter_config.subject_keyword).strip()
    return bool(expected) and expected == unicodedata.normalize("NFC", subject).strip()


def _passes_uid_and_time(
    parsed: ParsedNinjaEmail,
    req: PendingMailRequest,
    clock_skew_seconds: float,
) -> bool:
    if parsed.uid < req.baseline.uidnext:
        return False
    if parsed.internal_date_utc is not None:
        cutoff = req.baseline.captured_at_utc - timedelta(seconds=clock_skew_seconds)
        if parsed.internal_date_utc < cutoff:
            return False
    return True


def evaluate_email_for_requests(
    parsed: ParsedNinjaEmail,
    requests: Sequence[PendingMailRequest],
    filter_config: Optional[NinjaMailFilterConfig] = None,
    clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
) -> List[MailMatchOutcome]:
    """
    Đối chiếu một thư đã giải mã với danh sách yêu cầu đang chờ.
    Trả về danh sách MailMatchOutcome (rỗng nếu thư không thuộc ứng viên nào).
    """
    cfg = filter_config or NinjaMailFilterConfig()
    if not requests:
        return []
    if not _subject_matches(parsed.subject, cfg):
        return []
    if not _sender_matches(parsed.from_addresses, cfg):
        return []

    req_by_email: Dict[str, PendingMailRequest] = {
        req.email.strip().lower(): req for req in requests
    }
    eligible_ids = {
        req.request_id
        for req in requests
        if _passes_uid_and_time(parsed, req, clock_skew_seconds)
    }
    if not eligible_ids:
        return []

    to_reqs = {
        req_by_email[addr].request_id
        for addr in parsed.to_addresses
        if addr in req_by_email and req_by_email[addr].request_id in eligible_ids
    }
    other_reqs = {
        req_by_email[addr].request_id
        for addr in parsed.other_recipient_addresses
        if addr in req_by_email and req_by_email[addr].request_id in eligible_ids
    }
    body_reqs = {
        req_by_email[addr].request_id
        for addr in parsed.body_mentioned_emails
        if addr in req_by_email and req_by_email[addr].request_id in eligible_ids
    }

    all_matched_ids = to_reqs | other_reqs | body_reqs
    if not all_matched_ids:
        return []

    # Phát hiện xung đột giữa các trường người nhận hoặc trong nội dung thư
    has_conflict = (
        len(to_reqs) > 1
        or len(all_matched_ids) > 1
        or not to_reqs
        or (bool(to_reqs) and bool(other_reqs - to_reqs))
        or (bool(to_reqs) and bool(body_reqs - to_reqs))
    )
    if has_conflict:
        return [
            MailMatchOutcome(
                request_id=rid,
                status=STATUS_CHECK_MAIL,
                reason="Các trường người nhận trong thư chỉ về các yêu cầu khác nhau.",
                uid=parsed.uid,
            )
            for rid in sorted(all_matched_ids)
        ]

    # Chỉ có duy nhất một yêu cầu khớp rõ ràng qua To
    target_id = next(iter(to_reqs))
    if not parsed.plain_bodies and not parsed.html_bodies:
        return [
            MailMatchOutcome(
                request_id=target_id,
                status=STATUS_CHECK_MAIL,
                reason="Thư đăng ký không có nội dung văn bản hoặc HTML.",
                uid=parsed.uid,
            )
        ]

    urls = parsed.confirmation_urls
    if len(urls) == 1:
        return [
            MailMatchOutcome(
                request_id=target_id,
                status=STATUS_CONFIRMED,
                confirmation_url=urls[0],
                reason="Đã trích xuất đúng 1 link xác nhận hợp lệ.",
                uid=parsed.uid,
            )
        ]
    if len(urls) > 1:
        return [
            MailMatchOutcome(
                request_id=target_id,
                status=STATUS_CHECK_MAIL,
                reason=f"Tìm thấy {len(urls)} link xác nhận khác nhau không thể phân biệt.",
                uid=parsed.uid,
            )
        ]
    return [
        MailMatchOutcome(
            request_id=target_id,
            status=STATUS_CHECK_MAIL,
            reason="Không tìm thấy link xác nhận hợp lệ trong nội dung thư đăng ký.",
            uid=parsed.uid,
        )
    ]


def _is_attachment(disposition):
    if not disposition:
        return False
    params = disposition[1] if len(disposition) > 1 else None
    return disposition[0].upper() == b"ATTACHMENT" or any(
        key.upper().startswith(b"FILENAME") for key in (params or ())[::2]
    )


def _text_sections(structure, number=""):
    """Select inline text leaves only. BODY[TEXT] includes attachments on multipart mail."""
    if not structure:
        return
    if isinstance(structure[0], list):
        disposition = structure[3] if len(structure) > 3 else None
        if _is_attachment(disposition):
            return
        for index, child in enumerate(structure[0], 1):
            child_number = f"{number}.{index}" if number else str(index)
            yield from _text_sections(child, child_number)
        return

    if len(structure) < 7 or structure[0].upper() != b"TEXT":
        return
    subtype = structure[1].upper()
    params = structure[2] or ()
    attributes = {key.upper(): val for key, val in zip(params[::2], params[1::2])}
    disposition = structure[9] if len(structure) > 9 else None
    if subtype not in (b"PLAIN", b"HTML") or any(key.startswith(b"NAME") for key in attributes):
        return
    if _is_attachment(disposition):
        return
    yield number or "TEXT", subtype, attributes.get(b"CHARSET", b"utf-8"), structure[5]


class GmailRegistrationReader:
    """
    Một bộ đọc IMAP duy nhất phục vụ kiểm tra kết nối, đánh dấu mốc hộp thư
    và đọc thư xác nhận cho phiên đăng ký.
    """

    def __init__(
        self,
        config: ImapConfig,
        filter_config: Optional[NinjaMailFilterConfig] = None,
        imap_factory: Optional[Callable[[str, int, float], Any]] = None,
    ) -> None:
        self.config = config
        self.filter_config = filter_config or NinjaMailFilterConfig()
        self._imap_factory = imap_factory or _default_imap_factory
        self._stop_event = threading.Event()
        self._conn_lock = threading.Lock()
        self._conn: Any = None
        self._mailbox_state: Optional[Tuple[int, int]] = None
        self._idle_disabled = False
        self._claimed_uids: set[int] = set()
        self._claimed_urls: set[str] = set()
        self._parsed_cache: Dict[int, Optional[ParsedNinjaEmail]] = {}

    def reset_stop(self) -> None:
        self._stop_event.clear()

    def request_stop(self) -> None:
        """Đánh thức polling; IDLE kiểm tra tín hiệu dừng trong mỗi lần chờ socket."""
        self._stop_event.set()

    def close(self, *, graceful: bool = True) -> None:
        with self._conn_lock:
            conn, self._conn = self._conn, None
        if conn is None:
            return
        if graceful:
            self._safe_close_conn(conn)
        else:
            try:
                conn.shutdown()
            except OSError:
                pass

    @staticmethod
    def _safe_close_conn(conn) -> None:
        try:
            conn.logout()
        except Exception:
            try:
                conn.shutdown()
            except OSError:
                pass

    def _connect_and_select(self) -> Tuple[Any, int, int]:
        if self._stop_event.is_set():
            raise ImapStoppedError("Bộ đọc IMAP đã bị dừng.")
        token = self.config.oauth_token() if self.config.oauth_token else None
        try:
            conn = self._imap_factory(self.config.host, self.config.port, self.config.timeout)
        except (OSError, EOFError, imaplib.IMAP4.error) as exc:
            raise ImapConnectionError("Không thể kết nối máy chủ IMAP qua TLS.") from exc
        authenticated = False
        try:
            if token is not None:
                conn.oauth2_login(self.config.email, token)
            else:
                conn.login(self.config.email, self.config.app_password)
            authenticated = True
            state = conn.select_folder(self.config.mailbox, readonly=True)
            if not state.get(b"UIDVALIDITY") or not state.get(b"UIDNEXT"):
                state = conn.folder_status(self.config.mailbox, ["UIDVALIDITY", "UIDNEXT"])
            uidvalidity, uidnext = self._mailbox_ids(state)
        except Exception as exc:
            self._safe_close_conn(conn)
            if isinstance(exc, (OSError, EOFError, imaplib.IMAP4.abort)):
                raise ImapConnectionError("Kết nối Gmail IMAP bị gián đoạn.") from exc
            if isinstance(exc, ImapMailboxError):
                raise
            if authenticated:
                raise ImapMailboxError("Không mở được INBOX ở chế độ chỉ đọc.") from None
            if token is None:
                raise ImapAuthError(
                    "Đăng nhập IMAP thất bại. Kiểm tra Gmail và App Password 16 ký tự (cần bật xác minh 2 bước)."
                ) from None
            raise ImapAuthError("Đăng nhập IMAP thất bại. Kiểm tra tài khoản và phương thức đăng nhập.") from None
        if self._mailbox_state and self._mailbox_state[0] != uidvalidity:
            self._parsed_cache.clear()
            self._claimed_uids.clear()
        self._mailbox_state = uidvalidity, uidnext
        with self._conn_lock:
            self._conn = conn
        return conn, uidvalidity, uidnext

    @staticmethod
    def _mailbox_ids(state):
        if not state.get(b"UIDVALIDITY") or not state.get(b"UIDNEXT"):
            raise ImapMailboxError("Máy chủ không trả về UIDVALIDITY và UIDNEXT.")
        return state[b"UIDVALIDITY"], state[b"UIDNEXT"]

    def verify_connection(self) -> InboxBaseline:
        """
        Kiểm tra kết nối IMAP TLS, đăng nhập và mở INBOX chỉ đọc rồi đóng kết nối.
        Dùng cho nút Kiểm tra kết nối IMAP.
        """
        self.reset_stop()
        conn, uidvalidity, uidnext = self._connect_and_select()
        try:
            return InboxBaseline(
                uidvalidity=uidvalidity,
                uidnext=uidnext,
                captured_at_utc=datetime.now(timezone.utc),
                request_email=self.config.email,
                mailbox=self.config.mailbox,
            )
        finally:
            self.close()

    def ensure_connected(self) -> Tuple[int, int]:
        """Đảm bảo kết nối IMAP sẵn sàng và INBOX mở chỉ đọc; trả về (uidvalidity, uidnext)."""
        if self._conn is not None and self._mailbox_state:
            return self._mailbox_state
        _conn, uidvalidity, uidnext = self._connect_and_select()
        return uidvalidity, uidnext

    def mark_baseline(
        self,
        request_email: str,
        captured_at_utc: Optional[datetime] = None,
    ) -> InboxBaseline:
        self.ensure_connected()
        try:
            state = self._conn.folder_status(self.config.mailbox, ["UIDVALIDITY", "UIDNEXT"])
            uidvalidity, uidnext = self._mailbox_ids(state)
            if uidvalidity != self._mailbox_state[0]:
                self.close()
                uidvalidity, uidnext = self.ensure_connected()
        except (OSError, EOFError, imaplib.IMAP4.abort):
            self.close()
            uidvalidity, uidnext = self.ensure_connected()
        baseline_utc = captured_at_utc
        if baseline_utc is None:
            baseline_utc = datetime.now(timezone.utc)
        elif baseline_utc.tzinfo is None:
            baseline_utc = baseline_utc.replace(tzinfo=timezone.utc)
        else:
            baseline_utc = baseline_utc.astimezone(timezone.utc)
        return InboxBaseline(
            uidvalidity=uidvalidity,
            uidnext=uidnext,
            captured_at_utc=baseline_utc,
            request_email=request_email,
            mailbox=self.config.mailbox,
        )

    @staticmethod
    def _find_fetch_bytes(data: Dict[Any, Any], section: str) -> Optional[bytes]:
        exact = data.get(f"BODY[{section}]".encode("ascii"))
        if isinstance(exact, bytes):
            return exact
        target_upper = f"BODY[{section}]".upper().encode("ascii")
        prefix_upper = b"BODY[HEADER.FIELDS" if section.upper().startswith("HEADER.FIELDS") else target_upper
        for key, value in data.items():
            if not isinstance(key, bytes) or not isinstance(value, bytes):
                continue
            key_upper = key.upper()
            if key_upper == target_upper or (
                prefix_upper == b"BODY[HEADER.FIELDS" and key_upper.startswith(prefix_upper)
            ):
                return value
        return None

    def _fetch_candidate(self, conn, uid):
        return self._fetch_candidate_entry(conn, uid)[0]

    def _fetch_candidate_entry(self, conn, uid, on_log: Optional[Callable[[str], None]] = None):
        fields = ["INTERNALDATE", "BODYSTRUCTURE", f"BODY.PEEK[{HEADER_SECTION}]"]
        raw_fetch = conn.fetch([uid], fields)
        metadata = raw_fetch.get(uid, {})
        headers = self._find_fetch_bytes(metadata, HEADER_SECTION)
        if not headers:
            if on_log is not None:
                keys = [repr(k) for k in metadata.keys()][:8]
                on_log(f"[IMAP] UID {uid}: thiếu headers trong FETCH (keys={keys}).")
            return None, False
        msg = email.parser.BytesHeaderParser(policy=email.policy.default).parsebytes(headers)
        subject_val = _decode_header_value(msg, "Subject")
        subject_ok = _subject_matches(subject_val, self.filter_config)
        from_addrs = _extract_header_addresses(msg, ("From",))
        sender_ok = _sender_matches(from_addrs, self.filter_config)
        if not subject_ok or not sender_ok:
            if on_log is not None:
                on_log(
                    f"[IMAP] UID {uid}: bỏ qua (subject_ok={subject_ok}, sender_ok={sender_ok}, "
                    f"from_domains={[a.partition('@')[2] for a in from_addrs]})."
                )
            return None, True
        bodystructure = metadata.get(b"BODYSTRUCTURE")
        sections = list(_text_sections(bodystructure))
        if not sections and on_log is not None:
            on_log(f"[IMAP] UID {uid}: BODYSTRUCTURE không có phần text inline ({type(bodystructure).__name__}).")
        plain, html = [], []
        if sections:
            fields = [f"BODY.PEEK[{number}]" for number, *_ in sections]
            data = conn.fetch([uid], fields).get(uid, {})
            for number, subtype, charset, transfer in sections:
                raw = self._find_fetch_bytes(data, number)
                if not isinstance(raw, bytes):
                    if on_log is not None:
                        on_log(f"[IMAP] UID {uid}: không đọc được phần BODY[{number}] (keys={list(data.keys())[:6]}).")
                    continue
                charset_str = (
                    charset.decode("ascii", errors="replace")
                    if isinstance(charset, bytes)
                    else str(charset or "utf-8")
                )
                transfer_str = (
                    transfer.decode("ascii", errors="replace")
                    if isinstance(transfer, bytes)
                    else str(transfer or "7bit")
                )
                part = email.message.Message()
                part.set_payload(raw)
                part.set_type("text/" + subtype.decode("ascii").lower())
                part.set_param("charset", charset_str)
                part["Content-Transfer-Encoding"] = transfer_str
                (plain if subtype == b"PLAIN" else html).append(_decode_part_text(part))
        internal_dt = metadata.get(b"INTERNALDATE")
        if isinstance(internal_dt, datetime) and internal_dt.tzinfo is None:
            internal_dt = internal_dt.replace(tzinfo=timezone.utc)
        parsed = _parsed_message(msg, uid, internal_dt, plain, html, self.filter_config)
        if on_log is not None and not parsed.confirmation_urls:
            on_log(
                f"[IMAP] UID {uid}: thư Ninja có {len(plain)} plain / {len(html)} html "
                f"nhưng trích được 0 link hợp lệ."
            )
        return parsed, True

    def poll_once(
        self,
        pending_requests: Sequence[PendingMailRequest],
        on_log: Optional[Callable[[str], None]] = None,
    ) -> List[MailMatchOutcome]:
        """
        Thực hiện một lượt đọc INBOX từ mốc UIDNEXT thấp nhất trong danh sách yêu cầu đang chờ.
        Kiểm tra UIDVALIDITY, lọc lại UID >= UIDNEXT và chỉ đọc phần văn bản cần thiết.
        """
        if self._stop_event.is_set():
            raise ImapStoppedError("Bộ đọc IMAP đã bị dừng.")
        if not pending_requests:
            return []

        current_uidvalidity, _ = self.ensure_connected()
        outcomes: List[MailMatchOutcome] = []
        valid_requests: List[PendingMailRequest] = []

        for req in pending_requests:
            if req.baseline.uidvalidity != current_uidvalidity:
                outcomes.append(
                    MailMatchOutcome(
                        request_id=req.request_id,
                        status=STATUS_CHECK_MAIL,
                        reason="UIDVALIDITY của INBOX đã thay đổi so với mốc đã lưu.",
                    )
                )
            else:
                valid_requests.append(req)

        if not valid_requests:
            return outcomes

        min_uid = min(req.baseline.uidnext for req in valid_requests)
        with self._conn_lock:
            conn = self._conn
        if conn is None:
            raise ImapConnectionError("Kết nối IMAP chưa sẵn sàng.")

        try:
            if hasattr(conn, "noop"):
                conn.noop()
            raw_search = conn.search(["UID", f"{min_uid}:*"])
            candidate_uids = sorted(uid for uid in raw_search if uid >= min_uid)
        except (imaplib.IMAP4.abort, OSError, EOFError) as exc:
            self.close()
            raise ImapConnectionError("Mất kết nối khi tìm thư mới.") from exc

        if on_log is not None and candidate_uids:
            unclaimed = [u for u in candidate_uids if u not in self._claimed_uids]
            if unclaimed:
                on_log(f"[IMAP] Quét INBOX (UID >= {min_uid}): kiểm tra {len(unclaimed)} thư mới {unclaimed[:10]}.")

        provisional_outcomes: Dict[str, MailMatchOutcome] = {}

        for uid in candidate_uids:
            if self._stop_event.is_set():
                raise ImapStoppedError("Bộ đọc IMAP đã bị dừng.")
            if uid in self._claimed_uids:
                continue

            if uid not in self._parsed_cache:
                try:
                    candidate, cacheable = self._fetch_candidate_entry(conn, uid, on_log=on_log)
                    if cacheable:
                        self._parsed_cache[uid] = candidate
                except (imaplib.IMAP4.abort, OSError, EOFError) as exc:
                    self.close()
                    raise ImapConnectionError("Mất kết nối khi đọc thư.") from exc
                parsed = candidate
            else:
                parsed = self._parsed_cache[uid]
            if parsed is None:
                continue
            matched = evaluate_email_for_requests(
                parsed, valid_requests, self.filter_config, self.config.clock_skew_seconds
            )
            if not matched:
                if on_log is not None:
                    req_map = {req.email.strip().lower(): req for req in valid_requests}
                    time_ok = any(_passes_uid_and_time(parsed, req, self.config.clock_skew_seconds) for req in valid_requests)
                    to_hits = sum(1 for a in parsed.to_addresses if a in req_map)
                    on_log(
                        f"[IMAP] UID {uid}: không khớp yêu cầu chờ "
                        f"(time_ok={time_ok}, to_hits={to_hits}, to_count={len(parsed.to_addresses)}, "
                        f"dt={parsed.internal_date_utc})."
                    )
                continue

            # Kiểm tra xung đột nếu nhiều thư khớp cùng 1 yêu cầu trong cùng lượt hoặc link đã cấp trước đó
            for item in matched:
                if item.status == STATUS_CONFIRMED and item.confirmation_url:
                    url_key = _canonical_url_key(item.confirmation_url)
                    if url_key in self._claimed_urls:
                        item = MailMatchOutcome(
                            request_id=item.request_id,
                            status=STATUS_CHECK_MAIL,
                            reason="Link xác nhận đã được cấp cho một yêu cầu khác.",
                            uid=item.uid,
                        )
                prev = provisional_outcomes.get(item.request_id)
                if prev is not None:
                    if (
                        prev.status == STATUS_CONFIRMED
                        and item.status == STATUS_CONFIRMED
                        and prev.confirmation_url
                        and item.confirmation_url
                        and _canonical_url_key(prev.confirmation_url)
                        == _canonical_url_key(item.confirmation_url)
                    ):
                        continue
                    provisional_outcomes[item.request_id] = MailMatchOutcome(
                        request_id=item.request_id,
                        status=STATUS_CHECK_MAIL,
                        reason="Nhận được nhiều thư đăng ký khác nhau cho cùng một yêu cầu.",
                        uid=item.uid,
                    )
                else:
                    provisional_outcomes[item.request_id] = item

        url_counts: Dict[str, int] = {}
        for item in provisional_outcomes.values():
            if item.status == STATUS_CONFIRMED and item.confirmation_url:
                key = _canonical_url_key(item.confirmation_url)
                url_counts[key] = url_counts.get(key, 0) + 1

        for rid, final_item in provisional_outcomes.items():
            if final_item.status == STATUS_CONFIRMED and final_item.confirmation_url:
                url_key = _canonical_url_key(final_item.confirmation_url)
                if url_counts[url_key] > 1:
                    final_item = MailMatchOutcome(
                        request_id=rid,
                        status=STATUS_CHECK_MAIL,
                        reason="Cùng một link xác nhận khớp với nhiều yêu cầu.",
                        uid=final_item.uid,
                    )
                else:
                    self._claimed_urls.add(url_key)
                    if final_item.uid is not None:
                        self._claimed_uids.add(final_item.uid)
            outcomes.append(final_item)

        return outcomes

    @staticmethod
    def _pop_untagged_arrival(conn: Any) -> bool:
        imap_obj = getattr(conn, "_imap", None)
        untagged = getattr(imap_obj, "untagged_responses", None)
        if not isinstance(untagged, dict):
            return False
        found = False
        for key in ("EXISTS", "RECENT"):
            if untagged.pop(key, None):
                found = True
        return found

    @staticmethod
    def _has_buffered_imap_data(conn: Any) -> bool:
        imap_obj = getattr(conn, "_imap", None)
        sock_obj = getattr(imap_obj, "sock", None)
        if sock_obj is not None and hasattr(sock_obj, "pending"):
            try:
                if sock_obj.pending() > 0:
                    return True
            except Exception:
                pass
        return False

    def _wait_for_change(self, deadline, clock):
        conn = self._conn
        try:
            if not self._idle_disabled and conn.has_capability("IDLE"):
                self._pop_untagged_arrival(conn)
                conn.idle()
                try:
                    if self._pop_untagged_arrival(conn) or self._has_buffered_imap_data(conn):
                        return True
                    idle_window = max(DEFAULT_POLL_INTERVAL, self.config.poll_interval)
                    renew_at = min(deadline, clock() + min(25 * 60, idle_window))
                    while not self._stop_event.is_set():
                        remaining = renew_at - clock()
                        if remaining <= 0:
                            break
                        if self._has_buffered_imap_data(conn):
                            return True
                        # Local socket waits keep Stop responsive without sending commands.
                        for response in conn.idle_check(timeout=min(1.0, remaining)):
                            if b"BYE" in response:
                                raise imaplib.IMAP4.abort("BYE")
                            if b"EXISTS" in response or b"RECENT" in response:
                                return True
                finally:
                    conn.idle_done()
                return not self._stop_event.is_set()
        except (OSError, EOFError, imaplib.IMAP4.abort):
            self.close()
            raise ImapConnectionError("Kết nối IMAP IDLE bị ngắt.") from None
        except imaplib.IMAP4.error:
            # An unsolicited EXISTS can arrive before IDLE's continuation.
            # Reconnect and scan once before falling back to periodic polling.
            self._idle_disabled = True
            self.close(graceful=False)
            return not self._stop_event.is_set()

        interval = max(DEFAULT_POLL_INTERVAL, self.config.poll_interval)
        remaining = deadline - clock()
        stopped = self._stop_event.wait(max(0, min(interval, remaining)))
        return not stopped and remaining >= interval

    def wait_for_requests(
        self,
        get_pending_requests: Callable[[], Sequence[PendingMailRequest]],
        timeout_seconds: float,
        on_outcome: Optional[Callable[[MailMatchOutcome], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> List[MailMatchOutcome]:
        """
        Chờ và đối chiếu thư cho các yêu cầu đang chờ tới khi xong hết, hết giờ hoặc bị dừng.
        """
        deadline = clock() + max(0.0, timeout_seconds)
        collected: List[MailMatchOutcome] = []

        while not self._stop_event.is_set():
            pending = list(get_pending_requests())
            if not pending:
                break

            try:
                outcomes = self.poll_once(pending, on_log=on_log)
                for outcome in outcomes:
                    collected.append(outcome)
                    if on_outcome is not None:
                        on_outcome(outcome)
                if not get_pending_requests() or clock() >= deadline:
                    break
                if not self._wait_for_change(deadline, clock):
                    break
            except ImapStoppedError:
                break
            except ImapConnectionError as exc:
                if on_log is not None:
                    on_log(f"[IMAP] {exc} Sẽ thử kết nối lại.")
                remaining = deadline - clock()
                if remaining <= 0:
                    break
                if self._stop_event.wait(min(self.config.reconnect_delay, remaining)):
                    break
                continue

        return collected

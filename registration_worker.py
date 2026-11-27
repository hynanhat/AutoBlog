"""
Bộ điều phối gửi đăng ký tạm, nhận link qua Gmail IMAP và bàn giao form tại CAPTCHA.
"""

from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import email.utils
from pathlib import Path
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    async_playwright,
)

from gmail_fetcher import (
    GmailRegistrationReader,
    ImapConfig,
    InboxBaseline,
    MailMatchOutcome,
    NinjaMailFilterConfig,
    PendingMailRequest,
    STATUS_CHECK_MAIL,
    STATUS_CONFIRMED,
    is_confirmation_url,
)
from hiragana_hook import HiraganaHook, OCRHiraganaHook
from ocr_client import (
    HiraganaOCRClient,
    OCRConfig,
    ensure_ocr_server_running,
    resolve_default_ocr_url,
)
from parsers import GmailLine
from registration_form import (
    ConfirmationFormControls,
    RegistrationFormConfig,
    RegistrationFormError,
    STATUS_FORM_EXPIRED,
    STATUS_FORM_INVALID,
    capture_captcha_image,
    fill_confirmation_form,
    inspect_registration_submission_state,
    locate_confirm_step_button,
    prepare_confirmation_form,
    refresh_captcha_image,
    validate_registration_password,
    visible_click_position,
)

DEFAULT_REGISTER_URL = "https://www.ninja.co.jp/register/"
DEFAULT_WAIT_TIMEOUT_SECONDS = 180.0
DEFAULT_SUBMIT_TIMEOUT_SECONDS = 20.0
DEFAULT_OCR_MAX_ATTEMPTS = 5
DEFAULT_OCR_TIMEOUT_SECONDS = 60.0

STATUS_PENDING = "Đang chờ"
STATUS_SENDING = "Đang gửi đăng ký"
STATUS_WAITING_MAIL = "Chờ thư xác nhận"
STATUS_EMAIL_EXISTS = "Email đã tồn tại"
STATUS_SEND_UNKNOWN = "Chưa xác định kết quả gửi"
STATUS_TIMEOUT = "Hết giờ chờ thư"
STATUS_CANCELLED = "Đã hủy"
STATUS_FAILED = "Lỗi gửi đăng ký"
STATUS_NOT_SUBMITTED = "Chưa gửi đăng ký"
STATUS_FORM_PREPARING = "Đang mở và điền form"
STATUS_OCR_SOLVING = "Đang giải OCR CAPTCHA"
STATUS_REGISTERED = "Hoàn tất đăng ký"
STATUS_FORM_READY = "Form sẵn sàng – chờ tiếp nhận"
STATUS_FORM_HANDOFF = "Đã bàn giao form"
STATUS_TAB_CLOSED = "Tab đã đóng"
STATUS_FORM_FAILED = "Lỗi mở hoặc điền form"

RETRYABLE_STATUSES = (STATUS_TIMEOUT, STATUS_SEND_UNKNOWN)
RUNNING_STATUSES = (STATUS_PENDING, STATUS_SENDING, STATUS_WAITING_MAIL)

REGISTRATION_SELECTORS = {
    "form": "#register_form",
    "email_input": 'input[name="email"]',
    "submit_button": (
        'input[type="image"][alt="この内容を送信"], '
        'input[type="submit"][value="この内容を送信"], '
        'button[type="submit"]:text-is("この内容を送信")'
    ),
    "error_container": (
        ".errorArea, .error, .errortext, .caution, .alert, .alert-danger, .alert-error, font[color=red]"
    ),
}

_ALREADY_EXISTS_PATTERNS = (
    "すでに登録",
    "既に登録",
    "登録済み",
    "既にご登録",
    "すでに利用",
    "既に利用",
    "登録されています",
    "登録されております",
    "使用されています",
    "使われています",
    "already exists",
    "already registered",
    "đã tồn tại",
    "đã được đăng ký",
)

_ACCEPTED_PATTERNS = (
    "仮登録が完了",
    "仮登録を受け付け",
    "仮登録完了",
    "メールを送信しました",
    "メールをお送りしました",
    "確認メールを送信いたしました",
    "案内メールを送信いたしました",
    "送信完了",
    "temporary registration complete",
    "confirmation email sent",
    "đã tiếp nhận",
    "đã gửi thư xác nhận",
)

RegistrationStatusCallback = Callable[[int, str, str, str], None]


@dataclass
class RegistrationConfig:
    """Cấu hình cho một phiên gửi đăng ký và lấy link xác nhận."""

    entries: List[GmailLine]
    imap_config: ImapConfig
    wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS
    register_url: str = DEFAULT_REGISTER_URL
    filter_config: NinjaMailFilterConfig = field(default_factory=NinjaMailFilterConfig)
    headless: bool = False
    submit_timeout_seconds: float = DEFAULT_SUBMIT_TIMEOUT_SECONDS
    form: Optional[RegistrationFormConfig] = None
    max_sessions: int = 5
    use_ocr: bool = False
    ocr_url: Optional[str] = None
    ocr_max_attempts: int = DEFAULT_OCR_MAX_ATTEMPTS
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS


@dataclass
class RegistrationRecord:
    """Trạng thái đầy đủ của một yêu cầu đăng ký giữ trong bộ nhớ worker."""

    stt: int
    line_no: int
    request_id: str
    email: str
    status: str = STATUS_PENDING
    updated_at: str = ""
    baseline: Optional[InboxBaseline] = None
    send_attempts: int = 0
    submitted_at_utc: Optional[datetime] = None
    mail_deadline: float = 0.0
    confirmation_url: Optional[str] = field(default=None, repr=False)
    browser_context: Optional[BrowserContext] = field(default=None, repr=False)
    page: Optional[Page] = field(default=None, repr=False)
    closing_page: bool = field(default=False, repr=False)
    note: str = ""


def _server_clock_offset(response: Any) -> Optional[timedelta]:
    if response is None:
        return None
    try:
        date_header = response.headers.get("date")
    except Exception:
        return None
    if not date_header:
        return None
    try:
        server_dt = email.utils.parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if server_dt.tzinfo is None:
        server_dt = server_dt.replace(tzinfo=timezone.utc)
    else:
        server_dt = server_dt.astimezone(timezone.utc)
    return server_dt - datetime.now(timezone.utc)


def _now_display() -> str:
    return datetime.now().strftime("%H:%M:%S")


def classify_registration_response(page_text: str, error_text: str) -> Tuple[str, str]:
    """
    Phân loại phản hồi hiển thị trên trang sau khi gửi form đăng ký tạm.
    Trả về (status, sanitized_reason).
    """
    err_clean = " ".join((error_text or "").split())
    body_clean = " ".join((page_text or "").split())
    combined_lower = f"{err_clean} {body_clean}".lower()

    if err_clean:
        if any(pat.lower() in err_clean.lower() for pat in _ALREADY_EXISTS_PATTERNS):
            return STATUS_EMAIL_EXISTS, "Trang đăng ký báo địa chỉ email đã tồn tại."
        return STATUS_FAILED, "Trang đăng ký báo lỗi. Xem thông báo trên trang Ninja."

    if any(pat.lower() in combined_lower for pat in _ALREADY_EXISTS_PATTERNS):
        return STATUS_EMAIL_EXISTS, "Trang đăng ký báo địa chỉ email đã tồn tại."

    if any(pat.lower() in combined_lower for pat in _ACCEPTED_PATTERNS):
        return STATUS_WAITING_MAIL, "Ninja đã tiếp nhận yêu cầu đăng ký tạm."

    return STATUS_SEND_UNKNOWN, "Không xác nhận được thông báo tiếp nhận sau khi gửi."


class RegistrationWorker:
    """
    Điều phối gửi đăng ký tạm một lần và đọc link xác nhận qua Gmail IMAP.
    Giữ lại Browser/BrowserContext/Page và link xác nhận trong bộ nhớ cho giai đoạn sau.
    """

    def __init__(
        self,
        config: RegistrationConfig,
        on_status: RegistrationStatusCallback,
        on_log: Callable[[str], None],
        imap_factory: Optional[Callable[[str, int, float], Any]] = None,
        browser: Optional[Browser] = None,
        hiragana_hook: Optional[HiraganaHook] = None,
    ) -> None:
        self.config = config
        self.on_status = on_status
        self.on_log = on_log
        self.reader = GmailRegistrationReader(
            config.imap_config,
            filter_config=config.filter_config,
            imap_factory=imap_factory,
        )
        self._external_browser = browser
        self._browser: Optional[Browser] = browser
        self._playwright: Optional[Playwright] = None
        self._external_hiragana_hook = hiragana_hook
        self._hiragana_hook: Optional[HiraganaHook] = hiragana_hook
        self._ocr_client: Optional[HiraganaOCRClient] = None
        self._stop_requested = False
        self._state_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tab_changed = asyncio.Event()
        self._mail_wait_active = False
        self._queued_rows = False
        self._discard_forms = False
        self._baseline_lock = asyncio.Lock()

        self.records: List[RegistrationRecord] = [
            RegistrationRecord(
                stt=idx,
                line_no=entry.line_no,
                request_id=f"reg-{idx}-{uuid4().hex[:8]}",
                email=entry.email,
                status=STATUS_PENDING,
                updated_at=_now_display(),
            )
            for idx, entry in enumerate(config.entries, start=1)
        ]
        self._records_by_id: Dict[str, RegistrationRecord] = {
            rec.request_id: rec for rec in self.records
        }

    @staticmethod
    def can_send(record: RegistrationRecord) -> bool:
        """Chỉ cho phép gửi đăng ký khi chưa gửi lần nào và đang ở trạng thái chờ."""
        return record.send_attempts == 0 and record.status == STATUS_PENDING

    @staticmethod
    def can_retry_mail_check(record: RegistrationRecord) -> bool:
        """Chỉ cho phép đọc lại thư (không gửi lại) ở các trạng thái cho phép."""
        return (
            record.status in RETRYABLE_STATUSES
            and record.baseline is not None
        )

    def _emit_status(self, rec: RegistrationRecord, status: str, note: str = "") -> None:
        with self._state_lock:
            rec.status = status
            rec.updated_at = _now_display()
            if note:
                rec.note = note
            snapshot = (rec.stt, rec.email, rec.status, rec.updated_at)
        self.on_status(*snapshot)

    def request_stop(self) -> None:
        """Dừng worker đăng ký, đánh thức bộ đọc IMAP và ngắt thao tác mạng."""
        self._stop_requested = True
        self.reader.request_stop()
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._tab_changed.set)

    def get_confirmed_session(self, stt: int) -> Optional[Tuple[str, BrowserContext, Page]]:
        """Trả về (confirmation_url, context, page) trong bộ nhớ cho giai đoạn sau."""
        for rec in self.records:
            if rec.stt == stt and rec.status == STATUS_CONFIRMED and rec.confirmation_url:
                if rec.browser_context is not None and rec.page is not None:
                    return rec.confirmation_url, rec.browser_context, rec.page
        return None

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None:
            return self._browser
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        return self._browser

    async def _close_record_session(self, rec: RegistrationRecord) -> None:
        if rec.closing_page:
            return
        page = rec.page
        ctx = rec.browser_context
        rec.page = None
        rec.browser_context = None
        rec.closing_page = True
        try:
            if ctx is not None:
                await ctx.close()
            elif page is not None and not page.is_closed():
                await page.close()
        except PlaywrightError:
            pass
        finally:
            rec.closing_page = False
            self._tab_changed.set()
            if self._mail_wait_active and self._queued_rows:
                self.reader.request_stop()

    async def _new_record_page(self, rec: RegistrationRecord) -> Page:
        page = rec.page = await rec.browser_context.new_page()
        if self.config.form is not None:
            page.on("close", lambda *_: asyncio.create_task(self._handle_closed_page(rec, page)))
        return page

    async def _handle_closed_page(self, rec: RegistrationRecord, page: Page) -> None:
        if rec.page is not page or rec.status == STATUS_SENDING:
            return
        self._emit_status(rec, STATUS_TAB_CLOSED)
        rec.confirmation_url = None
        await self._close_record_session(rec)

    async def _park_record_page(self, rec: RegistrationRecord) -> None:
        # Keep the original context and cookies for a late mail retry.
        page, rec.page = rec.page, None
        if page is not None and not page.is_closed():
            try:
                await page.close()
            except PlaywrightError:
                pass

    async def _read_page_feedback(self, page: Page) -> Tuple[str, str]:
        error_parts: List[str] = []
        try:
            err_loc = page.locator(REGISTRATION_SELECTORS["error_container"])
            count = await err_loc.count()
            for idx in range(count):
                item = err_loc.nth(idx)
                try:
                    if await item.is_visible():
                        txt = (await item.inner_text(timeout=500)).strip()
                        if txt:
                            error_parts.append(txt)
                except Exception:
                    continue
        except Exception:
            pass

        body_text = ""
        try:
            body_text = (await page.locator("body").inner_text(timeout=500)).strip()
        except Exception:
            pass
        return body_text, " ".join(error_parts)

    async def _submit_single_request(self, browser: Browser, rec: RegistrationRecord) -> None:
        if self._stop_requested:
            self._emit_status(rec, STATUS_CANCELLED, "Đã hủy trước khi gửi.")
            return
        if not self.can_send(rec):
            return

        self._emit_status(rec, STATUS_SENDING)
        timeout_ms = int(self.config.submit_timeout_seconds * 1000)
        try:
            rec.browser_context = await browser.new_context()
            page = await self._new_record_page(rec)
            page_response = await page.goto(
                self.config.register_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            clock_offset = _server_clock_offset(page_response)
            register_url = urlsplit(self.config.register_url)
            current_url = urlsplit(page.url)
            allowed_hosts = {register_url.hostname}
            if register_url.hostname in self.config.filter_config.confirmation_hosts:
                allowed_hosts.update(self.config.filter_config.confirmation_hosts)
            if current_url.scheme != register_url.scheme or current_url.hostname not in allowed_hosts:
                raise RuntimeError("Trang đăng ký chuyển đến máy chủ khác.")
            form = page.locator(REGISTRATION_SELECTORS["form"])
            email = form.locator(REGISTRATION_SELECTORS["email_input"])
            submit = form.locator(REGISTRATION_SELECTORS["submit_button"])
            await email.fill(rec.email, timeout=timeout_ms)
            self.on_log(f"[{rec.stt}] Đã điền email.")
            action = await submit.evaluate(
                "(button) => button.hasAttribute('formaction') ? button.formAction : button.form?.action",
                timeout=timeout_ms,
            )
            method = await submit.evaluate(
                "(button) => button.hasAttribute('formmethod') ? button.formMethod : button.form?.method",
                timeout=timeout_ms,
            )
            target_url = urlsplit(action or "")
            if (method != "post" or target_url.scheme != current_url.scheme
                    or target_url.netloc != current_url.netloc):
                raise RuntimeError("Không nhận diện được đích gửi của form Ninja.")
            async with self._baseline_lock:
                if self._stop_requested:
                    self._emit_status(rec, STATUS_CANCELLED)
                    return
                captured_at = (
                    datetime.now(timezone.utc) + clock_offset if clock_offset is not None else None
                )
                rec.baseline = await asyncio.to_thread(
                    self.reader.mark_baseline, rec.email, captured_at
                )
            self.on_log(f"[{rec.stt}] Đã lưu mốc INBOX (UIDNEXT={rec.baseline.uidnext}).")
            if self._stop_requested:
                self._emit_status(rec, STATUS_CANCELLED)
                return
            status, reason = await self._click_and_track_submission(rec, page, submit, action, timeout_ms)
            if status == STATUS_WAITING_MAIL:
                rec.mail_deadline = asyncio.get_running_loop().time() + self.config.wait_timeout_seconds
            self._emit_status(rec, status, reason)
            self.on_log(f"[{rec.stt}] {rec.email}: {status} — {reason}")
        except Exception as exc:
            if self._stop_requested:
                self._emit_status(rec, STATUS_CANCELLED)
            else:
                status = STATUS_SEND_UNKNOWN if rec.send_attempts else STATUS_NOT_SUBMITTED
                self._emit_status(rec, status, "Gián đoạn khi chuẩn bị hoặc gửi yêu cầu.")
                self.on_log(f"[{rec.stt}] {status} ({type(exc).__name__}).")
        finally:
            if rec.status not in (STATUS_WAITING_MAIL, STATUS_SEND_UNKNOWN):
                await self._close_record_session(rec)

    @staticmethod
    async def _visible_click_position(submit: Any, timeout_ms: int) -> Optional[Dict[str, float]]:
        return await visible_click_position(submit, timeout_ms)

    async def _click_and_track_submission(self, rec, page, submit, action, timeout_ms):
        initial_feedback = await self._read_page_feedback(page)
        initial_request = None
        response_status = None
        failed = False
        wrong_email = False
        messages = deque()

        def on_request(request):
            nonlocal initial_request, wrong_email
            if initial_request is None and request.method == "POST" and request.url == action:
                initial_request = request
                wrong_email = parse_qs(request.post_data or "").get("email") != [rec.email]
                rec.send_attempts += 1
                rec.submitted_at_utc = datetime.now(timezone.utc)
                messages.append(f"Đã thấy POST {urlsplit(action).path}.")

        def belongs_to_submission(request):
            while request is not None:
                if request == initial_request:
                    return True
                request = request.redirected_from
            return False

        def on_response(response):
            nonlocal response_status
            if belongs_to_submission(response.request):
                response_status = response.status
                messages.append(f"Đã nhận phản hồi Ninja (HTTP {response.status}).")

        def on_failed(request):
            nonlocal failed
            if belongs_to_submission(request):
                failed = True

        def flush_messages():
            while messages:
                self.on_log(f"[{rec.stt}] {messages.popleft()}")

        listeners = (("request", on_request), ("response", on_response), ("requestfailed", on_failed))
        for event, handler in listeners:
            page.on(event, handler)

        try:
            position = await self._visible_click_position(submit, timeout_ms)
            click_kwargs: Dict[str, Any] = {"no_wait_after": True, "timeout": timeout_ms}
            if position is not None:
                click_kwargs["position"] = position
            await submit.click(**click_kwargs)
            self.on_log(f"[{rec.stt}] Đã bấm Gửi.")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_ms / 1000

            while loop.time() < deadline:
                flush_messages()
                if self._stop_requested:
                    return STATUS_CANCELLED, "Đã dừng trong khi chờ phản hồi."
                if wrong_email:
                    return STATUS_FAILED, "Địa chỉ trong yêu cầu gửi khác email đã điền."
                if page.is_closed() or failed or (response_status is not None and response_status >= 500):
                    break

                feedback = await self._read_page_feedback(page)
                if feedback != initial_feedback:
                    body, errors = feedback
                    status, reason = classify_registration_response(body, errors if errors != initial_feedback[1] else "")
                    if not initial_request and status in (STATUS_FAILED, STATUS_EMAIL_EXISTS):
                        return STATUS_NOT_SUBMITTED, "Form đã chặn gửi. Kiểm tra thông tin trên trang Ninja."
                    if initial_request is not None and response_status is not None:
                        if status in (STATUS_EMAIL_EXISTS, STATUS_FAILED) or (
                            status == STATUS_WAITING_MAIL and 200 <= response_status < 300
                        ):
                            return status, reason

                await asyncio.sleep(0.1)
        except PlaywrightError:
            if self._stop_requested:
                return STATUS_CANCELLED, "Đã dừng khi gửi."
            if initial_request is None and page.is_closed():
                return STATUS_SEND_UNKNOWN, "Tab đóng trong lúc gửi. Có thể kiểm tra thư lại."
        finally:
            flush_messages()
            for event, handler in listeners:
                page.remove_listener(event, handler)

        if initial_request is None and not page.is_closed():
            return STATUS_NOT_SUBMITTED, "Chưa thấy yêu cầu POST từ form Ninja."
        return STATUS_SEND_UNKNOWN, "Chưa xác nhận được phản hồi gửi. Có thể kiểm tra thư lại."

    def _pending_mail_requests(self) -> List[PendingMailRequest]:
        with self._state_lock:
            return [
                PendingMailRequest(
                    request_id=rec.request_id,
                    email=rec.email,
                    baseline=rec.baseline,
                )
                for rec in self.records
                if rec.status == STATUS_WAITING_MAIL and rec.baseline is not None
            ]

    def _apply_mail_outcome(self, outcome: MailMatchOutcome) -> None:
        rec = self._records_by_id.get(outcome.request_id)
        if rec is None:
            return
        if outcome.status == STATUS_CONFIRMED and outcome.confirmation_url:
            with self._state_lock:
                rec.confirmation_url = outcome.confirmation_url
            self._emit_status(rec, STATUS_CONFIRMED, "Đã giữ link xác nhận cùng phiên trình duyệt.")
            self.on_log(
                f"[{rec.stt}] Đã nhận link xác nhận cho {rec.email} (đang giữ trong bộ nhớ cùng phiên trình duyệt)."
            )
        else:
            self._emit_status(rec, STATUS_CHECK_MAIL, outcome.reason)
            self.on_log(f"[{rec.stt}] {rec.email}: {STATUS_CHECK_MAIL} — {outcome.reason}")

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            await self._run_batch()
        finally:
            await asyncio.to_thread(self.reader.close)

    async def _run_batch(self) -> None:
        """
        Kiểm tra kết nối IMAP thực tế, gửi đăng ký tạm cho các email hợp lệ
        và chờ nhận link xác nhận từ Gmail qua một bộ đọc IMAP duy nhất.
        """
        self._stop_requested = False
        self.reader.reset_stop()

        if not self.records:
            return
        if self.config.form is not None:
            if not validate_registration_password(self.config.form.password):
                raise ValueError("Mật khẩu đăng ký Ninja không hợp lệ.")
            if not 1 <= self.config.max_sessions <= 10 or self.config.headless:
                raise ValueError("Chọn từ 1 đến 10 phiên và mở trình duyệt để bàn giao form.")

        # Bước 2: Kiểm tra lại kết nối IMAP dùng cho phiên thực tế trước khi gửi bất kỳ yêu cầu nào
        self.on_log("Đang kiểm tra kết nối Gmail IMAP (INBOX chỉ đọc)...")
        await asyncio.to_thread(self.reader.ensure_connected)
        self.on_log("Kết nối Gmail IMAP sẵn sàng. Bắt đầu khởi chạy trình duyệt...")

        if self._stop_requested:
            for rec in self.records:
                if rec.status in RUNNING_STATUSES:
                    self._emit_status(rec, STATUS_CANCELLED)
            return

        browser = await self._ensure_browser()
        if self.config.form is not None:
            await self._run_form_batch(browser)
            return

        for rec in self.records:
            if self._stop_requested:
                self._emit_status(rec, STATUS_CANCELLED)
                continue
            await self._submit_single_request(browser, rec)

        if not self._stop_requested and self._pending_mail_requests():
            self.on_log(
                f"Đang chờ thư xác nhận tối đa {int(self.config.wait_timeout_seconds)} giây..."
            )
            await asyncio.to_thread(
                self.reader.wait_for_requests,
                self._pending_mail_requests,
                self.config.wait_timeout_seconds,
                self._apply_mail_outcome,
                self.on_log,
            )

        for rec in self.records:
            if rec.status in RUNNING_STATUSES:
                if self._stop_requested:
                    self._emit_status(rec, STATUS_CANCELLED, "Đã hủy bởi người dùng.")
                    await self._close_record_session(rec)
                elif rec.status == STATUS_WAITING_MAIL:
                    self._emit_status(
                        rec,
                        STATUS_TIMEOUT,
                        "Hết thời gian chờ thư; có thể dùng Kiểm tra thư lại.",
                    )
                    self.on_log(f"[{rec.stt}] {rec.email}: {STATUS_TIMEOUT}.")

    async def _run_form_batch(self, browser: Browser) -> None:
        queued = deque(self.records)
        loop = asyncio.get_running_loop()
        ocr_client = await self._start_ocr_session()
        try:
            while not self._stop_requested:
                self._tab_changed.clear()
                free = self.config.max_sessions - sum(
                    rec.page is not None or rec.closing_page for rec in self.records
                )
                starting = [queued.popleft() for _ in range(min(free, len(queued)))]
                self._queued_rows = bool(queued)
                if starting:
                    await asyncio.gather(*(self._submit_single_request(browser, rec) for rec in starting))
                    for rec in starting:
                        if rec.status == STATUS_SEND_UNKNOWN:
                            await self._park_record_page(rec)
                    continue

                waiting = [rec for rec in self.records if rec.status == STATUS_WAITING_MAIL]
                if not waiting:
                    if not queued:
                        break
                    await asyncio.to_thread(self.reader.close)
                    await self._tab_changed.wait()
                    continue

                outcomes = await self._wait_for_mail(waiting)
                if self._stop_requested:
                    break
                await asyncio.gather(*(
                    self._prepare_mail_outcome(outcome)
                    for outcome in outcomes
                    if self._records_by_id[outcome.request_id].status == STATUS_WAITING_MAIL
                ))
                for rec in waiting:
                    if rec.status == STATUS_WAITING_MAIL and rec.mail_deadline <= loop.time():
                        self._emit_status(rec, STATUS_TIMEOUT, "Có thể dùng Kiểm tra thư lại.")
                        self.on_log(f"[{rec.stt}] {STATUS_TIMEOUT}.")
                        await self._park_record_page(rec)
        finally:
            self._queued_rows = False
            await self._stop_ocr_session(ocr_client)
            if self._stop_requested:
                for rec in self.records:
                    if rec.status in (*RUNNING_STATUSES, STATUS_CONFIRMED, STATUS_FORM_PREPARING, STATUS_OCR_SOLVING):
                        self._emit_status(rec, STATUS_CANCELLED)
                        await self._close_record_session(rec)
            await asyncio.to_thread(self.reader.close)
            await self._wait_for_handoff()

    async def _start_ocr_session(self) -> Optional[HiraganaOCRClient]:
        if self._external_hiragana_hook is not None:
            self._hiragana_hook = self._external_hiragana_hook
            return None
        if self.config.form is None or not self.config.use_ocr:
            self._hiragana_hook = None
            return None
        ocr_url = (self.config.ocr_url or resolve_default_ocr_url()).rstrip("/")
        started_ok, start_msg = await asyncio.to_thread(ensure_ocr_server_running, ocr_url)
        if started_ok:
            self.on_log(f"[OCR] {start_msg} ({ocr_url})")
        else:
            self.on_log(
                f"[OCR Cảnh báo] {start_msg}. Chuyển sang chế độ chờ nhập CAPTCHA thủ công."
            )
            self._hiragana_hook = None
            return None
        if self._stop_requested:
            return None
        client = HiraganaOCRClient(
            OCRConfig(base_url=ocr_url, state_file=Path("ocr_cursor_register.json"))
        )
        try:
            client.start_polling(asyncio.get_running_loop())
        except Exception as exc:
            self.on_log(f"[OCR Cảnh báo] Không thể khởi động bộ đọc OCR ({exc}). Chuyển sang nhập tay.")
            self._hiragana_hook = None
            return None
        self._ocr_client = client
        self._hiragana_hook = OCRHiraganaHook(
            ocr_client=client,
            fallback_hook=None,
            timeout=self.config.ocr_timeout_seconds,
            on_log=self.on_log,
        )
        return client

    async def _stop_ocr_session(self, client: Optional[HiraganaOCRClient]) -> None:
        if client is not None:
            try:
                await client.stop_polling()
            except Exception:
                pass
        if self._ocr_client is client:
            self._ocr_client = None
        if self._external_hiragana_hook is None:
            self._hiragana_hook = None

    @staticmethod
    async def _is_ocr_healthy(hook: HiraganaHook) -> bool:
        client = getattr(hook, "ocr_client", None)
        check_health = getattr(client, "check_health", None)
        if not callable(check_health):
            return True
        result = await asyncio.to_thread(check_health)
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
            return result[0]
        return True

    async def _wait_for_mail(self, waiting: List[RegistrationRecord]) -> List[MailMatchOutcome]:
        pending = self._pending_mail_requests()
        timeout = min(rec.mail_deadline for rec in waiting) - asyncio.get_running_loop().time()
        outcomes: List[MailMatchOutcome] = []
        self._mail_wait_active = True
        try:
            await asyncio.to_thread(
                self.reader.wait_for_requests,
                lambda: [] if outcomes else pending,
                timeout,
                outcomes.append,
                self.on_log,
            )
        finally:
            self._mail_wait_active = False
            if not self._stop_requested:
                self.reader.reset_stop()
        return outcomes

    async def _click_and_observe_confirmation_submit(
        self,
        page: Page,
        rec: RegistrationRecord,
        submit_button: Locator,
        previous_time_origin: Optional[float],
        timeout_ms: int,
    ) -> Tuple[str, str]:
        initial_request = None
        response_status = None
        failed = False
        finished = False

        def on_request(request) -> None:
            nonlocal initial_request
            if initial_request is None and request.method == "POST" and request.frame == page.main_frame:
                initial_request = request

        def belongs(request) -> bool:
            while request is not None:
                if request == initial_request:
                    return True
                request = request.redirected_from
            return False

        def on_response(response) -> None:
            nonlocal response_status
            if belongs(response.request):
                response_status = response.status

        def on_finished(request) -> None:
            nonlocal finished
            if belongs(request) and request.redirected_to is None:
                finished = True

        def on_failed(request) -> None:
            nonlocal failed
            if belongs(request):
                failed = True

        listeners = (
            ("request", on_request),
            ("response", on_response),
            ("requestfinished", on_finished),
            ("requestfailed", on_failed),
        )
        for event, handler in listeners:
            page.on(event, handler)

        try:
            position = await visible_click_position(submit_button, timeout_ms)
            click_kwargs: Dict[str, Any] = {"no_wait_after": True, "timeout": timeout_ms}
            if position is not None:
                click_kwargs["position"] = position
            await submit_button.click(**click_kwargs)

            loop = asyncio.get_running_loop()
            start_time = loop.time()
            deadline = start_time + max(2.0, timeout_ms / 1000)
            settled_at: Optional[float] = None

            while loop.time() < deadline:
                if self._stop_requested:
                    return "cancelled", "Đã dừng trong khi gửi form."
                if rec.status == STATUS_TAB_CLOSED or page.is_closed():
                    return "closed", "Tab trình duyệt đã đóng."
                if failed or (response_status is not None and response_status >= 500):
                    return "server_error", "Ninja trả lỗi máy chủ hoặc rớt kết nối khi gửi."

                if initial_request is None and loop.time() - start_time >= min(3.5, timeout_ms / 1000):
                    try:
                        state, detail = await inspect_registration_submission_state(
                            page, previous_time_origin=previous_time_origin
                        )
                        if state in ("finished", "confirm_page", "expired", "retry"):
                            return state, detail
                    except PlaywrightError:
                        pass
                    return "no_post", "Không thấy yêu cầu POST khi bấm nút gửi form."

                if initial_request is not None and finished:
                    if settled_at is None:
                        settled_at = loop.time()
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 2000))
                        except PlaywrightError:
                            pass
                    try:
                        state, detail = await inspect_registration_submission_state(
                            page, previous_time_origin=previous_time_origin
                        )
                    except PlaywrightError as exc:
                        if "Execution context was destroyed" in str(exc) or "Target closed" in str(exc):
                            await asyncio.sleep(0.1)
                            continue
                        return "unknown", "Không thể đọc phản hồi sau khi gửi form."
                    if state in ("finished", "confirm_page", "expired", "retry"):
                        return state, detail
                    if loop.time() - settled_at >= 1.2:
                        return "unknown", detail or "Không xác định được kết quả sau khi gửi form."

                await asyncio.sleep(0.1)
        except PlaywrightError:
            if self._stop_requested:
                return "cancelled", "Đã dừng khi gửi form."
            if rec.status == STATUS_TAB_CLOSED or page.is_closed():
                return "closed", "Tab trình duyệt đã đóng."
            return "unknown", "Gián đoạn khi bấm nút gửi form."
        finally:
            for event, handler in listeners:
                page.remove_listener(event, handler)

        return "unknown", "Hết thời gian chờ phản hồi sau khi gửi form."

    async def _submit_confirmation_form(
        self,
        page: Page,
        rec: RegistrationRecord,
        controls: ConfirmationFormControls,
        timeout_ms: int,
    ) -> Tuple[str, str]:
        try:
            prev_origin = await page.evaluate("() => performance.timeOrigin")
        except PlaywrightError:
            prev_origin = None
        state, detail = await self._click_and_observe_confirmation_submit(
            page, rec, controls.submit_button, prev_origin, timeout_ms
        )
        if state == "confirm_page":
            confirm_button = await locate_confirm_step_button(page)
            if confirm_button is None:
                return "unknown", "Không tìm thấy nút xác nhận cuối trên trang Ninja."
            self.on_log(f"[{rec.stt}] Đã qua bước điền thông tin, đang bấm xác nhận hoàn tất...")
            try:
                confirm_origin = await page.evaluate("() => performance.timeOrigin")
            except PlaywrightError:
                confirm_origin = None
            state, detail = await self._click_and_observe_confirmation_submit(
                page, rec, confirm_button, confirm_origin, timeout_ms
            )
        return state, detail

    async def _solve_captcha_and_submit(
        self,
        rec: RegistrationRecord,
        page: Page,
        hook: HiraganaHook,
        timeout_ms: int,
    ) -> bool:
        max_attempts = max(1, int(self.config.ocr_max_attempts))
        last_image_bytes: Optional[bytes] = None

        for attempt in range(1, max_attempts + 1):
            if self._stop_requested or rec.status == STATUS_TAB_CLOSED or page.is_closed():
                return False

            controls = await prepare_confirmation_form(
                page,
                rec.email,
                self.config.form,
                timeout_ms,
                clear_captcha=(attempt > 1),
                focus_captcha=False,
            )
            self._emit_status(rec, STATUS_OCR_SOLVING)
            self.on_log(f"[{rec.stt}] Thử giải CAPTCHA OCR lần {attempt}/{max_attempts}...")

            image_bytes = await capture_captcha_image(
                page,
                controls.captcha_image,
                previous_bytes=last_image_bytes,
                timeout_ms=min(timeout_ms, 6000),
            )
            last_image_bytes = image_bytes

            if isinstance(hook, OCRHiraganaHook):
                solved_text = await hook.solve_ocr(image_bytes, rec.stt, rec.email)
            else:
                solved_text = await hook.solve(image_bytes, rec.stt, rec.email)

            if self._stop_requested or rec.status == STATUS_TAB_CLOSED or page.is_closed():
                return False

            if not solved_text:
                if isinstance(hook, OCRHiraganaHook) and not await self._is_ocr_healthy(hook):
                    self.on_log(
                        f"[{rec.stt}] Dịch vụ OCR không khả dụng. Chuyển sang Form sẵn sàng – chờ tiếp nhận để nhập tay."
                    )
                    return False
                if attempt < max_attempts:
                    self.on_log(f"[{rec.stt}] OCR chưa đọc được CAPTCHA hợp lệ, đang đổi ảnh mới...")
                    await refresh_captcha_image(page, controls.captcha_row, timeout_ms=min(timeout_ms, 5000))
                    continue
                break

            self.on_log(f"[{rec.stt}] OCR lần {attempt}/{max_attempts} nhận '{solved_text}'. Đang gửi form...")
            await controls.captcha_input.fill(solved_text)
            state, detail = await self._submit_confirmation_form(page, rec, controls, timeout_ms)

            if state == "finished":
                self._emit_status(rec, STATUS_REGISTERED, detail)
                self.on_log(f"[{rec.stt}] {STATUS_REGISTERED} ({rec.email}).")
                rec.confirmation_url = None
                await self._close_record_session(rec)
                return True
            if state == "expired":
                raise RegistrationFormError(STATUS_FORM_EXPIRED, detail)
            if state in ("cancelled", "closed"):
                return False
            if state == "retry":
                if attempt < max_attempts:
                    self.on_log(f"[{rec.stt}] CAPTCHA chưa đúng ({detail}). Thử lại...")
                    continue
                break

            self.on_log(f"[{rec.stt}] {detail} Chuyển sang kiểm tra thủ công.")
            return False

        self.on_log(
            f"[{rec.stt}] OCR chưa giải xong CAPTCHA sau {max_attempts} lần. "
            "Chuyển sang Form sẵn sàng – chờ tiếp nhận để nhập tay."
        )
        return False

    async def _prepare_mail_outcome(self, outcome: MailMatchOutcome) -> None:
        rec = self._records_by_id[outcome.request_id]
        self._apply_mail_outcome(outcome)
        if rec.status != STATUS_CONFIRMED:
            await self._close_record_session(rec)
            return
        if self._stop_requested:
            self._emit_status(rec, STATUS_CANCELLED)
            await self._close_record_session(rec)
            return
        timeout_ms = int(self.config.submit_timeout_seconds * 1000)
        try:
            if rec.browser_context is None:
                raise RegistrationFormError(STATUS_FORM_FAILED, "Phiên trình duyệt ban đầu đã đóng.")
            if rec.page is None:
                await self._new_record_page(rec)
            page = await self._navigate_confirmation_page(rec)
            self._emit_status(rec, STATUS_FORM_PREPARING)
            hook = self._hiragana_hook
            if hook is not None:
                completed = await self._solve_captcha_and_submit(rec, page, hook, timeout_ms)
                if completed:
                    return
                if self._stop_requested:
                    self._emit_status(rec, STATUS_CANCELLED)
                    rec.confirmation_url = None
                    await self._close_record_session(rec)
                    return
                if rec.status == STATUS_TAB_CLOSED or page.is_closed():
                    self._emit_status(rec, STATUS_TAB_CLOSED)
                    rec.confirmation_url = None
                    await self._close_record_session(rec)
                    return
                await prepare_confirmation_form(
                    page,
                    rec.email,
                    self.config.form,
                    timeout_ms,
                    clear_captcha=True,
                    focus_captcha=True,
                )
            else:
                await fill_confirmation_form(page, rec.email, self.config.form, timeout_ms)
        except RegistrationFormError as exc:
            if rec.status != STATUS_TAB_CLOSED:
                self._emit_status(rec, exc.status, str(exc))
                self.on_log(f"[{rec.stt}] {exc.status} — {exc}")
        except PlaywrightError:
            if rec.status != STATUS_TAB_CLOSED:
                self._emit_status(rec, STATUS_FORM_FAILED)
                self.on_log(f"[{rec.stt}] {STATUS_FORM_FAILED}.")
        else:
            if rec.status == STATUS_TAB_CLOSED or page.is_closed():
                self._emit_status(rec, STATUS_TAB_CLOSED)
            elif not self._stop_requested:
                self._emit_status(rec, STATUS_FORM_READY)
                self.on_log(f"[{rec.stt}] {STATUS_FORM_READY}.")
                return
            else:
                self._emit_status(rec, STATUS_CANCELLED)
        rec.confirmation_url = None
        await self._close_record_session(rec)

    async def _wait_for_handoff(self) -> None:
        while not self._discard_forms:
            self._tab_changed.clear()
            if not any(rec.status == STATUS_FORM_READY or rec.closing_page for rec in self.records):
                return
            await self._tab_changed.wait()

    def _ready_record(self, stt: int) -> RegistrationRecord:
        if 1 <= stt <= len(self.records):
            rec = self.records[stt - 1]
            if rec.status == STATUS_FORM_READY and rec.page is not None and not rec.page.is_closed():
                return rec
        raise RuntimeError("Form của dòng này đã đóng hoặc chưa sẵn sàng.")

    async def focus_form(self, stt: int) -> None:
        rec = self._ready_record(stt)
        try:
            await rec.page.bring_to_front()
        except PlaywrightError:
            raise RuntimeError("Không thể đưa tab xác nhận ra trước.") from None

    async def acknowledge_form(self, stt: int) -> None:
        if 1 <= stt <= len(self.records):
            if self.records[stt - 1].status in (STATUS_FORM_HANDOFF, STATUS_REGISTERED):
                return
        rec = self._ready_record(stt)
        page = rec.page
        status, note = STATUS_FORM_HANDOFF, ""
        try:
            current = urlsplit(page.url)
            if (
                current.scheme == "https" and current.netloc in self.config.filter_config.confirmation_hosts
                and current.path.startswith("/register/")
            ):
                state, detail = await inspect_registration_submission_state(page)
                if state == "finished" and page.url == current.geturl():
                    status, note = STATUS_REGISTERED, detail
        except (PlaywrightError, ValueError):
            pass
        if rec.status != STATUS_FORM_READY or rec.page is not page:
            return
        self._emit_status(rec, status, note)
        rec.confirmation_url = None
        await self._close_record_session(rec)
        self.on_log(f"[{stt}] {status}.")

    async def retry_mail_check(self) -> List[int]:
        """
        Chỉ đọc lại thư từ mốc hộp thư đã lưu cho các dòng Hết giờ chờ thư
        hoặc Chưa xác định kết quả gửi. Tuyệt đối không gửi lại đăng ký.
        """
        self._stop_requested = False
        self.reader.reset_stop()

        eligible = [rec for rec in self.records if self.can_retry_mail_check(rec)]
        if not eligible:
            self.on_log("Không có dòng nào ở trạng thái Hết giờ chờ thư hoặc Chưa xác định kết quả gửi.")
            return []

        self.on_log(
            f"Đang kiểm tra thư lại cho {len(eligible)} yêu cầu (không gửi lại đăng ký)..."
        )
        pending = [
            PendingMailRequest(
                request_id=rec.request_id,
                email=rec.email,
                baseline=rec.baseline,  # type: ignore[arg-type]
            )
            for rec in eligible
        ]
        try:
            outcomes = await asyncio.to_thread(self.reader.poll_once, pending, self.on_log)
        finally:
            await asyncio.to_thread(self.reader.close)
        changed_stts: List[int] = []
        matched_ids = set()
        for outcome in outcomes:
            matched_ids.add(outcome.request_id)
            if self.config.form is None:
                self._apply_mail_outcome(outcome)
            rec = self._records_by_id.get(outcome.request_id)
            if rec is not None:
                changed_stts.append(rec.stt)

        for rec in eligible:
            if rec.request_id not in matched_ids:
                self.on_log(f"[{rec.stt}] {rec.email}: Chưa thấy thư xác nhận phù hợp trong INBOX.")
        if self.config.form is not None and outcomes:
            ocr_client = await self._start_ocr_session()
            try:
                limit = self.config.max_sessions
                for start in range(0, len(outcomes), limit):
                    if self._stop_requested:
                        for pending_outcome in outcomes[start:]:
                            self._apply_mail_outcome(pending_outcome)
                        break
                    await asyncio.gather(*(
                        self._prepare_mail_outcome(outcome)
                        for outcome in outcomes[start:start + limit]
                    ))
                    await self._wait_for_handoff()
            finally:
                await self._stop_ocr_session(ocr_client)
        return changed_stts

    async def open_confirmation_page(self, stt: int) -> Page:
        """
        Mở link xác nhận đã giữ trong bộ nhớ trên chính phiên trình duyệt ban đầu.
        Dùng cho bước xác minh hoặc giai đoạn điền form tiếp theo.
        """
        if self.get_confirmed_session(stt) is None:
            raise RuntimeError(f"Dòng #{stt} chưa có link xác nhận hoặc phiên trình duyệt hợp lệ.")
        return await self._navigate_confirmation_page(self.records[stt - 1])

    async def _navigate_confirmation_page(self, rec: RegistrationRecord) -> Page:
        url, page = rec.confirmation_url, rec.page
        if page is None or page.is_closed():
            raise RegistrationFormError(STATUS_TAB_CLOSED, "Tab trình duyệt ban đầu đã đóng.")
        if not url or not is_confirmation_url(url, self.config.filter_config):
            raise RegistrationFormError(STATUS_FORM_INVALID, "Link xác nhận Ninja không hợp lệ.")
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self.config.submit_timeout_seconds * 1000),
            )
        except PlaywrightError:
            raise RegistrationFormError(
                STATUS_FORM_FAILED, "Không thể mở form xác nhận trong tab ban đầu."
            ) from None
        if response is None or not response.ok:
            status = STATUS_FORM_EXPIRED if response and response.status in (404, 410) else STATUS_FORM_FAILED
            raise RegistrationFormError(status, "Ninja không trả về form xác nhận. Hãy kiểm tra link còn hiệu lực.")
        final_url = urlsplit(page.url)
        if not (
            final_url.scheme == "https"
            and final_url.hostname in self.config.filter_config.confirmation_hosts
            and final_url.path.startswith("/register/")
            and final_url.username is None
            and final_url.port in (None, 443)
        ):
            raise RegistrationFormError(STATUS_FORM_INVALID, "Trang đích không phải form đăng ký của Ninja.")
        self.on_log(f"[{rec.stt}] Đã mở link xác nhận trong phiên trình duyệt ban đầu.")
        return page

    async def close(self) -> None:
        """Đóng bộ đọc IMAP và toàn bộ phiên trình duyệt đang giữ."""
        self.request_stop()
        await asyncio.to_thread(self.reader.close)
        for rec in self.records:
            await self._close_record_session(rec)
        if self._browser is not None and self._external_browser is None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None


class RegistrationWorkerController:
    """
    Giữ một event loop asyncio duy nhất trên thread nền để phiên trình duyệt
    và worker đăng ký sống xuyên suốt giữa Bắt đầu, Kiểm tra thư lại và Mở form.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._lock = threading.Lock()
        self.worker: Optional[RegistrationWorker] = None
        self._active_future: Optional[Future[Any]] = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            self._loop_ready.clear()
            loop = asyncio.new_event_loop()
            self._loop = loop

            def run_loop() -> None:
                asyncio.set_event_loop(loop)
                self._loop_ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            self._thread = threading.Thread(target=run_loop, daemon=True)
            self._thread.start()

        self._loop_ready.wait(timeout=5.0)
        return loop

    def is_busy(self) -> bool:
        return self._active_future is not None and not self._active_future.done()

    def start_batch(
        self,
        config: RegistrationConfig,
        on_status: RegistrationStatusCallback,
        on_log: Callable[[str], None],
        on_finished: Callable[[Optional[str]], None],
        imap_factory: Optional[Callable[[str, int, float], Any]] = None,
    ) -> None:
        if self.is_busy():
            raise RuntimeError("Phiên đăng ký đang chạy.")
        loop = self._ensure_loop()

        async def runner() -> None:
            if self.worker is not None:
                await self.worker.close()
            self.worker = RegistrationWorker(
                config=config,
                on_status=on_status,
                on_log=on_log,
                imap_factory=imap_factory,
            )
            await self.worker.run()

        fut = asyncio.run_coroutine_threadsafe(runner(), loop)
        self._active_future = fut

        def _done(done_fut: Future[Any]) -> None:
            self._active_future = None
            err: Optional[str] = None
            try:
                done_fut.result()
            except Exception as exc:
                err = str(exc) or type(exc).__name__
            on_finished(err)

        fut.add_done_callback(_done)

    def retry_mail_check(
        self,
        on_finished: Callable[[Optional[str], List[int]], None],
    ) -> None:
        if self.is_busy():
            raise RuntimeError("Phiên đăng ký đang bận.")
        if self.worker is None:
            raise RuntimeError("Chưa có phiên đăng ký nào để kiểm tra thư lại.")
        loop = self._ensure_loop()
        worker = self.worker
        fut = asyncio.run_coroutine_threadsafe(worker.retry_mail_check(), loop)
        self._active_future = fut

        def _done(done_fut: Future[List[int]]) -> None:
            self._active_future = None
            try:
                changed = done_fut.result()
                on_finished(None, changed)
            except Exception as exc:
                on_finished(str(exc) or type(exc).__name__, [])

        fut.add_done_callback(_done)

    def open_confirmation_page(
        self,
        stt: int,
        on_finished: Callable[[Optional[str]], None],
    ) -> None:
        if self.is_busy():
            raise RuntimeError("Phiên đăng ký đang bận.")
        if self.worker is None:
            raise RuntimeError("Chưa có phiên đăng ký nào.")
        loop = self._ensure_loop()
        worker = self.worker
        fut = asyncio.run_coroutine_threadsafe(worker.open_confirmation_page(stt), loop)
        self._active_future = fut

        def _done(done_fut: Future[Any]) -> None:
            self._active_future = None
            try:
                done_fut.result()
                on_finished(None)
            except Exception as exc:
                on_finished(str(exc) or type(exc).__name__)

        fut.add_done_callback(_done)

    def focus_form(self, stt: int, on_finished: Callable[[Optional[str]], None]) -> None:
        self._run_form_action("focus", stt, on_finished)

    def acknowledge_form(self, stt: int, on_finished: Callable[[Optional[str]], None]) -> None:
        self._run_form_action("acknowledge", stt, on_finished)

    def _run_form_action(
        self, action: str, stt: int, on_finished: Callable[[Optional[str]], None]
    ) -> None:
        worker = self.worker
        if worker is None or self._loop is None:
            on_finished("Chưa có phiên đăng ký nào.")
            return
        operation = worker.focus_form if action == "focus" else worker.acknowledge_form
        future = asyncio.run_coroutine_threadsafe(operation(stt), self._loop)

        def complete(done: Future) -> None:
            try:
                done.result()
            except RuntimeError as exc:
                on_finished(str(exc))
            except Exception:
                on_finished("Không thể thao tác với form Ninja.")
            else:
                on_finished(None)

        future.add_done_callback(complete)

    def request_stop(self) -> None:
        if self.worker is not None:
            self.worker.request_stop()

    def shutdown(self, timeout: float = 5.0) -> bool:
        self.request_stop()
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None or not thread.is_alive():
            return True
        active = self._active_future

        async def finish():
            # The reader must leave IDLE with DONE before its owner sends LOGOUT.
            self.request_stop()
            if self.worker is not None:
                self.worker._discard_forms = True
                self.worker._tab_changed.set()
            if active is not None:
                try:
                    await asyncio.wrap_future(active)
                except Exception:
                    pass
            if self.worker is not None:
                await self.worker.close()
                self.worker = None

        cleanup = asyncio.run_coroutine_threadsafe(finish(), loop)
        cleanup.add_done_callback(lambda _: loop.call_soon_threadsafe(loop.stop))
        thread.join(timeout=timeout)
        if thread.is_alive():
            return False
        self._loop = None
        self._thread = None
        return True

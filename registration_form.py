"""Điền form xác nhận Ninja trước khi bàn giao CAPTCHA cho người dùng."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError, Locator, Page


STATUS_FORM_EXPIRED = "Link hết hiệu lực"
STATUS_WRONG_ID = "Sai ID trên form"
STATUS_FORM_INVALID = "Không nhận diện được form"

_PASSWORD_RULE = re.compile(r"[A-Za-z0-9._-]{4,16}")
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ID = re.compile(r"^\s*(?:ID|ＩＤ)\s*[（(]\s*メールアドレス\s*[）)]\s*[:：]?\s*$", re.I)
_PASSWORD = re.compile(r"^\s*(?:必須\s*)?パスワード\s*[:：]?\s*$")
_PASSWORD_CONFIRM = re.compile(r"^\s*(?:必須\s*)?パスワードの確認\s*[:：]?\s*$")
_NEWSLETTER = re.compile(r"^\s*(?:必須\s*)?メールマガジン\s*[:：]?\s*$")
_CAPTCHA = re.compile(r"^\s*(?:必須\s*)?画像認証\s*[:：]?\s*$")
_CAPTCHA_INPUT = re.compile(r"^\s*(?:文字の入力|画像認証(?:の入力)?)\s*[:：]?\s*$")
_TERMS = re.compile(r"^利用規約\s*に同意します[。.]?$")
_PRIVACY = re.compile(r"^個人情報の(?:取扱い?|取り扱い)\s*に同意します[。.]?$")
_EXPIRY = re.compile(r"有効期限.{0,12}(?:過ぎ|切れ|経過)|仮登録.{0,18}無効|URL.{0,12}(?:無効|期限切れ)", re.I)
_REFRESH_CAPTCHA = re.compile(r"読みにくいので画像変更")
_FINISH_MARKER = re.compile(
    r"(?:新規ユーザー|本)?登録(?:手続き)?が完了|ユーザー登録完了|登録が完了しました|登録完了メール"
)
_CAPTCHA_ERROR = re.compile(
    r"(?:画像認証|認証文字|画像内の文字).{0,25}(?:一致|正しく|誤り|違い|入力|確認)|文字が一致しません"
)
_ERROR_CONTAINERS = (
    ".errorArea, .error, .errortext, .caution, .alert, .alert-danger, .alert-error, font[color=red]"
)
_CAPTCHA_INPUT_SELECTOR = 'input[type="text"]:visible, input:not([type]):visible'


@dataclass(frozen=True)
class ConfirmationFormControls:
    """Các phần tử tương tác trên form xác nhận Ninja."""

    form: Locator
    captcha_row: Locator
    captcha_image: Locator
    captcha_input: Locator
    submit_button: Locator


@dataclass(frozen=True)
class RegistrationFormConfig:
    password: str = field(repr=False)
    newsletter: bool = False
    accept_terms: bool = False
    accept_privacy: bool = False


class RegistrationFormError(RuntimeError):
    """Trạng thái theo từng dòng và thông báo dành cho giao diện."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def validate_registration_password(password: str) -> bool:
    return bool(_PASSWORD_RULE.fullmatch(password))


def _rows(page: Page, form: Locator, heading: re.Pattern[str]) -> Locator:
    return form.locator("tr:visible").filter(
        has=page.locator("th:visible").filter(has_text=heading)
    )


def _field(page: Page, form: Locator, heading: re.Pattern[str], selector: str) -> Locator:
    return form.get_by_label(heading).and_(page.locator(selector)).or_(
        _rows(page, form, heading).locator(selector)
    )


async def _require_one(locator: Locator, description: str) -> Locator:
    if await locator.count() != 1:
        raise RegistrationFormError(STATUS_FORM_INVALID, f"Không tìm thấy duy nhất {description}.")
    return locator


async def _check_form_id(page: Page, form: Locator, expected_email: str) -> None:
    heading_cells = _rows(page, form, _ID).locator("td:visible")
    labelled_inputs = form.get_by_label(_ID).and_(page.locator("input:visible"))
    data = await heading_cells.all_inner_texts()
    for control in await labelled_inputs.or_(heading_cells.locator("input:visible")).all():
        data.append(await control.input_value())
    addresses = {address.casefold() for value in data for address in _EMAIL.findall(value)}
    if len(addresses) != 1:
        raise RegistrationFormError(STATUS_FORM_INVALID, "Không xác định được ID email trên form.")
    if expected_email.strip().casefold() not in addresses:
        raise RegistrationFormError(STATUS_WRONG_ID, "ID email trên form khác email đã gửi.")


async def _named_choice(controls: Locator, label: re.Pattern[str], description: str) -> Locator:
    # Some versions of the form print the choice immediately after the input.
    labels = await controls.evaluate_all(
        r"""inputs => inputs.map(input => {
            const names = Array.from(input.labels || [], label => label.innerText);
            names.push(input.getAttribute("aria-label") || "");
            const ids = (input.getAttribute("aria-labelledby") || "").split(/\s+/);
            names.push(ids.map(id => input.ownerDocument.getElementById(id)?.innerText || "").join(" "));
            const siblings = [];
            for (let node = input.nextSibling; node; node = node.nextSibling) {
                if (node.nodeType === 1) {
                    if (node.matches("input, button, br") || node.querySelector("input, button")) break;
                    if (node.matches("label")) {
                        if (!node.htmlFor || node.htmlFor === input.id) {
                            siblings.push(node.innerText || "");
                        }
                        break;
                    }
                    siblings.push(node.innerText || "");
                } else if (node.nodeType === 3) {
                    siblings.push(node.textContent);
                }
            }
            names.push(siblings.join(""));
            let container = input.parentElement;
            for (let depth = 0; depth < 2 && container; depth++, container = container.parentElement) {
                if (container.matches("span, p, div, li") &&
                    container.querySelectorAll("input, button").length === 1) {
                    names.push(container.innerText || "");
                }
            }
            return names;
        })"""
    )
    matches = [
        index for index, texts in enumerate(labels)
        if any(label.fullmatch(" ".join(text.split())) for text in texts)
    ]
    if len(matches) != 1:
        raise RegistrationFormError(STATUS_FORM_INVALID, f"Không nhận diện được {description}.")
    return controls.nth(matches[0])


async def visible_click_position(locator: Locator, timeout_ms: int) -> Optional[Dict[str, float]]:
    """Tính tọa độ hiển thị sau khi cắt overflow để bấm đúng các nút sprite bị trượt khi hover."""
    await locator.scroll_into_view_if_needed(timeout=timeout_ms)
    return await locator.evaluate(
        """async (el) => {
            let rect = el.getBoundingClientRect();
            if (el.tagName === "INPUT" && el.type === "image" && el.src && rect.width < 30 && rect.height < 30) {
                await new Promise((resolve) => {
                    const timer = setTimeout(resolve, 3000);
                    const done = () => { clearTimeout(timer); resolve(); };
                    el.addEventListener("load", done, { once: true });
                    el.addEventListener("error", done, { once: true });
                });
                rect = el.getBoundingClientRect();
            }
            if (!rect.width || !rect.height) return null;
            let left = rect.left, top = rect.top, right = rect.right, bottom = rect.bottom;
            const doc = el.ownerDocument;
            for (let p = el.parentElement; p && p !== doc.body && p !== doc.documentElement; p = p.parentElement) {
                const style = doc.defaultView.getComputedStyle(p);
                if (/hidden|clip|auto|scroll/.test(style.overflowX)) {
                    const pr = p.getBoundingClientRect();
                    left = Math.max(left, pr.left);
                    right = Math.min(right, pr.right);
                }
                if (/hidden|clip|auto|scroll/.test(style.overflowY)) {
                    const pr = p.getBoundingClientRect();
                    top = Math.max(top, pr.top);
                    bottom = Math.min(bottom, pr.bottom);
                }
            }
            if (right <= left || bottom <= top) return null;
            return { x: (left + right) / 2 - rect.left, y: (top + bottom) / 2 - rect.top };
        }""",
        timeout=timeout_ms,
    )


async def locate_submit_button(form: Locator) -> Locator:
    """Tìm nút gửi form bản đăng ký trong phạm vi form xác nhận Ninja."""
    sprite_btn = form.locator("#registerInputSend input:visible, #registerInputSend button:visible")
    if await sprite_btn.count() > 0:
        return sprite_btn.first
    generic_btn = form.locator(
        'input[type="image"]:visible, input[type="submit"]:visible, button[type="submit"]:visible'
    )
    if await generic_btn.count() > 0:
        return generic_btn.first
    raise RegistrationFormError(STATUS_FORM_INVALID, "Không tìm thấy nút gửi form đăng ký Ninja.")


async def locate_confirm_step_button(page: Page) -> Optional[Locator]:
    """Tìm nút xác nhận cuối nếu Ninja hiển thị màn hình xác nhận (/register/confirm)."""
    confirm_btn = page.locator("#registerInputSend2 input:visible, #registerInputSend2 button:visible")
    if await confirm_btn.count() > 0:
        return confirm_btn.first
    generic_btn = page.locator(
        'form:visible input[type="image"]:visible, form:visible input[type="submit"]:visible, form:visible button[type="submit"]:visible'
    )
    if await generic_btn.count() > 0:
        return generic_btn.first
    return None


async def _fill(
    page: Page,
    expected_email: str,
    config: RegistrationFormConfig,
    *,
    clear_captcha: bool = False,
    focus_captcha: bool = True,
) -> Tuple[Locator, Locator, Locator, Locator]:
    id_marker = page.get_by_text(_ID).or_(page.get_by_label(_ID))
    candidate = page.locator("form:visible").filter(has=id_marker).filter(
        has=page.locator('input[type="password"]:visible')
    )
    expiry_text = page.get_by_text(_EXPIRY).and_(page.locator(":visible"))
    await candidate.or_(expiry_text).first.wait_for(state="visible")
    if await candidate.count() == 0 and await expiry_text.count():
        raise RegistrationFormError(STATUS_FORM_EXPIRED, "Ninja báo link xác nhận đã hết hiệu lực.")
    form = await _require_one(candidate, "form đăng ký Ninja")
    await _check_form_id(page, form, expected_email)

    password = await _require_one(
        _field(page, form, _PASSWORD, 'input[type="password"]:visible'), "ô mật khẩu"
    )
    confirmation = await _require_one(
        _field(page, form, _PASSWORD_CONFIRM, 'input[type="password"]:visible'), "ô xác nhận mật khẩu"
    )
    radio_label = re.compile("受信する" if config.newsletter else "受信しない")
    radio_selector = 'input[type="radio"]:visible'
    newsletter_fields = _rows(page, form, _NEWSLETTER).locator(radio_selector)
    labelled_radio = form.get_by_role("radio", name=radio_label).and_(page.locator(radio_selector))
    newsletter = await _named_choice(newsletter_fields.or_(labelled_radio), radio_label, "lựa chọn bản tin")
    checks = form.locator('input[type="checkbox"]:visible')
    terms = await _named_choice(checks, _TERMS, "ô đồng ý điều khoản")
    privacy = await _named_choice(checks, _PRIVACY, "ô đồng ý xử lý dữ liệu cá nhân")

    captcha_row = _rows(page, form, _CAPTCHA)
    captcha_images = captcha_row.locator(
        'td:visible img:visible:not([alt="必須"]):not(.img-hissu)'
    ).or_(
        form.get_by_role("img", name=re.compile("画像認証|CAPTCHA", re.I))
    )
    captcha_image = await _require_one(captcha_images, "ảnh CAPTCHA")
    captcha_inputs = captcha_row.locator(_CAPTCHA_INPUT_SELECTOR).or_(
        form.get_by_label(_CAPTCHA_INPUT).and_(page.locator(_CAPTCHA_INPUT_SELECTOR))
    )
    captcha_input = await _require_one(captcha_inputs, "ô nhập CAPTCHA")
    if clear_captcha and await captcha_input.is_editable() and await captcha_input.input_value():
        await captcha_input.fill("")
    if not await captcha_image.evaluate("img => img.decode().then(() => img.naturalWidth > 0)") or (
        not await captcha_input.is_editable() or await captcha_input.input_value()
    ):
        raise RegistrationFormError(STATUS_FORM_INVALID, "CAPTCHA chưa sẵn sàng để bàn giao.")

    await password.fill(config.password)
    await confirmation.fill(config.password)
    await newsletter.check()
    await terms.set_checked(config.accept_terms)
    await privacy.set_checked(config.accept_privacy)

    if (
        await password.input_value() != config.password
        or await confirmation.input_value() != config.password
        or not await newsletter.is_checked()
        or await terms.is_checked() != config.accept_terms
        or await privacy.is_checked() != config.accept_privacy
        or not await captcha_image.is_visible()
        or not await captcha_input.is_editable()
        or await captcha_input.input_value()
    ):
        raise RegistrationFormError(STATUS_FORM_INVALID, "Giá trị trên form chưa khớp cấu hình đăng ký.")
    if focus_captcha:
        await captcha_input.scroll_into_view_if_needed()
        await captcha_input.focus()
    return form, captcha_row, captcha_image, captcha_input


async def fill_confirmation_form(
    page: Page, expected_email: str, config: RegistrationFormConfig, timeout_ms: int
) -> None:
    """Xác nhận ID, điền form và để người dùng tiếp tục tại CAPTCHA."""
    if not validate_registration_password(config.password):
        raise RegistrationFormError(STATUS_FORM_INVALID, "Mật khẩu đăng ký sai định dạng.")
    try:
        await asyncio.wait_for(_fill(page, expected_email, config), timeout_ms / 1000)
    except (asyncio.TimeoutError, PlaywrightError):
        # Playwright logs may include the navigation URL or an input value.
        raise RegistrationFormError(STATUS_FORM_INVALID, "Không thể kiểm tra hoặc điền form xác nhận Ninja.") from None


async def prepare_confirmation_form(
    page: Page,
    expected_email: str,
    config: RegistrationFormConfig,
    timeout_ms: int,
    *,
    clear_captcha: bool = False,
    focus_captcha: bool = False,
) -> ConfirmationFormControls:
    """Điền form xác nhận và trả về các locator để tự giải CAPTCHA OCR và gửi form."""
    if not validate_registration_password(config.password):
        raise RegistrationFormError(STATUS_FORM_INVALID, "Mật khẩu đăng ký sai định dạng.")

    async def prepare() -> ConfirmationFormControls:
        form, captcha_row, captcha_image, captcha_input = await _fill(
            page,
            expected_email,
            config,
            clear_captcha=clear_captcha,
            focus_captcha=focus_captcha,
        )
        submit_button = await locate_submit_button(form)
        return ConfirmationFormControls(
            form=form,
            captcha_row=captcha_row,
            captcha_image=captcha_image,
            captcha_input=captcha_input,
            submit_button=submit_button,
        )

    try:
        return await asyncio.wait_for(prepare(), timeout_ms / 1000)
    except (asyncio.TimeoutError, PlaywrightError):
        raise RegistrationFormError(STATUS_FORM_INVALID, "Không thể kiểm tra hoặc điền form xác nhận Ninja.") from None


async def capture_captcha_image(
    page: Page,
    captcha_image: Locator,
    *,
    previous_bytes: Optional[bytes] = None,
    timeout_ms: int = 6000,
) -> Optional[bytes]:
    """Chụp ảnh PNG của vùng CAPTCHA khi ảnh đã tải xong và cập nhật."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, timeout_ms / 1000)
    change_deadline: Optional[float] = None
    image_bytes: Optional[bytes] = None

    while loop.time() < deadline:
        if page.is_closed():
            return None
        try:
            ready = await captcha_image.evaluate(
                "img => img.decode().then(() => img.naturalWidth > 0 && img.naturalHeight > 0)"
            )
            if ready:
                await captcha_image.scroll_into_view_if_needed(timeout=min(timeout_ms, 2000))
                shot = await captcha_image.screenshot(type="png", timeout=min(timeout_ms, 3000))
                if shot:
                    image_bytes = shot
                    if previous_bytes is None or shot != previous_bytes:
                        return shot
                    if change_deadline is None:
                        change_deadline = min(deadline, loop.time() + 2.0)
                    elif loop.time() >= change_deadline:
                        return image_bytes
        except PlaywrightError as exc:
            if "Execution context was destroyed" not in str(exc) and "Target closed" not in str(exc):
                return image_bytes
        await asyncio.sleep(0.25)

    return image_bytes


async def refresh_captcha_image(page: Page, captcha_row: Locator, timeout_ms: int = 5000) -> bool:
    """Bấm '読みにくいので画像変更' để đổi ảnh CAPTCHA mới nếu có."""
    links = captcha_row.locator("a:visible").filter(has_text=_REFRESH_CAPTCHA).or_(
        page.get_by_role("link", name=_REFRESH_CAPTCHA).and_(page.locator(":visible"))
    )
    if await links.count() == 0:
        return False
    target = links.first
    await target.scroll_into_view_if_needed(timeout=timeout_ms)
    await target.click(no_wait_after=True, timeout=timeout_ms)
    await asyncio.sleep(0.5)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 2000))
    except PlaywrightError:
        pass
    return True


async def _has_confirmation_form(page: Page) -> bool:
    id_marker = page.get_by_text(_ID).or_(page.get_by_label(_ID))
    pw_form = page.locator("form:visible").filter(has=id_marker).filter(
        has=page.locator('input[type="password"]:visible')
    )
    if await pw_form.count() > 0:
        return True
    captcha_row = _rows(page, page.locator("form:visible"), _CAPTCHA)
    if await captcha_row.count() > 0:
        return True
    return await page.locator('input[name="authimg"]:visible, .input-authimgInput:visible').count() > 0


async def inspect_registration_submission_state(
    page: Page,
    *,
    previous_time_origin: Optional[float] = None,
) -> Tuple[str, str]:
    """Phân loại trạng thái trang sau khi bấm gửi form xác nhận Ninja."""
    visible_error = False
    err_loc = page.locator(_ERROR_CONTAINERS)
    err_count = await err_loc.count()
    for idx in range(err_count):
        try:
            item = err_loc.nth(idx)
            if await item.is_visible() and (await item.inner_text(timeout=500)).strip():
                visible_error = True
                break
        except PlaywrightError:
            continue

    has_input_form = await _has_confirmation_form(page)
    expiry_visible = await page.get_by_text(_EXPIRY).and_(page.locator(":visible")).count() > 0
    if expiry_visible and not has_input_form:
        return "expired", "Ninja báo link xác nhận đã hết hiệu lực."

    has_captcha_err = await page.get_by_text(_CAPTCHA_ERROR).and_(page.locator(":visible")).count() > 0
    has_error = visible_error or has_captcha_err
    path = urlsplit(page.url).path.rstrip("/").lower()
    finish_marker = (
        await page.locator(".ttl-registFinish:visible, .btn-servicelist:visible").count() > 0
        or await page.get_by_text(_FINISH_MARKER).and_(page.locator(":visible")).count() > 0
    )

    if not has_error and not has_input_form and (
        path.startswith("/register/finish") or path.startswith("/register/complete") or finish_marker
    ):
        return "finished", "Đã hoàn tất đăng ký tài khoản Ninja."

    if not has_error and not has_input_form:
        has_send2 = await page.locator("#registerInputSend2 input:visible, #registerInputSend2 button:visible").count() > 0
        if (path.startswith("/register/confirm") or has_send2) and await locate_confirm_step_button(page) is not None:
            return "confirm_page", "Đang ở trang xác nhận cuối Ninja."

    if has_input_form:
        if has_error:
            return "retry", "Ninja báo mã CAPTCHA hoặc thông tin chưa khớp."
        if previous_time_origin is not None:
            current_origin = await page.evaluate("() => performance.timeOrigin")
            if current_origin != previous_time_origin:
                return "retry", "Trang đăng ký đã tải lại kèm CAPTCHA mới."
        captcha_inputs = _rows(page, page.locator("form:visible"), _CAPTCHA).locator(_CAPTCHA_INPUT_SELECTOR).or_(
            page.get_by_label(_CAPTCHA_INPUT).and_(page.locator(_CAPTCHA_INPUT_SELECTOR))
        )
        if await captcha_inputs.count() == 1 and not await captcha_inputs.first.input_value():
            return "retry", "Form giữ lại ô CAPTCHA trống để thử lại."
        return "pending", ""

    return "unknown", "Không nhận diện được trang kết quả sau khi gửi form."

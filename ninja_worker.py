import asyncio
import random
import re
import struct
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from urllib.parse import urlsplit
from playwright.async_api import async_playwright, Browser, Error, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from hiragana_hook import HiraganaHook, ManualHiraganaHook, OCRHiraganaHook
from ninja_hiragana import (
    MAKE_URL,
    OUTPUT_DIR,
    REFRESH_LINK,
    TRACK_IMAGES,
    on_make_page,
    watch_hiragana,
)

# ==============================================================================
# CÁC SELECTOR GIAO DIỆN NINJA.CO.JP
# ==============================================================================
SELECTORS = {
    # Bước 1: Trang https://www.ninja.co.jp/login/
    "login_url": "https://www.ninja.co.jp/login/",
    "login_email": 'input[name="email"], input[type="email"], #email',
    "login_password": "#password",
    "login_submit": '.btn-login input[type="image"], input[alt="ログイン"]',

    # Bước 2: Trang https://www.ninja.co.jp/home/blog/make
    "make_url": MAKE_URL,
    "blog_name": "#name",
    "subdomain": "#account",
    "domain_select": "#domain",
    "agree_limit": "#adult",
    "agree_terms": "#rule",
    "hiragana_image": "img#authimg:visible, #authimg img:visible",
    "hiragana_input": ".input-authimg input[type='text'], .input-authimg input, input[name='authimg'], input#authimg_input",
    "make_submit": 'input[type="submit"][value*="作成"], input[type="image"][alt*="作成"], button:has-text("作成する")',

    # Chuyển tiếp từ https://www.ninja.co.jp/home/ vào trang quản trị blog vừa tạo
    "home_url": "https://www.ninja.co.jp/home/",

    # Bước 3: Trang quản trị blog (/hoge/list.html) -> Soạn thảo (/hoge/NewEntry/)
    "write_blog_link": 'a:has-text("ブログを書く"), a[href*="NewEntry"]',
    "post_title": 'input[name="title"], input[placeholder*="タイトル"], input[type="text"]',
    "source_tab": 'a:has-text("ソースコード"), button:has-text("ソースコード"), li:has-text("ソースコード"), :text-is("ソースコード")',
    "post_body_textarea": 'textarea[name="body"], textarea:visible, textarea',
    "publish_button": 'button:has-text("公開する"), input[value*="公開する"], a:has-text("公開する"), :text-is("公開する")',

    # Bước 4: Link blog trên thanh header quản trị
    "blog_url_link": 'a:has-text("HTTPSで表示"), a[href*=".shinobi.jp"], a[target="_blank"][href^="http"]',
}

HUMAN_DELAY_MIN_SEC = 1.5
HUMAN_DELAY_MAX_SEC = 4.0

PUBLISHED_MESSAGE = re.compile(
    r"^(?:(?:(?:ブログ)?記事を)?(?:公開|投稿)しました|"
    r"(?:(?:ブログ)?記事の)?(?:公開|投稿)が完了しました)[。！!]?\s*$"
)
NOTICE_CONTAINER = ":is([role=status], [role=alert], .success, .notice, .message, .alert, .complete, .alert-success)"
PUBLICATION_UNCONFIRMED = "Cần kiểm tra xuất bản"
PUBLIC_POST_FOUND = "Đã thấy bài công khai"
MANUAL_REVIEW_SECONDS = 60
LOGIN_TIMEOUT_SECONDS = 20
LOGIN_HOME_SECONDS = 10
OVERVIEW_TIMEOUT_MS = 60_000
MAX_OCR_ATTEMPTS = 3
MANUAL_HIRAGANA_TIMEOUT = 60


class PublicationUnconfirmed(RuntimeError):
    """The editor did not provide a definitive publication outcome."""


class NeedsManualReview(RuntimeError):
    """Leave the current account's tabs available for the operator."""


async def login_home_stalled(page: Page) -> bool:
    """Wait for network idle or ten continuous seconds on the login landing page."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOGIN_TIMEOUT_SECONDS
    timeout = loop.create_future()
    timer = None
    was_home = None

    def track_location(frame):
        nonlocal timer, was_home
        if frame != page.main_frame or timeout.done():
            return
        at_home = frame.url == SELECTORS["home_url"]
        if at_home == was_home:
            return
        was_home = at_home
        if timer is not None:
            timer.cancel()
        when = loop.time() + LOGIN_HOME_SECONDS if at_home else deadline
        timer = loop.call_at(when, timeout.set_result, at_home)

    page.on("framenavigated", track_location)
    track_location(page.main_frame)
    idle = asyncio.create_task(page.wait_for_load_state("networkidle", timeout=0))
    try:
        done, _ = await asyncio.wait((idle, timeout), return_when=asyncio.FIRST_COMPLETED)
        if idle in done:
            await idle
            return False
        if timeout.result() and page.url == SELECTORS["home_url"]:
            return True
        raise NeedsManualReview("Sau 20 giây mạng vẫn hoạt động. Chưa xác nhận được kết quả đăng nhập.")
    finally:
        page.remove_listener("framenavigated", track_location)
        if timer is not None:
            timer.cancel()
        timeout.cancel()
        idle.cancel()
        await asyncio.gather(idle, return_exceptions=True)


async def public_post_matches(browser: Browser, blog_url: str, title: str, html: str) -> bool:
    """Check the public page once in a fresh, signed-out context."""
    if not blog_url or not title.strip():
        return False
    public_page = None
    try:
        public_page = await browser.new_page(java_script_enabled=False)
        response = await public_page.goto(blog_url, wait_until="domcontentloaded", timeout=10000)
        if not response or not response.ok or urlsplit(public_page.url).hostname != urlsplit(blog_url).hostname:
            return False
        titles = public_page.get_by_role("heading", name=title, exact=True).or_(
            public_page.locator(".entryList > li > a.entryListTitle").and_(
                public_page.get_by_role("link", name=title, exact=True)
            )
        )
        return await titles.evaluate_all(
            """(titles, html) => {
                const template = document.createElement('template');
                template.innerHTML = html;
                template.content.querySelectorAll('style, script, noscript').forEach(el => el.remove());
                const compact = text => text.normalize('NFC').replace(/\\s+/gu, '');
                const expected = compact(template.content.textContent);
                if (!expected) return false;
                return titles.some(el => {
                    if (el.matches('a.entryListTitle')) {
                        const body = el.parentElement.querySelector(':scope > .entryText .entryTextMain');
                        return !!body && compact(body.innerText) === expected;
                    }
                    if (compact(el.innerText).includes(expected)) return false;
                    const article = el.closest('article, [role=article], [itemprop=blogPost], .entry, .post');
                    return !!article && compact(article.innerText).includes(expected);
                });
            }""",
            html,
        )
    except Error:
        return False
    finally:
        if public_page is not None and not public_page.is_closed():
            try:
                await public_page.close()
            except Error:
                pass


async def publish_post(page: Page, timeout_ms: int = 20000) -> None:
    """Submit once and wait for an explicit publication receipt or form error."""
    receipt = page.get_by_text(PUBLISHED_MESSAGE).and_(
        page.locator(f"{NOTICE_CONTAINER}:visible, {NOTICE_CONTAINER} :visible")
    ).first
    errors = page.locator(
        ":is(.error, .errortext, .caution, .alert, .alert-danger, .alert-error, font[color=red]):visible"
    ).filter(has_text=re.compile(r"\S"), has_not_text=PUBLISHED_MESSAGE).first
    if await receipt.is_visible():
        raise PublicationUnconfirmed("Trang đã có thông báo xuất bản từ trước. Hãy kiểm tra danh sách bài trước khi gửi lại.")

    publish_button = page.locator(SELECTORS["publish_button"]).first
    try:
        await publish_button.click(timeout=timeout_ms)
        await receipt.or_(errors).first.wait_for(state="visible", timeout=timeout_ms)
        if await errors.is_visible():
            raise RuntimeError(f"Lỗi Shinobi khi xuất bản: {await errors.inner_text()}")
        if await receipt.is_visible():
            return
    except Error as exc:
        raise PublicationUnconfirmed(
            "Chưa xác nhận được bài đã xuất bản. Hãy kiểm tra danh sách bài trên Shinobi trước khi thử lại."
        ) from exc
    raise PublicationUnconfirmed("Thông báo xác nhận xuất bản không còn hiển thị. Hãy kiểm tra danh sách bài.")


@dataclass
class JobConfig:
    gmails: List[str]
    shared_password: str
    blog_name: str
    subdomain: str
    shared_title: str
    html_mode: int  # 1: Dùng chung | 2: Theo từng số thứ tự từ file TXT
    shared_html: str
    posts_map: Dict[int, Dict[str, str]]
    max_concurrent: int = 10


StatusCallback = Callable[[int, str, str, str, str], None]


class NinjaAutomationRunner:
    def __init__(
        self,
        config: JobConfig,
        hiragana_hook: HiraganaHook,
        on_status: StatusCallback,
        on_log: Callable[[str], None],
    ):
        self.config = config
        self.hiragana_hook = hiragana_hook
        self.on_status = on_status
        self.on_log = on_log
        self._stop_requested = False

    def request_stop(self):
        self._stop_requested = True

    async def _watch_hiragana(self, page: Page, idx: int):
        try:
            await watch_hiragana(page, OUTPUT_DIR, lambda message: self.on_log(f"[{idx}] {message}"))
        except (Error, OSError) as exc:
            self.on_log(f"[{idx}] Lỗi khi lưu ảnh Hiragana. {str(exc).splitlines()[0]}")

    async def _human_delay(
        self,
        page: Page,
        min_sec: float = HUMAN_DELAY_MIN_SEC,
        max_sec: float = HUMAN_DELAY_MAX_SEC,
    ):
        """Tạm dừng ngẫu nhiên (mặc định 1.5s - 4.0s) để mô phỏng nhịp thao tác tự nhiên."""
        delay_ms = int(random.uniform(min_sec, max_sec) * 1000)
        await page.wait_for_timeout(delay_ms)

    async def run(self):
        max_tabs = max(1, min(10, self.config.max_concurrent))
        semaphore = asyncio.Semaphore(max_tabs)

        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self.on_log(
                f"Đã khởi chạy 1 tiến trình Chromium (tối đa {max_tabs} phiên song song, "
                f"delay ngẫu nhiên {HUMAN_DELAY_MIN_SEC}–{HUMAN_DELAY_MAX_SEC}s)."
            )

            tasks = [
                self._process_account(browser, semaphore, idx, email)
                for idx, email in enumerate(self.config.gmails, start=1)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, res in enumerate(results, start=1):
                if isinstance(res, Exception):
                    email = self.config.gmails[i - 1]
                    self.on_status(i, email, "-", f"LỖI HỆ THỐNG: {res}", "")
                    self.on_log(f"[{i}] Ngoại lệ ngoài luồng xử lý ({email}): {res}")

            await browser.close()
            self.on_log("Đã đóng toàn bộ phiên trình duyệt.")

    async def _process_account(
        self,
        browser: Browser,
        semaphore: asyncio.Semaphore,
        idx: int,
        email: str,
    ):
        if self._stop_requested:
            self.on_status(idx, email, "-", "ĐÃ HỦY", "")
            return

        async with semaphore:
            if self._stop_requested:
                self.on_status(idx, email, "-", "ĐÃ HỦY", "")
                return

            context = None
            page: Page | None = None
            selected_domain = "-"
            hiragana_watcher = None

            try:
                # Dãn nhịp khởi động nhẹ để các tab không gửi request đồng loạt cùng 1 mili-giây
                await asyncio.sleep(random.uniform(0.5, 2.5))
                if self._stop_requested:
                    self.on_status(idx, email, "-", "ĐÃ HỦY", "")
                    return

                context = await browser.new_context()
                page = await context.new_page()

                # BƯỚC 1: Đăng nhập
                self.on_status(idx, email, selected_domain, "Đang đăng nhập...", "")
                await page.goto(SELECTORS["login_url"], wait_until="domcontentloaded", timeout=30000)
                await self._human_delay(page)

                await page.fill(SELECTORS["login_email"], email)
                await self._human_delay(page)

                await page.fill(SELECTORS["login_password"], self.config.shared_password)
                await self._human_delay(page)

                await page.locator(SELECTORS["login_submit"]).first.click()
                stalled_home = await login_home_stalled(page)

                if "login" in page.url.lower():
                    raise NeedsManualReview("Vẫn ở trang đăng nhập. Hãy xem thông báo của Shinobi trong tab.")

                if self._stop_requested:
                    self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                    return

                # BƯỚC 2: Khởi tạo blog và chờ nhập Hiragana
                self.on_status(idx, email, selected_domain, "Đang tạo blog...", "")
                if stalled_home:
                    self.on_log(f"[{idx}] Tab đã ở /home/ 10 giây. Đang mở trang tạo blog.")
                else:
                    await self._human_delay(page)
                await page.add_init_script(TRACK_IMAGES)
                if stalled_home and page.url != SELECTORS["home_url"]:
                    raise NeedsManualReview("Tab đã rời /home/ trước khi mở trang tạo blog.")
                await page.goto(SELECTORS["make_url"], wait_until="domcontentloaded", timeout=30000)
                hiragana_watcher = asyncio.create_task(self._watch_hiragana(page, idx))
                await self._human_delay(page)

                await page.fill(SELECTORS["blog_name"], self.config.blog_name)
                await self._human_delay(page)

                account_input = page.locator(SELECTORS["subdomain"])
                await account_input.fill(self.config.subdomain)

                # Kích hoạt onblur="getUsedDomain('account', 'domain', 'useddomain')"
                await account_input.blur()
                await self._human_delay(page)

                # Chọn tên miền tuần tự từ trên xuống (bỏ qua domain disabled)
                selected_domain = await self._select_domain_sequentially(page, idx)
                await self._human_delay(page)

                # Tick 2 ô điều khoản (#adult và #rule)
                for cb_key in ("agree_limit", "agree_terms"):
                    cb = page.locator(SELECTORS[cb_key]).first
                    if await cb.count() > 0 and not await cb.is_checked():
                        await cb.check()
                        await self._human_delay(page)

                self.on_status(idx, email, selected_domain, "Đang xử lý Hiragana...", "")
                blog_created = await self._process_hiragana_step(page, idx, email, selected_domain)
                if not blog_created:
                    raise RuntimeError("Chưa hoàn tất bước xác thực Hiragana để tạo blog.")

                if self._stop_requested:
                    self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                    return

                # BƯỚC 2.5: Từ trang /home/ bấm vào blog vừa tạo để vào trang quản trị
                self.on_status(idx, email, selected_domain, "Đang vào trang quản trị blog...", "")
                await self._human_delay(page)
                try:
                    page = await self._open_created_blog_dashboard(context, page, idx)
                except PlaywrightTimeoutError as exc:
                    raise NeedsManualReview("Hết thời gian chờ mở trang quản trị. Phiên chưa đến bước soạn bài.") from exc

                if self._stop_requested:
                    self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                    return

                # BƯỚC 3: Soạn thảo HTML Source & Xuất bản
                self.on_status(idx, email, selected_domain, "Đang tải trình soạn thảo (chờ 5s)...", "")
                await self._human_delay(page)
                write_btn = page.locator(SELECTORS["write_blog_link"]).first
                await write_btn.wait_for(state="visible", timeout=20000)
                await write_btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=20000)

                # Delay 5 giây để trang /hoge/NewEntry/ tải xong hoàn toàn trình soạn thảo HTML
                await page.wait_for_timeout(5000)

                if self._stop_requested:
                    self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                    return

                self.on_status(idx, email, selected_domain, "Đang đăng bài HTML...", "")
                post_title, post_html = self._get_post_payload(idx)
                await page.locator(SELECTORS["post_title"]).first.fill(post_title)
                await self._human_delay(page)

                # Chuyển sang tab ソースコード và chờ ngẫu nhiên để textarea HTML hiển thị
                source_tab = page.locator(SELECTORS["source_tab"]).first
                if await source_tab.count() > 0:
                    await source_tab.click()
                    await self._human_delay(page)

                body_box = page.locator(SELECTORS["post_body_textarea"]).first
                await body_box.wait_for(state="visible", timeout=15000)
                await body_box.fill(post_html)
                await self._human_delay(page)

                if self._stop_requested:
                    self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                    return

                try:
                    await publish_post(page)
                except PublicationUnconfirmed as exc:
                    status = PUBLICATION_UNCONFIRMED
                    detail = f"{exc} Tiêu đề: {post_title}"
                else:
                    status = "DONE"
                    detail = "Đã nhận xác nhận xuất bản."
                blog_url = await self._extract_blog_url(page, selected_domain)
                if status == PUBLICATION_UNCONFIRMED and await public_post_matches(browser, blog_url, post_title, post_html):
                    status = PUBLIC_POST_FOUND
                    detail = f"Đã đối chiếu tiêu đề và nội dung trên blog công khai: {post_title}."
                self.on_status(idx, email, selected_domain, status, blog_url)
                self.on_log(f"[{idx}] {detail} {blog_url}".rstrip())

            except NeedsManualReview as exc:
                if hiragana_watcher is not None:
                    hiragana_watcher.cancel()
                    await asyncio.gather(hiragana_watcher, return_exceptions=True)
                self.on_log(f"[{idx}] {exc}")
                self.on_status(idx, email, selected_domain, "Chờ kiểm tra trên Chromium", "")
                await self._review_tabs(context, idx)
                status = "ĐÃ HỦY" if self._stop_requested else f"LỖI: {exc}"
                self.on_status(idx, email, selected_domain, status, "")
            except Exception as exc:
                self.on_status(idx, email, selected_domain, f"LỖI: {exc}", "")
                self.on_log(f"[{idx}] Lỗi ({email}): {exc}")
            finally:
                if hiragana_watcher is not None and not hiragana_watcher.done():
                    hiragana_watcher.cancel()
                    await asyncio.gather(hiragana_watcher, return_exceptions=True)
                if page is not None and not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass

    async def _capture_hiragana_image(
        self,
        page: Page,
        idx: int,
        previous_bytes: Optional[bytes] = None,
    ) -> Optional[bytes]:
        """Tìm và chụp ảnh Hiragana (img#authimg), nếu ảnh quá nhỏ sẽ lấy từ bộ theo dõi."""
        known_img = page.locator("img#authimg:visible, #authimg img:visible").first
        xp_img = page.locator(f"xpath=//a[normalize-space(.)='{REFRESH_LINK}']/preceding::img[1]").first
        hiragana_img = known_img if (await known_img.count() > 0 and await known_img.is_visible()) else xp_img

        image_bytes = None
        if await hiragana_img.count() > 0:
            try:
                await page.wait_for_function(
                    "img => img && img.complete && (img.naturalHeight > 30 || img.clientHeight > 30)",
                    arg=await hiragana_img.element_handle(),
                    timeout=6000,
                )
            except Exception:
                pass
            try:
                await hiragana_img.scroll_into_view_if_needed()
                image_bytes = await hiragana_img.screenshot(type="png")
            except Exception:
                pass

        if image_bytes and len(image_bytes) > 24 and image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            _w, _h = struct.unpack(">II", image_bytes[16:24])
            if _h <= 30:
                self.on_log(f"[{idx}] Ảnh chụp bị quá nhỏ ({_w}x{_h}), đang lấy ảnh chuẩn từ bộ theo dõi...")
                recent_imgs = sorted(OUTPUT_DIR.glob("hiragana-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
                for r_img in recent_imgs[:5]:
                    data = r_img.read_bytes()
                    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
                        _rw, _rh = struct.unpack(">II", data[16:24])
                        if _rh > 30 and (previous_bytes is None or data != previous_bytes):
                            image_bytes = data
                            self.on_log(f"[{idx}] Đã lấy ảnh chuẩn ({_rw}x{_rh}) từ {r_img.name}")
                            break
        elif not image_bytes:
            recent_imgs = sorted(OUTPUT_DIR.glob("hiragana-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
            for r_img in recent_imgs[:5]:
                data = r_img.read_bytes()
                if previous_bytes is None or data != previous_bytes:
                    image_bytes = data
                    self.on_log(f"[{idx}] Lấy ảnh từ bộ theo dõi: {r_img.name}")
                    break

        return image_bytes

    async def _wait_for_make_submission(self, page: Page, timeout_sec: float = 15.0) -> bool:
        """
        Chờ phản hồi sau khi gửi biểu mẫu tạo blog:
        - Trả về True nếu thành công (URL đã chuyển trang khỏi /home/blog/make).
        - Trả về False nếu nhập sai Hiragana hoặc biểu mẫu có lỗi (vẫn ở make, input bị xóa hoặc hiện lỗi).
        """
        start_time = asyncio.get_running_loop().time()
        deadline = start_time + timeout_sec

        await asyncio.sleep(1.0)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass

        while asyncio.get_running_loop().time() < deadline:
            if page.is_closed():
                raise RuntimeError("Trang Chromium đã bị đóng.")

            # Nếu URL đã chuyển khỏi /home/blog/make -> Đã tạo blog thành công
            if not on_make_page(page):
                return True

            # Kiểm tra nếu ô input đã bị xóa rỗng sau khi web reload lỗi
            hiragana_input = page.locator(SELECTORS["hiragana_input"]).first
            if await hiragana_input.count() > 0:
                val = (await hiragana_input.input_value()).strip()
                if val == "":
                    return False

                err_loc = page.locator(
                    ":is(.error, .errortext, .caution, .alert, .alert-danger, .alert-error, font[color=red]):visible"
                ).first
                if await err_loc.count() > 0 and await err_loc.is_visible():
                    err_text = (await err_loc.inner_text()).strip()
                    if err_text:
                        return False

            await asyncio.sleep(0.5)

        return not on_make_page(page)

    async def _refresh_hiragana_image(self, page: Page, idx: int) -> None:
        """Bấm link 読みにくいので画像変更 để web đổi ảnh Hiragana mới."""
        try:
            refresh_link = page.locator(f"xpath=//a[normalize-space(.)='{REFRESH_LINK}']").first
            if await refresh_link.count() > 0 and await refresh_link.is_visible():
                self.on_log(f"[{idx}] Bấm '{REFRESH_LINK}' để đổi ảnh Hiragana mới...")
                await refresh_link.click()
                await page.wait_for_timeout(1000)
        except Exception as exc:
            self.on_log(f"[{idx}] Không thể bấm đổi ảnh: {exc}")

    async def _handle_manual_hiragana(
        self,
        page: Page,
        idx: int,
        email: str,
        selected_domain: str,
        timeout: float = float(MANUAL_HIRAGANA_TIMEOUT),
    ) -> bool:
        """
        Chờ người dùng nhập Hiragana thủ công trực tiếp trên Chromium
        hoặc bấm xác nhận trên giao diện App, với thời gian chờ tối đa (mặc định 60 giây).
        """
        hiragana_input = page.locator(SELECTORS["hiragana_input"]).first
        if await hiragana_input.count() > 0:
            try:
                await hiragana_input.focus()
            except Exception:
                pass

        self.on_status(idx, email, selected_domain, f"Chờ nhập tay Hiragana ({int(timeout)}s)", "")
        self.on_log(
            f"[{idx}] Vui lòng nhập Hiragana trên Chromium và bấm nút tạo blog ('作成する'). "
            f"Thời gian chờ tối đa {int(timeout)} giây..."
        )

        manual_hook = getattr(self.hiragana_hook, "fallback_hook", None) or self.hiragana_hook
        manual_task = None
        if isinstance(manual_hook, ManualHiraganaHook):
            manual_task = asyncio.create_task(manual_hook.solve(None, idx, email))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_reported_sec = int(timeout)

        try:
            while loop.time() < deadline:
                if self._stop_requested:
                    self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                    return False
                if page.is_closed():
                    raise RuntimeError("Đã đóng trang Chromium khi đang chờ nhập Hiragana.")

                # 1. Người dùng đã gửi thành công trên Chromium
                if not on_make_page(page):
                    self.on_log(f"[{idx}] Xác nhận tạo blog thành công qua nhập tay.")
                    return True

                # 2. Người dùng đã bấm nút xác nhận trên giao diện App
                if manual_task and manual_task.done():
                    await asyncio.sleep(1.0)
                    if not on_make_page(page):
                        self.on_log(f"[{idx}] Đã xác nhận gửi trên giao diện, tạo blog thành công.")
                        return True
                    if await hiragana_input.count() > 0 and await hiragana_input.is_visible():
                        val = (await hiragana_input.input_value()).strip()
                        if val:
                            self.on_log(f"[{idx}] Nhận diện ký tự '{val}' đã nhập, tự động bấm Tạo Blog...")
                            await page.locator(SELECTORS["make_submit"]).first.click()
                            if await self._wait_for_make_submission(page):
                                return True

                remaining_sec = max(0, int(deadline - loop.time()))
                if last_reported_sec - remaining_sec >= 10 and remaining_sec > 0:
                    last_reported_sec = remaining_sec
                    self.on_status(idx, email, selected_domain, f"Chờ nhập tay Hiragana ({remaining_sec}s)", "")

                await asyncio.sleep(0.5)

            self.on_log(f"[{idx}] Hết thời gian chờ nhập tay Hiragana ({int(timeout)}s).")
            raise RuntimeError(f"Hết thời gian chờ nhập tay Hiragana ({int(timeout)}s).")
        finally:
            if manual_task and not manual_task.done():
                manual_task.cancel()
                await asyncio.gather(manual_task, return_exceptions=True)

    async def _process_hiragana_step(
        self,
        page: Page,
        idx: int,
        email: str,
        selected_domain: str,
    ) -> bool:
        """
        Quy trình xác thực Hiragana:
        - Nếu dùng OCR (OCRHiraganaHook), thử OCR tối đa MAX_OCR_ATTEMPTS (3) lần.
        - Khi nhập sai, web tự đổi ảnh -> thử lại OCR với ảnh mới.
        - Nếu sau 3 lần OCR vẫn không được -> chuyển sang nhập tay (chờ 60s).
        """
        use_ocr = isinstance(self.hiragana_hook, OCRHiraganaHook)
        last_image_bytes: Optional[bytes] = None

        if use_ocr:
            for attempt in range(1, MAX_OCR_ATTEMPTS + 1):
                if self._stop_requested:
                    self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                    return False
                if page.is_closed():
                    raise RuntimeError("Đã đóng trang Chromium khi đang xử lý Hiragana.")

                self.on_status(idx, email, selected_domain, f"Đang giải OCR Hiragana ({attempt}/{MAX_OCR_ATTEMPTS})...", "")
                self.on_log(f"[{idx}] Thử giải OCR Hiragana lần {attempt}/{MAX_OCR_ATTEMPTS}...")

                # 1. Chụp ảnh Hiragana hiện tại
                image_bytes = await self._capture_hiragana_image(page, idx, previous_bytes=last_image_bytes)
                last_image_bytes = image_bytes

                # 2. Gọi OCR giải ảnh
                solved_text = await self.hiragana_hook.solve_ocr(image_bytes, idx, email)

                if solved_text:
                    self.on_log(f"[{idx}] OCR lần {attempt} nhận diện '{solved_text}'. Tự động điền và bấm nút Tạo Blog...")
                    hiragana_input = page.locator(SELECTORS["hiragana_input"]).first
                    await hiragana_input.fill(solved_text)
                    await self._human_delay(page)
                    await page.locator(SELECTORS["make_submit"]).first.click()

                    # 3. Kiểm tra kết quả submit
                    is_success = await self._wait_for_make_submission(page)
                    if is_success:
                        self.on_log(f"[{idx}] Tạo blog thành công ở lần thử OCR thứ {attempt}!")
                        return True

                    self.on_log(
                        f"[{idx}] Nhập sai Hiragana (lần {attempt}/{MAX_OCR_ATTEMPTS}). "
                        f"Web đã tự đổi ảnh mới."
                    )
                    await self._human_delay(page, min_sec=1.5, max_sec=2.5)
                else:
                    self.on_log(f"[{idx}] OCR lần {attempt}/{MAX_OCR_ATTEMPTS} không nhận diện được ảnh hợp lệ.")
                    if attempt < MAX_OCR_ATTEMPTS:
                        await self._refresh_hiragana_image(page, idx)
                        await self._human_delay(page, min_sec=1.5, max_sec=2.5)

            self.on_log(
                f"[{idx}] Đã thử OCR {MAX_OCR_ATTEMPTS} lần không thành công. "
                f"Chuyển sang nhập tay (thời gian chờ: {MANUAL_HIRAGANA_TIMEOUT}s)..."
            )
        else:
            self.on_log(f"[{idx}] Chế độ nhập tay Hiragana (thời gian chờ: {MANUAL_HIRAGANA_TIMEOUT}s)...")

        return await self._handle_manual_hiragana(
            page, idx, email, selected_domain, timeout=float(MANUAL_HIRAGANA_TIMEOUT)
        )

    async def _review_tabs(self, context, idx: int):
        if self._stop_requested or not context.pages:
            return
        self.on_log(f"[{idx}] Giữ tab tối đa {MANUAL_REVIEW_SECONDS} giây để kiểm tra thủ công. Đóng các tab của phiên để kết thúc sớm.")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MANUAL_REVIEW_SECONDS
        while context.pages and not self._stop_requested:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.25, remaining))

    async def _open_created_blog_dashboard(self, context, page: Page, idx: int) -> Page:
        if "/hoge/" in page.url:
            return page

        if page.url.rstrip("/") != SELECTORS["home_url"].rstrip("/"):
            await page.goto(SELECTORS["home_url"], wait_until="domcontentloaded", timeout=OVERVIEW_TIMEOUT_MS)
            await self._human_delay(page)

        blog_name = self.config.blog_name
        pages_before = len(context.pages)

        direct_link = page.get_by_role("link", name=blog_name, exact=True).last
        blog_menu = page.get_by_role("link", name="ブログ", exact=True).first
        if page.url == SELECTORS["home_url"]:
            ready = direct_link.or_(blog_menu).first
            try:
                await ready.wait_for(state="visible", timeout=OVERVIEW_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                if self._stop_requested or page.url != SELECTORS["home_url"]:
                    raise
                self.on_log(f"[{idx}] Trang tổng quan chưa hiện liên kết sau 60 giây. Đang tải lại một lần.")
                # GET the overview explicitly so Chromium cannot replay a form submission.
                await page.goto(SELECTORS["home_url"], wait_until="domcontentloaded", timeout=OVERVIEW_TIMEOUT_MS)
                if page.url != SELECTORS["home_url"]:
                    raise NeedsManualReview("Shinobi đã chuyển khỏi trang tổng quan sau khi tải lại.")
                await ready.wait_for(state="visible", timeout=OVERVIEW_TIMEOUT_MS)
        if self._stop_requested:
            return page
        if await direct_link.is_visible():
            await direct_link.click()
        else:
            await blog_menu.wait_for(state="visible", timeout=15000)
            await blog_menu.click()
            await self._human_delay(page)

            dropdown_link = page.get_by_role("link", name=blog_name, exact=True).first
            await dropdown_link.wait_for(state="visible", timeout=15000)
            await dropdown_link.click()

        await self._human_delay(page)
        if len(context.pages) > pages_before:
            target_page = context.pages[-1]
            await target_page.wait_for_load_state("domcontentloaded", timeout=20000)
            return target_page

        await page.wait_for_load_state("domcontentloaded", timeout=20000)
        return page

    async def _select_domain_sequentially(self, page: Page, idx: int) -> str:
        select_el = page.locator(SELECTORS["domain_select"]).first
        options = await select_el.locator("option").all()

        available_values: List[tuple[str, str]] = []
        for opt in options:
            val = (await opt.get_attribute("value")) or ""
            label = (await opt.inner_text()).strip()
            is_disabled = await opt.is_disabled()
            if not val or "選択" in label or is_disabled:
                continue
            available_values.append((val, label))

        if not available_values:
            raise RuntimeError("Không còn tên miền khả dụng nào cho subdomain này.")

        chosen_val, _chosen_label = available_values[(idx - 1) % len(available_values)]
        await select_el.select_option(value=chosen_val)
        return chosen_val.strip()

    def _get_post_payload(self, idx: int) -> tuple[str, str]:
        if self.config.html_mode == 1:
            return self.config.shared_title, self.config.shared_html
        item = self.config.posts_map[idx]
        title = item["title"] if item["title"] else self.config.shared_title
        return title, item["html"]

    async def _extract_blog_url(self, page: Page, selected_domain: str) -> str:
        subdomain = self.config.subdomain.lower()
        domain = selected_domain.lower().lstrip(".")
        if re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain):
            return f"https://{subdomain}.{domain}/"
        # Some domain options have a numeric value. Use the public link in the editor.
        try:
            links = await page.locator(SELECTORS["blog_url_link"] + ", a:has-text('ブログを見る'), a:has-text('公開ページ')").evaluate_all(
                "(links) => links.map(a => a.href)"
            )
        except Error:
            return ""
        for href in links:
            try:
                url = urlsplit(href)
                if url.scheme in ("https", "http") and (url.hostname or "").startswith(subdomain + "."):
                    return href
            except ValueError:
                continue
        return ""

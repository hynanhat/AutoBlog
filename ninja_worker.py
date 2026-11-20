import asyncio
import random
import re
import struct
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from playwright.async_api import async_playwright, Browser, Error, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from hiragana_hook import HiraganaHook
from ninja_hiragana import MAKE_URL, OUTPUT_DIR, TRACK_IMAGES, watch_hiragana

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
    "nav_blog_menu": 'header a:text-is("ブログ"), nav a:text-is("ブログ"), a:text-is("ブログ")',

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


async def publish_post(page: Page, timeout_ms: int = 20000) -> None:
    """Submit once and wait for an explicit publication receipt or form error."""
    receipt = page.get_by_text(PUBLISHED_MESSAGE).and_(
        page.locator(f"{NOTICE_CONTAINER}:visible, {NOTICE_CONTAINER} :visible")
    ).first
    errors = page.locator(
        ":is(.error, .errortext, .caution, .alert, .alert-danger, .alert-error, font[color=red]):visible"
    ).filter(has_text=re.compile(r"\S"), has_not_text=PUBLISHED_MESSAGE).first
    if await receipt.is_visible():
        raise RuntimeError("Trang đã có thông báo xuất bản từ trước. Hãy kiểm tra danh sách bài trước khi gửi lại.")

    publish_button = page.locator(SELECTORS["publish_button"]).first
    await publish_button.click(timeout=timeout_ms)
    try:
        await receipt.or_(errors).first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "Chưa xác nhận được bài đã xuất bản. Hãy kiểm tra danh sách bài trên Shinobi trước khi thử lại."
        ) from exc
    if await errors.is_visible():
        raise RuntimeError(f"Lỗi Shinobi khi xuất bản: {await errors.inner_text()}")
    if not await receipt.is_visible():
        raise RuntimeError("Thông báo xác nhận xuất bản không còn hiển thị. Hãy kiểm tra danh sách bài.")


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
                await page.wait_for_load_state("networkidle", timeout=20000)

                if "login" in page.url.lower():
                    raise RuntimeError("Đăng nhập thất bại (sai mật khẩu hoặc cần xác minh).")

                if self._stop_requested:
                    self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                    return

                # BƯỚC 2: Khởi tạo blog và chờ nhập Hiragana
                self.on_status(idx, email, selected_domain, "Đang tạo blog...", "")
                await self._human_delay(page)
                await page.add_init_script(TRACK_IMAGES)
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

                # 1. Tìm chính xác thẻ <img> chứa ảnh Hiragana
                known_img = page.locator("img#authimg:visible, #authimg img:visible").first
                xp_img = page.locator("xpath=//a[normalize-space(.)='読みにくいので画像変更']/preceding::img[1]").first
                hiragana_img = known_img if (await known_img.count() > 0 and await known_img.is_visible()) else xp_img

                image_bytes = None
                if await hiragana_img.count() > 0:
                    try:
                        # Đợi ảnh tải xong hoàn toàn và có chiều cao thực tế > 30px
                        await page.wait_for_function(
                            "img => img && img.complete && (img.naturalHeight > 30 || img.clientHeight > 30)",
                            arg=await hiragana_img.element_handle(),
                            timeout=6000,
                        )
                    except Exception:
                        pass
                    await hiragana_img.scroll_into_view_if_needed()
                    image_bytes = await hiragana_img.screenshot(type="png")

                # 2. Kiểm tra kích thước ảnh: nếu bị cắt nhỏ (chiều cao <= 30px), lấy ảnh gốc từ thư mục hiragana
                if image_bytes and len(image_bytes) > 24 and image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                    _w, _h = struct.unpack(">II", image_bytes[16:24])
                    if _h <= 30:
                        self.on_log(f"[{idx}] Ảnh chụp bị quá nhỏ ({_w}x{_h}), đang lấy ảnh chuẩn từ bộ theo dõi...")
                        recent_imgs = sorted(OUTPUT_DIR.glob("hiragana-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
                        for r_img in recent_imgs[:5]:
                            data = r_img.read_bytes()
                            if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
                                _rw, _rh = struct.unpack(">II", data[16:24])
                                if _rh > 30:
                                    image_bytes = data
                                    self.on_log(f"[{idx}] Đã lấy ảnh chuẩn ({_rw}x{_rh}) từ {r_img.name}")
                                    break
                elif not image_bytes:
                    recent_imgs = sorted(OUTPUT_DIR.glob("hiragana-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if recent_imgs:
                        image_bytes = recent_imgs[0].read_bytes()
                        self.on_log(f"[{idx}] Lấy ảnh từ bộ theo dõi: {recent_imgs[0].name}")

                hiragana_input = page.locator(SELECTORS["hiragana_input"]).first
                if await hiragana_input.count() > 0:
                    await hiragana_input.focus()

                confirmation = asyncio.create_task(self.hiragana_hook.solve(image_bytes, idx, email))
                try:
                    while not confirmation.done():
                        if self._stop_requested:
                            self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                            return
                        if page.is_closed():
                            raise RuntimeError("Đã đóng trang Chromium khi đang chờ nhập Hiragana.")
                        await asyncio.wait({confirmation}, timeout=0.3)
                    solved_text = confirmation.result()
                finally:
                    if not confirmation.done():
                        confirmation.cancel()
                        await asyncio.gather(confirmation, return_exceptions=True)
                if solved_text is not None:
                    self.on_log(f"[{idx}] OCR nhận diện '{solved_text}'. Tự động điền và bấm nút Tạo Blog...")
                    await hiragana_input.fill(solved_text)
                    await self._human_delay(page)
                    await page.locator(SELECTORS["make_submit"]).first.click()
                elif await hiragana_input.is_visible():
                    self.on_status(idx, email, selected_domain, "Chờ gửi trên Chromium", "")
                    self.on_log(
                        f"[{idx}] Biểu mẫu Hiragana vẫn đang mở. Kiểm tra thông báo trên Chromium, "
                        "nhập ký tự và bấm nút tạo blog (作成する)."
                    )
                    while await hiragana_input.is_visible():
                        if self._stop_requested:
                            self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                            return
                        if page.is_closed():
                            raise RuntimeError("Đã đóng trang Chromium khi đang chờ gửi biểu mẫu.")
                        await asyncio.sleep(0.3)

                await page.wait_for_load_state("networkidle", timeout=25000)

                if self._stop_requested:
                    self.on_status(idx, email, selected_domain, "ĐÃ HỦY", "")
                    return

                # BƯỚC 2.5: Từ trang /home/ bấm vào blog vừa tạo để vào trang quản trị
                self.on_status(idx, email, selected_domain, "Đang vào trang quản trị blog...", "")
                await self._human_delay(page)
                page = await self._open_created_blog_dashboard(context, page)

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

                await publish_post(page)

                # BƯỚC 4: Trích xuất link blog
                blog_url = await self._extract_blog_url(page, selected_domain)
                self.on_status(idx, email, selected_domain, "DONE", blog_url)
                self.on_log(f"[{idx}] Thành công: {email} -> {blog_url}")

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

    async def _open_created_blog_dashboard(self, context, page: Page) -> Page:
        if "/hoge/" in page.url:
            return page

        if "ninja.co.jp/home" not in page.url:
            await page.goto(SELECTORS["home_url"], wait_until="domcontentloaded", timeout=20000)
            await self._human_delay(page)

        blog_name = self.config.blog_name
        pages_before = len(context.pages)

        direct_link = page.locator(f'a:text-is("{blog_name}")').last
        clicked = False
        if await direct_link.count() > 0 and await direct_link.is_visible():
            await direct_link.click()
            clicked = True
        else:
            blog_menu = page.locator(SELECTORS["nav_blog_menu"]).first
            await blog_menu.wait_for(state="visible", timeout=15000)
            await blog_menu.click()
            await self._human_delay(page)

            dropdown_link = page.locator(f'a:text-is("{blog_name}")').first
            await dropdown_link.wait_for(state="visible", timeout=15000)
            await dropdown_link.click()
            clicked = True

        if not clicked:
            raise RuntimeError(f"Không tìm thấy blog '{blog_name}' trên trang /home/.")

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
        link_el = page.locator(SELECTORS["blog_url_link"]).first
        if await link_el.count() > 0 and await link_el.is_visible():
            href = await link_el.get_attribute("href")
            if href and href.startswith("http"):
                return href.strip()

        view_link = page.locator("a:has-text('ブログを見る'), a:has-text('公開ページ')").first
        if await view_link.count() > 0:
            href = await view_link.get_attribute("href")
            if href and href.startswith("http") and "admin" not in href:
                return href.strip()

        if selected_domain and selected_domain != "-":
            return f"https://{self.config.subdomain}.{selected_domain}/"
        return page.url

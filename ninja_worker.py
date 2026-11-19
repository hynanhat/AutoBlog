import asyncio
import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from playwright.async_api import async_playwright, Browser, Page

from captcha_hook import CaptchaHook

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
    "make_url": "https://www.ninja.co.jp/home/blog/make",
    "blog_name": "#name",
    "subdomain": "#account",
    "domain_select": "#domain",
    "agree_limit": "#adult",
    "agree_terms": "#rule",
    "captcha_image": "#authimg img, #authimg",
    "captcha_input": ".input-authimg input[type='text'], .input-authimg input",
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
        captcha_hook: CaptchaHook,
        on_status: StatusCallback,
        on_log: Callable[[str], None],
    ):
        self.config = config
        self.captcha_hook = captcha_hook
        self.on_status = on_status
        self.on_log = on_log
        self._stop_requested = False

    def request_stop(self):
        self._stop_requested = True

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
            await asyncio.gather(*tasks, return_exceptions=True)

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

            # Dãn nhịp khởi động nhẹ để các tab không gửi request đồng loạt cùng 1 mili-giây
            await asyncio.sleep(random.uniform(0.5, 2.5))

            context = await browser.new_context()
            page: Page = await context.new_page()
            selected_domain = "-"

            try:
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

                # BƯỚC 2: Khởi tạo Blog & Cổng CAPTCHA
                self.on_status(idx, email, selected_domain, "Đang tạo blog...", "")
                await self._human_delay(page)
                await page.goto(SELECTORS["make_url"], wait_until="domcontentloaded", timeout=30000)
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

                self.on_status(idx, email, selected_domain, "Chờ giải CAPTCHA...", "")

                # Cuộn tới vùng CAPTCHA (#authimg) để người dùng nhìn rõ
                captcha_img = page.locator(SELECTORS["captcha_image"]).first
                image_bytes = None
                if await captcha_img.count() > 0:
                    await captcha_img.scroll_into_view_if_needed()
                    image_bytes = await captcha_img.screenshot()

                captcha_input = page.locator(SELECTORS["captcha_input"]).first
                if await captcha_input.count() > 0:
                    await captcha_input.focus()

                # Gọi cổng CaptchaHook (Manual hoặc OCR riêng ở Giai đoạn 4)
                solved_text = await self.captcha_hook.solve(image_bytes, idx, email)
                if solved_text is not None:
                    await captcha_input.fill(solved_text)
                    await self._human_delay(page)
                    await page.locator(SELECTORS["make_submit"]).first.click()

                await page.wait_for_load_state("networkidle", timeout=25000)

                # BƯỚC 2.5: Từ trang /home/ bấm vào blog vừa tạo để vào trang quản trị
                self.on_status(idx, email, selected_domain, "Đang vào trang quản trị blog...", "")
                await self._human_delay(page)
                page = await self._open_created_blog_dashboard(context, page)

                # BƯỚC 3: Soạn thảo HTML Source & Xuất bản
                self.on_status(idx, email, selected_domain, "Đang tải trình soạn thảo (chờ 5s)...", "")
                await self._human_delay(page)
                write_btn = page.locator(SELECTORS["write_blog_link"]).first
                await write_btn.wait_for(state="visible", timeout=20000)
                await write_btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=20000)

                # Delay 5 giây để trang /hoge/NewEntry/ tải xong hoàn toàn trình soạn thảo HTML
                await page.wait_for_timeout(5000)

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

                publish_btn = page.locator(SELECTORS["publish_button"]).first
                await publish_btn.scroll_into_view_if_needed()
                await publish_btn.wait_for(state="visible", timeout=15000)
                await publish_btn.click()
                await page.wait_for_load_state("networkidle", timeout=20000)

                # BƯỚC 4: Trích xuất link blog
                blog_url = await self._extract_blog_url(page, selected_domain)
                self.on_status(idx, email, selected_domain, "DONE", blog_url)
                self.on_log(f"[{idx}] Thành công: {email} -> {blog_url}")

            except Exception as exc:
                self.on_status(idx, email, selected_domain, f"LỖI: {exc}", "")
                self.on_log(f"[{idx}] Lỗi ({email}): {exc}")
            finally:
                await context.close()

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
        if await link_el.count() > 0:
            href = await link_el.get_attribute("href")
            if href:
                return href.strip()
        if selected_domain and selected_domain != "-":
            return f"https://{self.config.subdomain}.{selected_domain}/"
        return page.url
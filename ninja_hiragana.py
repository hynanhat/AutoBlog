"""Lưu ảnh Hiragana hiển thị trên trang tạo blog Ninja."""

import asyncio
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error, Page, TimeoutError, async_playwright


MAKE_URL = "https://www.ninja.co.jp/home/blog/make"
REFRESH_LINK = "読みにくいので画像変更"
OUTPUT_DIR = Path("hiragana")
READ_IMAGE_STATE = "img => window.ninjaHiraganaImageState(img)"

# Ghi nhận mỗi lần ảnh tải xong, kể cả khi địa chỉ ảnh được dùng lại.
TRACK_IMAGES = """
(() => {
    const revisions = new WeakMap();
    let revision = 0;
    window.ninjaHiraganaImageState = (image) => {
        if (!image.complete || image.naturalWidth === 0 || !image.currentSrc) {
            return null;
        }
        if (!revisions.has(image)) revisions.set(image, ++revision);
        return [performance.timeOrigin, image.currentSrc, revisions.get(image)];
    };
    document.addEventListener("load", (event) => {
        if (event.target instanceof HTMLImageElement) {
            revisions.set(event.target, ++revision);
        }
    }, true);
})();
"""


def on_make_page(page: Page) -> bool:
    address = urlsplit(page.url)
    return (
        address.scheme == "https"
        and address.netloc == "www.ninja.co.jp"
        and address.path.rstrip("/") == "/home/blog/make"
    )


def save_png(output_dir: Path, png: bytes) -> Path:
    timestamp = datetime.now()
    while True:
        name = timestamp.strftime("hiragana-%Y%m%d-%H%M%S-%f.png")
        destination = output_dir / name
        try:
            with destination.open("xb") as output:
                output.write(png)
            return destination.resolve()
        except FileExistsError:
            timestamp += timedelta(microseconds=1)


async def watch_hiragana(
    page: Page,
    output_dir: Path,
    on_log: Callable[[str], None] = print,
) -> None:
    refresh_xpath = f"xpath=//a[normalize-space(.)='{REFRESH_LINK}']"
    refresh_link = page.locator(refresh_xpath).first
    known_image = page.locator("img#authimg:visible, #authimg img:visible").first
    preceding_image = page.locator(f"{refresh_xpath}/preceding::img[1]").first
    last_state = None
    saved_images = set()
    output_dir.mkdir(parents=True, exist_ok=True)

    while not page.is_closed():
        if on_make_page(page):
            try:
                if await refresh_link.is_visible():
                    image = known_image if await known_image.is_visible() else preceding_image
                    if await image.is_visible():
                        state = await image.evaluate(READ_IMAGE_STATE, timeout=1000)
                        if state is not None and state != last_state:
                            png = await image.screenshot(type="png", timeout=2000)
                            current_state = await image.evaluate(READ_IMAGE_STATE, timeout=1000)
                            if state == current_state:
                                fingerprint = sha256(png).digest()
                                if fingerprint not in saved_images:
                                    destination = save_png(output_dir, png)
                                    saved_images.add(fingerprint)
                                    on_log(f"Đã lưu ảnh Hiragana tại {destination}")
                                last_state = state
            except TimeoutError:
                # Ảnh có thể vừa được thay thế trong lúc chụp.
                pass
            except Error as error:
                if page.is_closed() or "Target page, context or browser has been closed" in str(error):
                    break
                if "Execution context was destroyed" not in str(error):
                    raise

        await asyncio.sleep(0.3)


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        try:
            page = await browser.new_page()
            await page.add_init_script(TRACK_IMAGES)
            print("Hãy đăng nhập Ninja trong Chromium. Ảnh Hiragana sẽ được lưu tự động.", flush=True)
            print("Bạn tự đọc và nhập Hiragana. Đóng Chromium hoặc nhấn Ctrl+C để dừng.", flush=True)
            try:
                await page.goto(MAKE_URL, wait_until="domcontentloaded")
                await watch_hiragana(page, OUTPUT_DIR, lambda message: print(message, flush=True))
            except Error:
                if browser.is_connected() and not page.is_closed():
                    raise
        finally:
            if browser.is_connected():
                await browser.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nĐã dừng chương trình lưu ảnh Hiragana.")
    except (Error, OSError) as error:
        raise SystemExit(f"Lỗi khi chạy chương trình lưu ảnh Hiragana. {str(error).splitlines()[0]}") from None

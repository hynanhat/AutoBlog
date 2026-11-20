import codecs
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from playwright.async_api import async_playwright

from ninja_worker import publish_post
from parsers import parse_posts_txt


class PostImportTests(unittest.TestCase):
    def test_supported_encodings_preserve_markup_and_entities(self):
        source = "=== [BLOG 1] ===\nTITLE: Tiêu đề\n<p>A &amp; B</p>"
        with TemporaryDirectory() as folder:
            path = Path(folder) / "posts.txt"
            for encoding, bom in (
                ("utf-8", b""), ("utf-8", codecs.BOM_UTF8),
                ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
            ):
                with self.subTest(encoding=encoding, bom=bom):
                    path.write_bytes(bom + source.encode(encoding))
                    posts, errors = parse_posts_txt(str(path))
                    self.assertEqual(errors, [])
                    self.assertEqual(posts[1], {"title": "Tiêu đề", "html": "<p>A &amp; B</p>"})

    def test_unreadable_input_returns_errors_for_the_gui(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "posts.txt"
            for data in (None, b"\x81\xff", codecs.BOM_UTF16_LE + b"A"):
                with self.subTest(data=data):
                    if data is not None:
                        path.write_bytes(data)
                    posts, errors = parse_posts_txt(str(path))
                    self.assertEqual(posts, {})
                    self.assertTrue(errors)

    def test_sample_contains_html_elements(self):
        path = Path(__file__).resolve().parents[1] / "posts_sample.txt"
        posts, errors = parse_posts_txt(str(path))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(posts), [1, 2, 3])
        for post in posts.values():
            self.assertIn("<p>", post["html"])


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        browser = await playwright.chromium.launch()
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()

    async def test_editor_navigation_and_draft_are_not_receipts(self):
        await self.page.set_content(
            "<button>公開する</button><a>ブログを見る</a>"
            "<textarea>投稿しました</textarea><div role='status'>保存しました</div>"
        )
        with self.assertRaisesRegex(RuntimeError, "Chưa xác nhận"):
            await publish_post(self.page, timeout_ms=500)

    async def test_waits_for_explicit_receipt(self):
        await self.page.set_content(
            """<button onclick="setTimeout(() => document.querySelector('span').textContent='記事を公開しました。', 80)">
            公開する</button><div role="status"><span></span></div>"""
        )
        await publish_post(self.page, timeout_ms=3000)

    async def test_visible_error_is_reported(self):
        await self.page.set_content(
            """<button onclick="document.querySelector('.error').hidden=false">公開する</button>
            <p class="error" hidden>入力エラー</p>"""
        )
        with self.assertRaisesRegex(RuntimeError, "入力エラー"):
            await publish_post(self.page, timeout_ms=3000)

    async def test_existing_receipt_does_not_trigger_another_submit(self):
        await self.page.set_content(
            """<button onclick="window.clicked=true">公開する</button><p role="status">投稿しました。</p>"""
        )
        with self.assertRaisesRegex(RuntimeError, "từ trước"):
            await publish_post(self.page)
        self.assertFalse(await self.page.evaluate("Boolean(window.clicked)"))

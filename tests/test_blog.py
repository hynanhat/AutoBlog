import asyncio
import codecs
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

from ninja_worker import NeedsManualReview, NinjaAutomationRunner, PublicationUnconfirmed, SELECTORS, login_home_stalled, public_post_matches, publish_post
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

    def test_title_and_whitespace_in_multiline_blocks(self):
        source = (
            "=== [BLOG 1] ===\n \n  title: A: B  \n\n <pre>\n  x\n\n</pre> \n"
            "=== [BLOG 2] ===\n  <p>No title</p>\nTITLE: in body\n"
            "=== [BLOG 3] ===\nTITLE: Empty\n   "
        )
        with TemporaryDirectory() as folder:
            path = Path(folder) / "posts.txt"
            path.write_text(source, encoding="utf-8")
            posts, errors = parse_posts_txt(str(path))
        self.assertEqual(posts[1], {"title": "A: B", "html": "<pre>\n  x\n\n</pre>"})
        self.assertEqual(posts[2], {"title": "", "html": "<p>No title</p>\nTITLE: in body"})
        self.assertEqual(posts[3], {"title": "Empty", "html": ""})
        self.assertEqual(errors, ["[BLOG 3] đang bị trống nội dung HTML."])

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
        self.browser = browser
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()

    async def test_busy_home_after_login_uses_its_own_deadline(self):
        home = "http://portal.test/home/"
        await self.page.route("http://portal.test/**", lambda route: route.fulfill(
            content_type="text/html", body="<p>Signing in</p>"
        ))
        await self.page.goto("http://portal.test/login/")
        with patch.dict(SELECTORS, home_url=home), patch("ninja_worker.LOGIN_HOME_SECONDS", 0.2):
            wait = asyncio.create_task(login_home_stalled(self.page))
            try:
                started = asyncio.get_running_loop().time()
                await self.page.evaluate("history.pushState(null, '', '/home/')")
                self.assertTrue(await asyncio.wait_for(wait, 2))
                self.assertGreaterEqual(asyncio.get_running_loop().time() - started, 0.18)
            finally:
                wait.cancel()
                await asyncio.gather(wait, return_exceptions=True)

    async def test_leaving_home_cancels_login_fallback(self):
        home = "http://portal.test/home/"
        await self.page.route(home, lambda route: route.fulfill(content_type="text/html", body="<p>Home</p>"))
        await self.page.goto(home)
        with patch.dict(SELECTORS, home_url=home), patch("ninja_worker.LOGIN_HOME_SECONDS", 0.2), \
                patch("ninja_worker.LOGIN_TIMEOUT_SECONDS", 0.3):
            await self.page.evaluate("setTimeout(() => history.pushState(null, '', '/login/'), 50)")
            with self.assertRaises(NeedsManualReview):
                await login_home_stalled(self.page)

    async def test_idle_home_continues_without_fallback(self):
        home = "http://portal.test/home/"
        await self.page.route(home, lambda route: route.fulfill(body="Home", content_type="text/html"))
        await self.page.goto(home, wait_until="networkidle")
        with patch.dict(SELECTORS, home_url=home):
            self.assertFalse(await login_home_stalled(self.page))

    async def test_slow_overview_is_refreshed_once_by_get(self):
        home = "http://portal.test/home/"
        runner = NinjaAutomationRunner(SimpleNamespace(blog_name="My blog"), Mock(), Mock(), Mock())
        runner._human_delay = AsyncMock()
        methods = []

        async def overview(route):
            methods.append(route.request.method)
            html = "<p>Loading</p>" if len(methods) == 1 else '<a href="/dashboard">My blog</a>'
            await route.fulfill(content_type="text/html", body=html)

        await self.page.route(home, overview)
        await self.page.route("http://portal.test/dashboard", lambda route: route.fulfill(body="Dashboard"))
        await self.page.goto(home)
        with patch.dict(SELECTORS, home_url=home), patch("ninja_worker.OVERVIEW_TIMEOUT_MS", 500):
            result = await runner._open_created_blog_dashboard(self.page.context, self.page, 1)
        self.assertEqual(methods, ["GET", "GET"])
        self.assertEqual(result.url, "http://portal.test/dashboard")

    async def test_overview_redirect_to_login_stops_after_one_refresh(self):
        runner = NinjaAutomationRunner(SimpleNamespace(blog_name="My blog"), Mock(), Mock(), Mock())
        requests = []

        async def serve(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")
            path = request.split()[1]
            response = b"200 OK"
            if path == b"/home/" and path in requests:
                response = b"302 Found\r\nLocation: /login"
            requests.append(path)
            writer.write(b"HTTP/1.1 " + response + b"\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n<p>Wait</p>")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        async with server:
            origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            home = origin + "/home/"
            await self.page.goto(home)
            with patch.dict(SELECTORS, home_url=home), patch("ninja_worker.OVERVIEW_TIMEOUT_MS", 500):
                with self.assertRaises(NeedsManualReview):
                    await runner._open_created_blog_dashboard(self.page.context, self.page, 1)
            self.assertEqual(self.page.url, origin + "/login")
            self.assertEqual(requests.count(b"/home/"), 2)

    async def test_editor_navigation_and_draft_are_not_receipts(self):
        await self.page.set_content(
            "<button>公開する</button><a>ブログを見る</a>"
            "<textarea>投稿しました</textarea><div role='status'>保存しました</div>"
        )
        with self.assertRaisesRegex(PublicationUnconfirmed, "Chưa xác nhận"):
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

    async def test_public_verification_requires_title_and_body_in_same_post(self):
        document = (
            b"<article><h2>My title</h2><p>A &amp; B</p></article>"
            b"<article><h2>Older</h2><p>Other text</p></article>"
            b"<ul class=entryList><li><a class=entryListTitle href='/entry'>Blog title</a>"
            b"<div class=entryText><div class=entryTextMain><p>Body without heading</p></div></div></li></ul>"
        )

        async def serve(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n" + document)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        async with server:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
            for title, html, expected in [
                ("My title", "<p>A &amp; B</p>", True),
                ("My title", "<p>Other text</p>", False),
                ("Unknown", "<p>A &amp; B</p>", False),
                ("My title", "<p>My title</p>", False),
                ("Blog title", "<p>Body without heading</p>", True),
                ("Blog title", "<p>A &amp; B</p>", False),
            ]:
                with self.subTest(title=title, html=html):
                    self.assertEqual(await public_post_matches(self.browser, url, title, html), expected)

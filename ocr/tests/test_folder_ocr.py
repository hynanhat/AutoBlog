"""Folder-to-HTTP acceptance checks with a local Gemini endpoint."""

import base64
import sys
import time
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

import cv2
from starlette.testclient import TestClient

try:
    from folder_ocr import JobStore
    from formatter import normalize_hiragana
    from ocr_service import OCRService
    from preprocess import denoise_hiragana, read_image
    from server import create_app
except ImportError:
    from ocr.folder_ocr import JobStore
    from ocr.formatter import normalize_hiragana
    from ocr.ocr_service import OCRService
    from ocr.preprocess import denoise_hiragana, read_image
    from ocr.server import create_app

if __package__:
    from .test_ocr import SAMPLE, TEXT, gemini_fixture
else:
    from test_ocr import SAMPLE, TEXT, gemini_fixture


def wait_items(client, path, count):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(path)
        response.raise_for_status()
        page = response.json()
        if len(page["items"]) == count:
            return page
        time.sleep(0.03)
    raise AssertionError(f"Timed out waiting for {count} items on {path}")


class FolderTests(unittest.TestCase):
    def test_ten_concurrent_clean_images_and_result_cursor(self):
        image = read_image(SAMPLE)
        inputs, replies, expected = {}, {}, {}
        for index, char in enumerate("あいうえおかきくけこ"):
            name = f"ảnh {index}.PNG"
            source = cv2.copyMakeBorder(image, 0, 0, index, 0, cv2.BORDER_REPLICATE)
            clean, _ = denoise_hiragana(source)
            digest = sha256(cv2.imencode(".png", clean)[1]).digest()
            replies[digest] = char + "っがぇぴゅ"
            expected[name] = normalize_hiragana(replies[digest])
            inputs[name] = cv2.imencode(".png", source)[1].tobytes()

        first = next(iter(replies))
        received = set()
        lock, all_started, release_first, release_rest = (
            Lock(),
            Event(),
            Event(),
            Event(),
        )

        def reply(body):
            png = base64.urlsafe_b64decode(
                body["contents"][0]["parts"][1]["inlineData"]["data"]
            )
            digest = sha256(png).digest()
            with lock:
                received.add(digest)
                if len(received) == 10:
                    all_started.set()
            gate = release_first if digest == first else release_rest
            gate.wait(timeout=20)
            return replies[digest]

        with TemporaryDirectory() as temp, gemini_fixture([reply] * 10) as calls:
            inbox = Path(temp) / "inbox"
            app = create_app(inbox, OCRService("test", delay=0), interval=0.03)
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").json()["workers"], 10)
                try:
                    for name, data in inputs.items():
                        (inbox / name).write_bytes(data)
                    self.assertTrue(all_started.wait(timeout=15))
                    jobs = client.get("/jobs").json()["items"]
                    self.assertEqual([j["status"] for j in jobs], ["processing"] * 10)
                    self.assertEqual(client.get("/results").json()["items"], [])
                    release_rest.set()
                    wait_items(client, "/results", 9)
                    early = client.get("/results?limit=4").json()
                    later = client.get(f"/results?after={early['next_cursor']}").json()
                    release_first.set()
                    last = wait_items(
                        client, f"/results?after={later['next_cursor']}", 1
                    )
                finally:
                    release_rest.set()
                    release_first.set()
                rows = early["items"] + later["items"] + last["items"]
                self.assertEqual([r["cursor"] for r in rows], list(range(1, 11)))
                self.assertEqual(last["items"][0]["filename"], next(iter(inputs)))
                self.assertEqual(
                    {r["filename"]: r["result"]["normalized_text"] for r in rows},
                    expected,
                )
                self.assertEqual(client.get(f"/jobs/{rows[0]['id']}").json(), rows[0])
                self.assertEqual(
                    client.get("/results?after=10").json(),
                    {"items": [], "next_cursor": 10},
                )
                self.assertEqual(len(calls), 10)

    def test_bad_image_replacement_and_restart(self):
        with TemporaryDirectory() as temp, gemini_fixture([TEXT, TEXT]) as calls:
            inbox = Path(temp)
            path = inbox / "文字.jpg"
            path.write_bytes(b"broken")
            (inbox / "writing.part").write_bytes(b"partial")
            with TestClient(
                create_app(inbox, OCRService("test", delay=0), interval=0.03)
            ) as client:
                failed = wait_items(client, "/results", 1)["items"][0]
                self.assertEqual(failed["status"], "failed")
                self.assertIsNone(failed["result"])
                self.assertTrue(failed["error"])
                path.write_bytes(cv2.imencode(".jpg", read_image(SAMPLE))[1].tobytes())
                rows = wait_items(client, "/results", 2)["items"]
                self.assertEqual(rows[1]["result"]["text"], TEXT)
                self.assertEqual(len(calls), 1)
            # Simulate a stop after claiming a job whose source has been removed.
            store = JobStore(inbox / ".ocr-state" / "jobs.sqlite3")
            store.enqueue("saved.png", "test-stamp", SAMPLE.read_bytes())
            self.assertEqual(len(store.claim(1)), 1)
            with TestClient(
                create_app(inbox, OCRService("test", delay=0), interval=0.03)
            ) as client:
                rows = wait_items(client, "/results", 3)["items"]
                self.assertEqual(rows[-1]["filename"], "saved.png")
                self.assertEqual(rows[-1]["result"]["normalized_text"], TEXT)
                self.assertEqual(len(client.get("/jobs").json()["items"]), 3)
                self.assertEqual(client.get("/jobs/9999").status_code, 404)
                for query in ("limit=0", "after=-1", "limit=abc", f"after={2**64}"):
                    self.assertEqual(client.get("/results?" + query).status_code, 400)
                self.assertEqual(len(calls), 2)

    def test_rate_limit_retry_hint(self):
        quota = {
            "error": {
                "code": 429,
                "message": "busy",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "31.5s",
                    },
                ],
            }
        }
        with (
            gemini_fixture([quota, TEXT]),
            OCRService("test", delay=0) as service,
            patch("ocr_service.time.sleep") as pause,
        ):
            result = service.recognize(read_image(SAMPLE))
            self.assertEqual(result["text"], TEXT)
            pause.assert_any_call(31.5)

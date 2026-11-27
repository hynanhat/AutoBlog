import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ocr.folder_ocr import JobStore, file_stamp
from ocr_cleanup import cleanup_ocr_images, cleanup_preference_file, save_cleanup_preference


class OcrCleanupTests(unittest.TestCase):
    def test_only_removes_captures_and_finished_auto_blog_inputs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            captures = root / "hiragana"
            captures.mkdir()
            archived = captures / "hiragana-20261001-123456-123456.png"
            archived.write_bytes(b"archive")
            user_copy = captures / "hand-picked.png"
            user_copy.write_bytes(b"user image")

            inbox = root / "ocr" / "inbox"
            state = inbox / ".ocr-state"
            state.mkdir(parents=True)
            store = JobStore(state / "jobs.sqlite3")
            store.initialize()

            done, failed, changed, processing, queued, unscanned = (
                inbox / f"ninja-acc-{index}-1234-abcd1234.png" for index in range(1, 7)
            )
            imported = inbox / "outside-image.png"
            unfinished_write = inbox / "ninja-acc-7-1234-abcd1234.png.part"
            for path in (done, failed, changed, processing, queued, unscanned, imported, unfinished_write):
                path.write_bytes(b"sample")

            for path in (done, failed, imported, changed, processing, queued):
                store.enqueue(path.name, file_stamp(path.stat()), path.read_bytes())
            ids = [row["id"] for row in store.claim(5)]
            store.finish(ids[0], {"text": "sample"}, None)
            store.finish(ids[1], None, "test failure")
            store.finish(ids[2], {"text": "sample"}, None)
            store.finish(ids[3], {"text": "sample"}, None)
            changed.write_bytes(b"replacement content")

            cleanup_ocr_images(root)
            cleanup_ocr_images(root)

            for path in (archived, done, failed):
                self.assertFalse(path.exists(), path.name)
            for path in (user_copy, changed, processing, queued, unscanned, imported, unfinished_write, store.path):
                self.assertTrue(path.is_file(), path.name)
            self.assertEqual(store.get(ids[0])["status"], "done")
            self.assertEqual(store.get(ids[1])["status"], "failed")
            self.assertEqual(store.get(ids[4])["status"], "processing")

    def test_cleanup_preference_persists_without_using_working_tree(self):
        with TemporaryDirectory() as folder, patch.dict("os.environ", {"LOCALAPPDATA": folder}):
            self.assertFalse(cleanup_preference_file().is_file())
            save_cleanup_preference(True)
            self.assertTrue(cleanup_preference_file().is_file())
            save_cleanup_preference(False)
            self.assertFalse(cleanup_preference_file().is_file())

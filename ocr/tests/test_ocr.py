"""Offline acceptance checks using the real SDK against a local HTTP fixture."""

import json
import os
import sys
import unicodedata
import unittest
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

import numpy as np
import pandas as pd
import streamlit as st
from google import genai
from PIL import Image
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

try:
    from evaluate import align, load_labels, main, summarize
    from formatter import Reading, Transcription, format_reading, normalize_hiragana
    from ocr_service import OCRService, ReadingCache, ordered_map
    from preprocess import denoise_hiragana, read_image, write_image
except ImportError:
    from ocr.evaluate import align, load_labels, main, summarize
    from ocr.formatter import Reading, Transcription, format_reading, normalize_hiragana
    from ocr.ocr_service import OCRService, ReadingCache, ordered_map
    from ocr.preprocess import denoise_hiragana, read_image, write_image

SAMPLE = ROOT / "data/samples/sample-01.png"
TEXT = "はひしぬせさ"


@contextmanager
def gemini_fixture(replies):
    queue = deque(replies)
    calls = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            reply = queue.popleft() if queue else 400
            if callable(reply):
                reply = reply(body)
            status = reply if isinstance(reply, int) else 200
            if isinstance(reply, dict) and "error" in reply:
                status = reply["error"]["code"]
            if status == 200:
                reading = {
                    "raw_hiragana": reply,
                    "confidence_note": "test",
                }
                data = {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": json.dumps(reading)}],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 31,
                        "candidatesTokenCount": 20,
                        "totalTokenCount": 51,
                    },
                }
            else:
                data = {"error": {"code": status, "message": "simulated API failure"}}
            if isinstance(reply, dict):
                data = reply
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_type = genai.Client

    def local_client(**options):
        options["http_options"].base_url = f"http://127.0.0.1:{server.server_port}"
        return client_type(**options)

    try:
        with (
            patch("ocr_service.genai.Client", side_effect=local_client),
            patch.dict(os.environ, {"GEMINI_API_KEY": "local-test-key"}),
        ):
            yield calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class OCRTests(unittest.TestCase):
    def test_normalize_hiragana(self):
        for voiced, base in zip(
            "がぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽゔ",
            "かきくけこさしすせそたちつてとはひふへほはひふへほう",
            strict=True,
        ):
            decomposed = unicodedata.normalize("NFD", voiced)
            spacing = "゜" if decomposed.endswith("\u309a") else "゛"
            for form in (voiced, decomposed, base + spacing):
                with self.subTest(form=form):
                    self.assertEqual(normalize_hiragana(form), base)
        for source, expected in [
            ("ぁぃぅぇぉっゃゅょゎゕゖ", "あいうえおつやゆよわかけ"),
            ("はひじぬせさ", TEXT),
            ("ちつぇゅぅさ", "ちつえゆうさ"),
            ("", ""),
            ("゛゜\u3099\u309a", ""),
            ("1ッゞゃ゜。", "ゝや"),
        ]:
            with self.subTest(source=source):
                self.assertEqual(normalize_hiragana(source), expected)
                self.assertEqual(normalize_hiragana(expected), expected)

    def test_preprocessing_and_format(self):
        image = read_image(SAMPLE)
        for mode in ("manual", "auto"):
            clean, stages = denoise_hiragana(image, mode=mode)
            self.assertEqual(len(stages), 6)
            self.assertEqual(set(clean.flatten()), {0, 255})
            with TemporaryDirectory() as folder:
                path = Path(folder) / "ảnh sạch あ.png"
                write_image(path, clean)
                self.assertTrue((read_image(path)[:, :, 0] == clean).all())
        reading = format_reading(
            Reading(raw_hiragana=" は1ひ。", romaji=["ha", "hi"], confidence_note="")
        )
        self.assertEqual(reading["text"], "はひ")
        self.assertEqual(reading["characters"][1]["romaji"], "hi")
        self.assertTrue(reading["warnings"])

    def test_metrics_and_labels(self):
        for truth, prediction, expected in [
            ("abc", "ac", 1),
            ("abc", "abbc", 1),
            ("abc", "adc", 1),
            ("", "aa", 2),
        ]:
            self.assertEqual(align(truth, prediction)[0], expected)
        rows = [
            {"ground_truth": "あいう", "original": "あう", "clean": "あいう"},
            {"ground_truth": "あいう", "original": "あえう", "clean": "あいう"},
        ]
        metrics, pairs = summarize(rows)
        self.assertAlmostEqual(metrics[0]["cer"], 2 / 6)
        self.assertEqual([r["exact_match"] for r in metrics], [0, 1])
        self.assertEqual(
            pairs,
            [
                {
                    "variant": "original",
                    "reference": "い",
                    "prediction": "え",
                    "count": 1,
                }
            ],
        )
        with self.assertRaises(ValueError):
            load_labels(BytesIO(f"filename,ground_truth\nx,{TEXT}\nx,{TEXT}".encode()))

    def test_fast_denoise(self):
        image = read_image(SAMPLE)
        for mode in ("auto", "manual"):
            for kernel, area in ((1, 1), (3, 9), (5, 1)):
                options = {"mode": mode, "morph_kernel": kernel, "min_area": area}
                full, stages = denoise_hiragana(image, **options)
                fast, skipped = denoise_hiragana(image, include_stages=False, **options)
                np.testing.assert_array_equal(fast, full)
                self.assertEqual(len(stages), 6)
                self.assertEqual(skipped, {})

    def test_local_romaji(self):
        reading = format_reading(
            Transcription(raw_hiragana="きゃかゞぢっゔゟ", confidence_note="")
        )
        self.assertEqual(
            [c["romaji"] for c in reading["characters"]],
            ["ki", "ya", "ka", "ga", "ji", "tsu", "vu", "yori"],
        )
        self.assertTrue(reading["warnings"])

    def test_parallel_cache_and_resolution(self):
        image = read_image(SAMPLE)
        cache = ReadingCache()
        model = "models/gemini-3.1-flash-lite"
        with (
            gemini_fixture([{}, TEXT, TEXT, TEXT, TEXT]) as calls,
            patch("ocr_service.CACHE_LIMIT", 1),
        ):
            with OCRService("test", model, 0, cache=cache) as service:
                with self.assertRaisesRegex(ValueError, "rỗng"):
                    service.recognize(image)
                rows = list(ordered_map(service.recognize, [image, image.copy()], 2))
                self.assertEqual(rows[0], rows[1])
                self.assertEqual(len(calls), 2)
                self.assertEqual(
                    calls[1][1]["generationConfig"]["mediaResolution"],
                    "MEDIA_RESOLUTION_MEDIUM",
                )
                larger = np.tile(image, (1, 2, 1))
                service.recognize(larger)
                self.assertNotIn("mediaResolution", calls[2][1]["generationConfig"])
                service.recognize(image)
                self.assertEqual(service.usage.cache_hits, 1)
                self.assertEqual(service.usage.responses_with_usage, 3)
            with OCRService("test", "other-model", 0, cache=cache) as other:
                other.recognize(image)
                self.assertNotIn("thinkingConfig", calls[-1][1]["generationConfig"])
            self.assertEqual(len(calls), 5)

    def test_concurrent_order(self):
        last_started = Event()

        def read(index):
            if index == 0:
                self.assertTrue(last_started.wait(timeout=4))
            if index == 2:
                last_started.set()
            return index

        self.assertEqual(list(ordered_map(read, range(3), 2)), [0, 1, 2])

    def test_workers_share_request_spacing(self):
        image = read_image(SAMPLE)
        with (
            gemini_fixture([TEXT, TEXT]) as calls,
            OCRService("test", delay=1) as service,
            patch("ocr_service.time") as clock,
        ):
            clock.monotonic.return_value = 100
            list(ordered_map(service.recognize, [image, image[:, 1:]], 2))
            clock.sleep.assert_called_once_with(1)
            self.assertEqual(len(calls), 2)

    def test_api(self):
        with (
            gemini_fixture([429, TEXT, 403]) as calls,
            OCRService("test", delay=0) as service,
        ):
            with patch("ocr_service.time.sleep") as wait:
                self.assertEqual(service.recognize(read_image(SAMPLE))["text"], TEXT)
                wait.assert_any_call(2)
                self.assertEqual(service.recognize(read_image(SAMPLE))["text"], TEXT)
                self.assertEqual(service.usage.cache_hits, 1)
                with self.assertRaisesRegex(RuntimeError, "403"):
                    service.recognize(read_image(SAMPLE)[:, 1:])
            self.assertEqual(len(calls), 3)
            route, body = calls[0]
            self.assertIn("gemini-2.5-flash", route)
            cfg = body["generationConfig"]
            self.assertEqual(cfg["temperature"], 0)
            self.assertEqual(cfg["responseMimeType"], "application/json")
            self.assertEqual(
                set(cfg["responseSchema"]["properties"]),
                {"raw_hiragana", "confidence_note"},
            )
            thinking = cfg["thinkingConfig"]
            self.assertEqual(
                thinking.get("thinkingBudget", thinking.get("thinking_budget")), 0
            )
            self.assertTrue(body["contents"][0]["parts"][1]["inlineData"]["data"])

    def test_cli(self):
        truths = list(load_labels(ROOT / "data/labels.csv").values())
        replies = [v for truth in truths for v in (truth[:-1], truth)]
        with (
            TemporaryDirectory() as directory,
            gemini_fixture(replies) as calls,
            patch.dict(os.environ, {"GEMINI_MODEL": "fixture-flash"}),
        ):
            main(
                [
                    "--labels",
                    str(ROOT / "data/labels.csv"),
                    "--images",
                    str(SAMPLE.parent),
                    "--out",
                    directory,
                    "--delay",
                    "0",
                    "--workers",
                    "1",
                ]
            )
            self.assertEqual(len(calls), 2 * len(truths))
            self.assertTrue(all("fixture-flash" in route for route, _ in calls))
            folder = Path(directory)
            usage = json.loads((folder / "performance.json").read_text())
            self.assertEqual(usage["api_requests"], 6)
            self.assertEqual(usage["reported_total_tokens"], 6 * 51)
            self.assertEqual(usage["responses_with_usage"], 6)
            metrics = pd.read_csv(folder / "metrics.csv")
            self.assertEqual(metrics.model.tolist(), ["fixture-flash"] * 2)
            self.assertAlmostEqual(metrics.cer[0], 1 / 6)
            self.assertEqual(metrics.exact_match.tolist(), [0, 1])
            self.assertEqual(len(pd.read_csv(folder / "predictions.csv")), len(truths))
            self.assertTrue(pd.read_csv(folder / "confusions.csv").empty)
            with Image.open(folder / "comparison.png") as graph:
                self.assertGreater(graph.info["dpi"][0], 299)

    def test_evaluation_upload(self):
        at = AppTest.from_file(str(ROOT / "ocr_ui.py"), default_timeout=60)
        with gemini_fixture([TEXT, TEXT]) as calls:
            at.run()
            next(r for r in at.radio if r.label == "Dữ liệu đánh giá").set_value(
                "Tải dữ liệu"
            ).run()
            labels = next(
                u for u in at.file_uploader if u.label == "Nhãn đối chứng (CSV)"
            )
            labels.upload(
                "labels.csv",
                f"filename,ground_truth\nảnh.png,{TEXT}\n".encode(),
                "text/csv",
            )
            images = next(u for u in at.file_uploader if u.label == "Ảnh đối chứng")
            images.upload("wrong.png", SAMPLE.read_bytes(), "image/png").run()
            self.assertTrue(any("Thiếu ảnh" in e.value for e in at.error))
            images = next(u for u in at.file_uploader if u.label == "Ảnh đối chứng")
            images.upload("ảnh.png", SAMPLE.read_bytes(), "image/png").run()
            at.button(key="run_eval").click().run()
            self.assertFalse(at.exception)
            self.assertEqual(at.session_state["eval_result"][1][0]["clean"], TEXT)
            self.assertEqual(len(calls), 2)

    def test_streamlit(self):
        at = AppTest.from_file(str(ROOT / "ocr_ui.py"), default_timeout=60)
        source = "はひじぬせさ"
        with (
            gemini_fixture([source] * 9) as calls,
            patch("streamlit.download_button", wraps=st.download_button) as downloads,
        ):
            at.run()
            at.number_input[0].set_value(0.0)
            at.file_uploader[0].upload("ảnh.png", SAMPLE.read_bytes(), "image/png")
            at.sidebar.radio[0].set_value("Manual").run()
            at.button(key="read_clean").click().run()
            self.assertTrue(at.checkbox(key="single_normalized_view").value)
            self.assertEqual(at.code[0].value, "はひしぬせさ")
            self.assertEqual(
                at.session_state["single_result"][1]["clean"]["text"], source
            )
            self.assertEqual(len(calls), 1)
            at.button(key="read_clean").click().run()
            self.assertEqual(len(calls), 1)
            at.button(key="read_original").click().run()
            self.assertEqual(len(calls), 2)
            next(r for r in at.radio if r.label == "Nguồn ảnh").set_value(
                "Thư mục"
            ).run()
            at.file_uploader[1].upload("bộ/ảnh.png", SAMPLE.read_bytes(), "image/png")
            at.file_uploader[1].upload("bộ/lỗi.png", b"bad", "image/png").run()
            next(b for b in at.button if b.label == "Nhận diện hàng loạt").click().run()
            rows = at.session_state["batch_result"][1]
            self.assertEqual(rows[0]["text"], source)
            self.assertTrue(rows[1]["error"])
            self.assertEqual(len(calls), 2)
            at.button(key="run_eval").click().run()
            self.assertEqual(len(at.session_state["eval_result"][1]), 3)
            self.assertEqual(len(at.metric), 4)
            self.assertFalse(at.exception)
            self.assertEqual(len(calls), 6)
            scores = [m.value for m in at.metric]
            token, readings = at.session_state["single_result"]
            at.session_state["single_result"] = (
                token,
                {**readings, "original": {"text": source}},
            )
            for enabled in (True, False, True):
                downloads.reset_mock()
                for key in (
                    "single_normalized_view",
                    "normalize_batch",
                    "normalize_eval",
                ):
                    at.checkbox(key=key).set_value(enabled)
                at.run()
                self.assertFalse(at.exception)
                self.assertEqual([m.value for m in at.metric], scores)
                payloads = {
                    call.args[2]: call.args[1] for call in downloads.call_args_list
                }
                single = json.loads(payloads["ocr.json"])
                batch = json.loads(payloads["batch.json"])
                batch_csv = pd.read_csv(
                    BytesIO(payloads["batch.csv"]), keep_default_na=False
                )
                evaluation = pd.read_csv(BytesIO(payloads["predictions.csv"]))
                self.assertEqual(single["original"]["text"], source)
                self.assertEqual(batch[1]["error"], rows[1]["error"])
                self.assertEqual(
                    evaluation.ground_truth.tolist(),
                    list(load_labels(ROOT / "data/labels.csv").values()),
                )
                self.assertEqual(evaluation.clean.tolist(), [source] * 3)
                for variant in ("original", "clean"):
                    self.assertEqual("normalized_text" in single[variant], enabled)
                    if enabled:
                        self.assertEqual(single[variant]["normalized_text"], TEXT)
                self.assertEqual("normalized_text" in batch[0], enabled)
                self.assertEqual("normalized_text" in batch_csv, enabled)
                self.assertEqual("original_normalized" in evaluation, enabled)
                self.assertEqual("clean_normalized" in evaluation, enabled)
                self.assertEqual(
                    payloads["ocr.txt"].count("(chuẩn hóa): " + TEXT),
                    2 if enabled else 0,
                )
                if enabled:
                    self.assertEqual(batch[0]["normalized_text"], TEXT)
                    self.assertIsNone(batch[1]["normalized_text"])
                    self.assertEqual(batch_csv.normalized_text.tolist(), [TEXT, ""])
                    self.assertEqual(
                        evaluation.original_normalized.tolist(), [TEXT] * 3
                    )
                    self.assertEqual(evaluation.clean_normalized.tolist(), [TEXT] * 3)
                    self.assertIn(
                        "Romaji của kết quả OCR", [c.value for c in at.caption]
                    )
                self.assertNotIn(
                    "normalized_text", at.session_state["single_result"][1]["clean"]
                )
                self.assertEqual(len(calls), 6)
            next(
                b for b in at.get("download_button") if b.label == "Tải JSON"
            ).click().run()
            self.assertFalse(at.exception)
            self.assertEqual(len(calls), 6)
            at.button(key="run_eval").click().run()
            self.assertEqual(len(calls), 6)
            at.button(key="clear_ocr").click().run()
            self.assertNotIn("eval_result", at.session_state)
            at.button(key="read_clean").click().run()
            self.assertEqual(len(calls), 7)

    def test_interactive_timeout_preserves_first_reading(self):
        at = AppTest.from_file(str(ROOT / "ocr_ui.py"), default_timeout=60)
        with gemini_fixture([TEXT, 504, TEXT]) as calls:
            at.run()
            at.button(key="read_clean").click().run()
            at.button(key="read_original").click().run()
            self.assertEqual(len(calls), 2)
            self.assertTrue(any("504" in error.value for error in at.error))
            self.assertIn(TEXT, [code.value for code in at.code])
            self.assertEqual(set(at.session_state["single_result"][1]), {"clean"})
            at.button(key="read_original").click().run()
            self.assertEqual(len(calls), 3)
            self.assertEqual(
                set(at.session_state["single_result"][1]), {"clean", "original"}
            )

    def test_paste_without_key(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}),
            patch(
                "ocr_service.OCRService.__init__", side_effect=AssertionError
            ) as service,
        ):
            at = AppTest.from_file(str(ROOT / "ocr_ui.py"), default_timeout=60).run()
            for source, expected in [
                ("はひじぬせさ", TEXT),
                ("ちつぇゅぅさ", "ちつえゆうさ"),
                ("っゃゎゕゖ", "つやわかけ"),
                ("゛゜", ""),
            ]:
                at.text_input(key="paste_hiragana").set_value(source).run()
                self.assertFalse(at.exception)
                self.assertIn(expected, [c.value for c in at.code])
                if len(expected) != 6:
                    self.assertTrue(
                        any(f"{len(expected)}/6" in w.value for w in at.warning)
                    )
            service.assert_not_called()


if __name__ == "__main__":
    unittest.main()

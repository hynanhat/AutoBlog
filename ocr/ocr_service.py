"""Gemini OCR for a single local image per request."""

import math
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from hashlib import sha256
from itertools import islice
from threading import Condition, Lock

import cv2
import numpy as np
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from config import DEFAULT_MODEL, MAX_WORKERS
from formatter import Transcription, format_reading

PROMPT = (
    "Copy visible random hiragana in order, including small kana and voicing. "
    "No corrections or guesses. Note only uncertainty, briefly in Vietnamese; else empty."
)
CACHE_LIMIT = 256


def rate_limit_wait(exc, attempt):
    """Read Google's RetryInfo and the HTTP Retry-After header, in seconds."""
    header = getattr(exc.response, "headers", {}).get("Retry-After", "")
    waits = [2 ** (attempt + 1)]
    if header:
        try:
            waits.append(float(header))
        except ValueError:
            try:
                waits.append(parsedate_to_datetime(header).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    if isinstance(exc.details, dict):
        error = exc.details.get("error", exc.details)
        for detail in error.get("details", []) if isinstance(error, dict) else []:
            if isinstance(detail, dict) and "retryDelay" in detail:
                try:
                    waits.append(float(str(detail["retryDelay"]).removesuffix("s")))
                except ValueError:
                    pass
    if len(waits) == 1 and attempt == 2:
        waits.append(60)
    return max(seconds for seconds in waits if math.isfinite(seconds))


def ordered_map(function, items, workers):
    """Limit concurrent calls and yield results in input order."""
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"Số luồng OCR phải từ 1 đến {MAX_WORKERS}.")
    if workers == 1:
        yield from map(function, items)
        return
    indexed = enumerate(items)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {
            executor.submit(function, item): index
            for index, item in islice(indexed, workers)
        }
        ready = {}
        next_row = 0
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                ready[pending.pop(future)] = future.result()
            for index, item in islice(indexed, len(finished)):
                pending[executor.submit(function, item)] = index
            while next_row in ready:
                yield ready.pop(next_row)
                next_row += 1


class ReadingCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.pending = set()
        self.condition = Condition()

    def get_or_read(self, key, read):
        with self.condition:
            self.condition.wait_for(lambda: key not in self.pending)
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key], True
            self.pending.add(key)
        try:
            reading = read()
            with self.condition:
                self.entries[key] = reading
                if len(self.entries) > CACHE_LIMIT:
                    self.entries.popitem(last=False)
            return reading, False
        finally:
            with self.condition:
                self.pending.remove(key)
                self.condition.notify_all()


@dataclass
class Usage:
    api_requests: int = 0
    cache_hits: int = 0
    responses_with_usage: int = 0
    reported_total_tokens: int = 0


class OCRService:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        delay: float = 1.0,
        *,
        cache=None,
    ):
        if not api_key or not api_key.strip():
            raise ValueError("Cần nhập GEMINI_API_KEY để chạy OCR.")
        if not model.strip() or delay < 0:
            raise ValueError("Tên model phải khác rỗng và thời gian chờ phải không âm.")
        self._api_key = api_key.strip()
        self._client = None
        self.model = model.strip()
        self.delay = delay
        self.last_call = None
        self._resume_at = 0.0
        self.cache = cache if cache is not None else ReadingCache()
        self._start_lock = Lock()
        self._usage_lock = Lock()
        self.usage = Usage()

    @property
    def client(self):
        if self._client is None:
            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(
                    timeout=60000, retry_options=types.HttpRetryOptions(attempts=1)
                ),
            )
        return self._client

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._client is not None:
            self._client.close()
            self._client = None

    def _config(self, shape, interactive):
        model_id = self.model.removeprefix("models/")
        options = {}
        if model_id == "gemini-3.1-flash-lite" and max(shape[:2]) <= 384:
            options["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
        if model_id == "gemini-2.5-flash":
            options["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(
            http_options=types.HttpOptions(timeout=45000) if interactive else None,
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=Transcription,
            **options,
        )

    def recognize(self, image, *, interactive=False) -> dict:
        key = (
            self.model,
            image.shape,
            image.dtype.str,
            sha256(np.ascontiguousarray(image)).digest(),
        )
        reading, hit = self.cache.get_or_read(
            key, lambda: self._request(image, interactive)
        )
        if hit:
            with self._usage_lock:
                self.usage.cache_hits += 1
        return format_reading(reading)

    def _request(self, image, interactive):
        ok, data = cv2.imencode(".png", image)
        if not ok:
            raise ValueError("Không thể mã hóa ảnh để nhận diện.")
        part = types.Part.from_bytes(data=data.tobytes(), mime_type="image/png")
        config = self._config(image.shape, interactive)
        attempts = 1 if interactive else 4
        for attempt in range(attempts):
            with self._start_lock:
                client = self.client
                if self.last_call is not None:
                    wait = (
                        max(self.last_call + self.delay, self._resume_at)
                        - time.monotonic()
                    )
                    if wait > 0:
                        time.sleep(wait)
                self.last_call = time.monotonic()
                self.usage.api_requests += 1
            try:
                response = client.models.generate_content(
                    model=self.model,
                    contents=[PROMPT, part],
                    config=config,
                )
            except errors.APIError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                    pause = (
                        rate_limit_wait(exc, attempt)
                        if exc.code == 429
                        else 2 ** (attempt + 1)
                    )
                    if pause <= 120:
                        if exc.code == 429:
                            with self._start_lock:
                                self._resume_at = max(
                                    self._resume_at, time.monotonic() + pause
                                )
                        time.sleep(pause)
                        continue
                if exc.code == 404:
                    raise RuntimeError(
                        f"Gemini không tìm thấy model {self.model} cho tài khoản này (HTTP 404). "
                        "Chọn model khác ở thanh bên hoặc bằng --model."
                    ) from exc
                if exc.code == 504:
                    raise RuntimeError(
                        f"Gemini hết thời gian xử lý với {self.model} (HTTP 504). Bạn có thể thử lại."
                    ) from exc
                raise RuntimeError(
                    f"Gemini trả lỗi HTTP {exc.code}. Kiểm tra model, khóa và hạn mức."
                ) from exc
            usage = response.usage_metadata
            if usage is not None and usage.total_token_count is not None:
                with self._usage_lock:
                    self.usage.responses_with_usage += 1
                    self.usage.reported_total_tokens += usage.total_token_count
            if not response.text:
                raise ValueError("Gemini trả về phản hồi rỗng.")
            try:
                return Transcription.model_validate_json(response.text)
            except ValidationError as exc:
                raise ValueError("Phản hồi Gemini không đúng cấu trúc OCR.") from exc

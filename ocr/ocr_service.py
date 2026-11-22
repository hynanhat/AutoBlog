"""Gemini OCR for a single local image per request."""

import logging
import math
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from hashlib import sha256
from itertools import islice
from threading import Condition, Lock
from typing import Optional

import cv2
import numpy as np
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from config import DEFAULT_MODEL, MAX_WORKERS
from formatter import Transcription, format_reading
from key_pool import APIKeyPool, DEFAULT_MODELS, load_keys_from_sources, mask_key

LOG = logging.getLogger("ocr_service")

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
            if reading is None:
                raise RuntimeError("Dịch vụ OCR không trả về kết quả.")

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
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        delay: float = 1.0,
        *,
        cache=None,
        key_pool: Optional[APIKeyPool] = None,
        models: Optional[list] = None,
    ):
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("Thời gian chờ (delay) phải là số hữu hạn không âm.")
        if key_pool is not None:
            self.key_pool = key_pool
        else:
            raw_keys = [api_key] if api_key and api_key.strip() else None
            model_list = models or ([model] if model and model.strip() else None)
            self.key_pool = APIKeyPool(keys=raw_keys, models=model_list)

        self.model = self.key_pool.models[0]
        self.delay = delay
        self.last_call = None
        self._resume_at = 0.0
        self.cache = cache if cache is not None else ReadingCache()
        self._start_lock = Lock()
        self._usage_lock = Lock()
        self.usage = Usage()

    @property
    def client(self):
        """Trả về client của key đầu tiên cho mục đích tương thích ngược."""
        if self.key_pool.keys:
            return self.key_pool._get_client(self.key_pool.keys[0])
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if hasattr(self, "key_pool"):
            self.key_pool.close()

    def _config(self, shape, interactive, model_name: Optional[str] = None):
        target_model = model_name or self.model
        model_id = target_model.removeprefix("models/")
        options = {}
        if model_id in ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite") and max(shape[:2]) <= 384:
            options["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
        if model_id in ("gemini-2.5-flash", "gemini-3.5-flash"):
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
        attempts = 1 if interactive else max(len(self.key_pool.keys) * len(self.key_pool.models) * 2, 4)

        for attempt in range(attempts):
            # Điều phối lấy Key và Model an toàn từ KeyPool
            api_key, active_model, client = self.key_pool.acquire_key()
            config = self._config(image.shape, interactive, model_name=active_model)

            with self._start_lock:
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
                    model=active_model,
                    contents=[PROMPT, part],
                    config=config,
                )
            except errors.APIError as exc:
                if exc.code == 429:
                    pause = rate_limit_wait(exc, attempt)
                    with self._start_lock:
                        self._resume_at = max(
                            self._resume_at, time.monotonic() + pause
                        )
                    self.key_pool.record_rate_limit(api_key, active_model, pause)
                    if attempt + 1 >= attempts:
                        raise RuntimeError(
                            f"Gemini giới hạn tần suất yêu cầu (HTTP 429). "
                            f"Vui lòng thử lại sau {math.ceil(pause)} giây."
                        ) from exc
                    if len(self.key_pool.keys) > 1 and self.key_pool.has_available_key(exclude_key=api_key):
                        continue
                    time.sleep(pause)
                    continue  # Lập tức xoay sang key/model tiếp theo!
                if exc.code == 401 or (exc.code in (400, 403) and "API_KEY_INVALID" in str(exc)):
                    self.key_pool.record_invalid(api_key)
                    remaining_valid_keys = any(
                        not slot.is_invalid
                        for (k, m), slot in self.key_pool._tracked.items()
                    )
                    if not interactive and remaining_valid_keys and attempt + 1 < attempts:
                        LOG.warning(
                            "Khóa API %s không hợp lệ hoặc hết hạn (HTTP %s). Tự động chuyển sang khóa tiếp theo...",
                            mask_key(api_key),
                            exc.code,
                        )
                        continue
                    raise RuntimeError(
                        f"Gemini báo khóa API không hợp lệ hoặc hết hạn (HTTP {exc.code}). "
                        "Vui lòng kiểm tra lại GEMINI_API_KEY trong .env."
                    ) from exc

                if exc.code == 403:
                    raise RuntimeError(
                        "Gemini từ chối quyền truy cập (HTTP 403). "
                        "Kiểm tra quyền của khóa đối với model đã chọn."
                    ) from exc
                if exc.code in (500, 502, 503, 504) and attempt + 1 < attempts:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if exc.code == 404:
                    raise RuntimeError(
                        f"Gemini không tìm thấy model {active_model} cho tài khoản này (HTTP 404). "
                        "Chọn model khác bằng --model hoặc GEMINI_MODELS."
                    ) from exc
                if exc.code == 504:
                    raise RuntimeError(
                        f"Gemini hết thời gian xử lý với {active_model} (HTTP 504). Bạn có thể thử lại."
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

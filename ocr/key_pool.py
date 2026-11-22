"""
Quản lý hồ bơi API Key (APIKeyPool) và tự động đổi Model (Model Fallback) cho OCR Hiragana.
Kiểm soát chủ động 2 tầng hạn mức:
1. RPM (Requests Per Minute): Tối đa 15 RPM (Ngưỡng an toàn: 12 RPM).
2. RPD (Requests Per Day): Tối đa 500 RPD (Ngưỡng an toàn: 450 RPD).
Lưu trữ bền vững hạn mức ngày (.key_usage.json) để không bị mất số liệu khi khởi động lại.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from google import genai
from google.genai import types

LOG = logging.getLogger("key_pool")

DEFAULT_SAFE_RPM = 12
DEFAULT_SAFE_RPD = 450
DEFAULT_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]


@dataclass
class TrackedKeyModel:
    key: str
    model: str
    max_rpm: int = DEFAULT_SAFE_RPM
    max_rpd: int = DEFAULT_SAFE_RPD
    timestamps: deque = field(default_factory=deque)
    daily_requests: int = 0
    last_reset_day: int = field(default_factory=lambda: time.gmtime().tm_yday)
    penalty_until: float = 0.0
    is_invalid: bool = False
    last_used: float = 0.0
    total_requests: int = 0

    def clean_old_timestamps(self, now: float) -> None:
        while self.timestamps and now - self.timestamps[0] >= 60.0:
            self.timestamps.popleft()

    def check_daily_reset(self, current_day: int) -> None:
        if current_day != self.last_reset_day:
            self.daily_requests = 0
            self.last_reset_day = current_day

    def current_rpm(self, now: float) -> int:
        self.clean_old_timestamps(now)
        return len(self.timestamps)

    def is_safe_to_use(self, now: float, current_day: int) -> bool:
        if self.is_invalid:
            return False
        if now < self.penalty_until:
            return False
        self.check_daily_reset(current_day)
        if self.daily_requests >= self.max_rpd:
            return False
        self.clean_old_timestamps(now)
        return len(self.timestamps) < self.max_rpm

    def time_until_available(self, now: float, current_day: int) -> float:
        if self.is_invalid:
            return float("inf")
        self.check_daily_reset(current_day)
        if self.daily_requests >= self.max_rpd:
            return float("inf")
        if now < self.penalty_until:
            return max(0.0, self.penalty_until - now)
        self.clean_old_timestamps(now)
        if len(self.timestamps) < self.max_rpm:
            return 0.0
        oldest = self.timestamps[0]
        return max(0.0, 60.0 - (now - oldest) + 0.1)

    def record_request(self, now: float, current_day: int) -> None:
        self.check_daily_reset(current_day)
        self.timestamps.append(now)
        self.daily_requests += 1
        self.total_requests += 1
        self.last_used = now


def usage_key_id(api_key: str) -> str:
    """Tạo mã băm SHA-256 định danh cố định cho API Key để lưu trữ mà không lộ key thô."""
    return sha256(api_key.encode("utf-8")).hexdigest()


def mask_key(key: str) -> str:
    """Ẩn phần giữa của API Key để bảo mật log/UI."""
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def load_keys_from_sources(
    raw_key: Optional[str] = None,
    api_keys_file: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> List[str]:
    """
    Nạp danh sách API Key không trùng lặp từ:
    1. api_keys_file (ocr/api_keys.txt hoặc api_keys.txt)
    2. GEMINI_API_KEYS (phân tách dấu phẩy hoặc xuống dòng)
    3. raw_key / GEMINI_API_KEY
    """
    keys: List[str] = []
    seen = set()

    def add_key(k: str):
        cleaned = k.strip().strip("'\"")
        if cleaned and not cleaned.startswith("#") and cleaned not in seen:
            seen.add(cleaned)
            keys.append(cleaned)

    # 1. Từ tệp api_keys.txt nếu có
    search_dirs = [base_dir] if base_dir else []
    search_dirs.extend([Path("."), Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent])

    file_candidates = []
    if api_keys_file and Path(api_keys_file).is_file():
        file_candidates.append(Path(api_keys_file))
    for d in search_dirs:
        if d:
            file_candidates.append(d / "api_keys.txt")
            file_candidates.append(d / "ocr" / "api_keys.txt")

    for fpath in file_candidates:
        if fpath.is_file():
            try:
                for line in fpath.read_text(encoding="utf-8").splitlines():
                    add_key(line)
                if keys:
                    LOG.info("Đã nạp %s API Key từ %s", len(keys), fpath)
                    break
            except Exception as exc:
                LOG.warning("Lỗi khi đọc %s: %s", fpath, exc)

    # 2. Từ biến môi trường GEMINI_API_KEYS (danh sách)
    env_keys = os.getenv("GEMINI_API_KEYS", "")
    if env_keys:
        for chunk in env_keys.replace(";", "\n").replace(",", "\n").splitlines():
            add_key(chunk)

    # 3. Từ raw_key hoặc GEMINI_API_KEY đơn lẻ
    if raw_key:
        for chunk in raw_key.replace(";", "\n").replace(",", "\n").splitlines():
            add_key(chunk)
    else:
        env_single = os.getenv("GEMINI_API_KEY", "")
        if env_single:
            for chunk in env_single.replace(";", "\n").replace(",", "\n").splitlines():
                add_key(chunk)

    return keys


class APIKeyPool:
    """
    Bộ quản lý hồ bơi API Key và chuỗi Model Fallback.
    Điều phối phân tải Least-Loaded, bảo vệ hạn mức RPM (12) và RPD (450).
    """

    def __init__(
        self,
        keys: Optional[List[str]] = None,
        models: Optional[List[str]] = None,
        usage_file: Optional[Path] = None,
        max_rpm: int = DEFAULT_SAFE_RPM,
        max_rpd: int = DEFAULT_SAFE_RPD,
        client_factory: Optional[Callable[[str], Any]] = None,
    ):
        raw_keys = keys or load_keys_from_sources()
        if not raw_keys:
            raise ValueError("Không tìm thấy GEMINI_API_KEY hợp lệ. Vui lòng thêm key vào .env hoặc ocr/api_keys.txt.")
        self.keys = raw_keys

        env_models = os.getenv("GEMINI_MODELS", "").strip()
        if models:
            self.models = [m.strip() for m in models if m.strip()]
        elif env_models:
            self.models = [m.strip() for m in env_models.split(",") if m.strip()]
        else:
            single = os.getenv("GEMINI_MODEL", "").strip()
            if single and single != DEFAULT_MODELS[0]:
                self.models = [single] + [m for m in DEFAULT_MODELS if m != single]
            else:
                self.models = list(DEFAULT_MODELS)

        self.max_rpm = max_rpm
        self.max_rpd = max_rpd
        self._lock = threading.RLock()
        self._client_factory = client_factory or self._default_client_factory
        self._clients: Dict[str, Any] = {}

        # Khởi tạo tracked slots cho từng cặp (key, model)
        self._tracked: Dict[Tuple[str, str], TrackedKeyModel] = {}
        for k in self.keys:
            for m in self.models:
                self._tracked[(k, m)] = TrackedKeyModel(key=k, model=m, max_rpm=self.max_rpm, max_rpd=self.max_rpd)

        # File lưu đĩa RPD bền vững
        if usage_file is not None:
            self.usage_file = Path(usage_file)
        else:
            root_dir = Path(__file__).resolve().parent
            self.usage_file = root_dir / ".key_usage.json"

        self.load_usage()

    @staticmethod
    def _default_client_factory(api_key: str) -> genai.Client:
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=60000, retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )

    def _get_client(self, api_key: str) -> Any:
        if api_key not in self._clients:
            self._clients[api_key] = self._client_factory(api_key)
        return self._clients[api_key]

    def load_usage(self) -> None:
        """Nạp số lượng request trong ngày từ tệp JSON và nâng cấp schema nếu cần."""
        with self._lock:
            if not self.usage_file.is_file():
                return
            try:
                data = json.loads(self.usage_file.read_text(encoding="utf-8"))
                schema_version = data.get("schema_version", 1)
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                saved_date = data.get("date")

                if schema_version < 2:
                    LOG.info("Phát hiện tệp %s phiên bản cũ. Bắt đầu chuyển đổi sang schema_version 2 (SHA-256)...", self.usage_file.name)
                    old_usage = data.get("usage", {})
                    migrated_usage: Dict[str, Any] = {}
                    for raw_k, models_dict in old_usage.items():
                        migrated_usage[usage_key_id(raw_k)] = models_dict
                    data["schema_version"] = 2
                    data["usage"] = migrated_usage

                    tmp_file = self.usage_file.with_suffix(".tmp")
                    tmp_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    tmp_file.replace(self.usage_file)
                    LOG.info("Đã nâng cấp %s lên schema_version 2 thành công.", self.usage_file.name)

                # Chỉ nạp nếu cùng ngày UTC
                if saved_date == today_str:
                    usage_map = data.get("usage", {})
                    for (k, m), slot in self._tracked.items():
                        k_id = usage_key_id(k)
                        k_meta = usage_map.get(k_id, {})
                        m_meta = k_meta.get(m, {})
                        saved_reqs = m_meta.get("daily_requests", 0)
                        slot.daily_requests = int(saved_reqs)
                        slot.total_requests = int(m_meta.get("total_requests", slot.daily_requests))
                    LOG.info("Đã khôi phục số liệu RPD từ %s (Ngày: %s)", self.usage_file.name, saved_date)
                else:
                    LOG.info("Đã bước sang ngày mới UTC (%s -> %s). Tự động đặt lại bộ đếm RPD.", saved_date, today_str)
            except Exception as exc:
                LOG.warning("Không thể đọc tệp trạng thái RPD (%s): %s", self.usage_file, exc)

    def _save_usage_unlocked(self) -> None:
        try:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            usage_map: Dict[str, Dict[str, Any]] = {}

            for (k, m), slot in self._tracked.items():
                key_id = usage_key_id(k)
                if key_id not in usage_map:
                    usage_map[key_id] = {}
                usage_map[key_id][m] = {
                    "daily_requests": slot.daily_requests,
                    "total_requests": slot.total_requests,
                    "max_rpd": slot.max_rpd,
                    "status": "exhausted" if slot.daily_requests >= slot.max_rpd else "active",
                }

            payload = {
                "schema_version": 2,
                "date": today_str,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "usage": usage_map,
            }

            self.usage_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.usage_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_file.replace(self.usage_file)
        except Exception as exc:
            LOG.debug("Không thể ghi tệp RPD usage: %s", exc)

    def save_usage(self) -> None:
        """Ghi nguyên tử số liệu RPD xuống đĩa."""
        with self._lock:
            self._save_usage_unlocked()

    def has_available_key(self, exclude_key: Optional[str] = None) -> bool:
        """Kiểm tra xem còn key/model nào khác sẵn sàng gửi request ngay không."""
        with self._lock:
            now = time.monotonic()
            current_day = time.gmtime().tm_yday
            for (k, m), slot in self._tracked.items():
                if exclude_key and k == exclude_key:
                    continue
                if slot.is_safe_to_use(now, current_day):
                    return True
            return False

    def acquire_key(self) -> Tuple[str, str, Any]:
        """
        Lấy cặp (api_key, model_name, client) an toàn nhất để giải ảnh:
        1. Duyệt qua từng model theo thứ tự ưu tiên (Model Fallback).
        2. Với mỗi model, tìm key an toàn có current_rpm < max_rpm và daily_requests < max_rpd.
        3. Chọn key có tải thấp nhất (ít RPM nhất và ít RPD nhất).
        4. Nếu model 1 hết hạn mức ngày (>= 450 RPD), tự động chuyển sang model 2!
        5. Nếu tất cả key chỉ đang bận RPM (phút), tính thời gian chờ nhỏ nhất và tạm nghỉ an toàn.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                current_day = time.gmtime().tm_yday

                # Thử lần lượt từng model theo thứ tự ưu tiên
                for model_name in self.models:
                    model_slots = [
                        slot for (k, m), slot in self._tracked.items() if m == model_name and not slot.is_invalid
                    ]
                    if not model_slots:
                        continue

                    # Kiểm tra xem toàn bộ key của model này có bị hết hạn mức ngày (RPD >= 450) không
                    all_daily_exhausted = all(slot.daily_requests >= slot.max_rpd for slot in model_slots)
                    if all_daily_exhausted:
                        # Model này đã cạn kiệt 450 lượt hôm nay -> Chuyển sang model tiếp theo trong chuỗi fallback!
                        LOG.debug("Model %s đã hết hạn mức ngày trên toàn bộ key. Chuyển sang model dự phòng tiếp theo...", model_name)
                        continue

                    # Lọc ra các key an toàn ngay lúc này (current_rpm < 12 và daily < 450)
                    safe_slots = [slot for slot in model_slots if slot.is_safe_to_use(now, current_day)]
                    if not safe_slots and len(self.keys) == 1:
                        candidate = model_slots[0]
                        candidate.check_daily_reset(current_day)
                        candidate.clean_old_timestamps(now)
                        if (
                            not candidate.is_invalid
                            and candidate.daily_requests < candidate.max_rpd
                            and len(candidate.timestamps) < candidate.max_rpm
                        ):
                            safe_slots = [candidate]

                    if safe_slots:
                        # Chọn key ít bận nhất (ưu tiên RPM thấp nhất, sau đó daily_requests thấp nhất)
                        chosen = min(safe_slots, key=lambda s: (s.current_rpm(now), s.daily_requests))
                        chosen.record_request(now, current_day)
                        client = self._get_client(chosen.key)
                        model_to_use = chosen.model
                        key_to_use = chosen.key
                        self._save_usage_unlocked()
                        return key_to_use, model_to_use, client

                # Nếu duyệt qua tất cả model mà không có slot nào an toàn ngay:
                # Kiểm tra xem có phải TẤT CẢ key trên TẤT CẢ model đều hết hạn mức ngày (RPD) không
                valid_slots = [slot for slot in self._tracked.values() if not slot.is_invalid]
                if not valid_slots:
                    raise RuntimeError("Tất cả API Key đều bị Google báo không hợp lệ (Invalid API Key).")

                if all(slot.daily_requests >= slot.max_rpd for slot in valid_slots):
                    raise RuntimeError(
                        f"Tất cả API Key và Model dự phòng đều đã đạt ngưỡng an toàn ngày ({self.max_rpd}/500 RPD). "
                        "Vui lòng thêm API Key mới vào ocr/api_keys.txt hoặc chờ sang ngày mới (00:00 UTC)."
                    )

                # Chỉ bị nghẽn RPM phút: tìm thời gian chờ nhỏ nhất
                min_wait = min(slot.time_until_available(now, current_day) for slot in valid_slots)
                sleep_time = max(0.2, min(min_wait, 5.0))

            LOG.debug("Tất cả key đang ở ngưỡng an toàn RPM (>=%s). Tạm nghỉ %0.2fs để hạ nhiệt...", self.max_rpm, sleep_time)
            time.sleep(sleep_time)

    def record_rate_limit(self, key: str, model: str, pause_seconds: float) -> None:
        """Ghi nhận phạt cooldown khi Google trả về HTTP 429 đột xuất."""
        with self._lock:
            slot = self._tracked.get((key, model))
            if slot:
                slot.penalty_until = time.monotonic() + pause_seconds
                LOG.warning(
                    "Key %s trên model %s bị 429. Đặt tạm nghỉ %0.1fs. Hệ thống tự động chuyển sang key/model khác.",
                    mask_key(key),
                    model,
                    pause_seconds,
                )

    def record_invalid(self, key: str) -> None:
        """Đánh dấu key hỏng (400/403) trên toàn bộ model."""
        with self._lock:
            for (k, m), slot in self._tracked.items():
                if k == key:
                    slot.is_invalid = True
            LOG.error("API Key %s không hợp lệ (Invalid Key). Đã loại bỏ khỏi hệ thống.", mask_key(key))

    def get_status_summary(self) -> Dict[str, Any]:
        """Tạo báo cáo tổng hợp tình trạng sức khỏe của hồ bơi Key để hiển thị trên UI và /health."""
        with self._lock:
            now = time.monotonic()
            current_day = time.gmtime().tm_yday
            details = []
            total_active_slots = 0
            total_remaining_rpd = 0

            for (k, m), slot in self._tracked.items():
                slot.check_daily_reset(current_day)
                rpm = slot.current_rpm(now)
                rpd = slot.daily_requests
                is_safe = slot.is_safe_to_use(now, current_day)
                if is_safe:
                    total_active_slots += 1
                rem_rpd = max(0, slot.max_rpd - rpd)
                total_remaining_rpd += rem_rpd

                if slot.is_invalid:
                    st = "invalid"
                elif rpd >= slot.max_rpd:
                    st = "exhausted_today"
                elif now < slot.penalty_until:
                    st = f"cooldown_{int(slot.penalty_until - now)}s"
                elif rpm >= slot.max_rpm:
                    st = "near_rpm_limit"
                else:
                    st = "active"

                details.append({
                    "key_masked": mask_key(k),
                    "model": m,
                    "rpm": f"{rpm}/{slot.max_rpm}",
                    "rpd": f"{rpd}/{slot.max_rpd}",
                    "remaining_rpd": rem_rpd,
                    "status": st,
                })

            return {
                "total_keys": len(self.keys),
                "models": self.models,
                "total_active_slots": total_active_slots,
                "total_remaining_rpd": total_remaining_rpd,
                "details": details,
            }

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                try:
                    if hasattr(client, "close"):
                        client.close()
                except Exception:
                    pass
            self._clients.clear()

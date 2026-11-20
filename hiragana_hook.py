from abc import ABC, abstractmethod
import asyncio
from typing import Callable, Awaitable, Optional

from ocr_client import (
    HiraganaOCRClient,
    OCRError,
    OCRConnectionError,
    OCRValidationError,
    OCRJobFailedError,
)


class HiraganaHook(ABC):
    """
    Cổng kết nối chuẩn cho bước giải văn bản Hiragana (画像認証).
    - Giai đoạn 1 & 2: Dùng ManualHiraganaHook (Human-in-the-loop).
    - Giai đoạn 4: Kế thừa lớp này và gắn module OCR riêng của bạn vào hàm solve().
    """

    @abstractmethod
    async def solve(
        self,
        image_bytes: Optional[bytes],
        account_index: int,
        email: str,
    ) -> Optional[str]:
        """
        Trả về:
        - Chuỗi ký tự Hiragana (str) nếu module tự động giải và muốn tool tự điền vào ô 文字の入力.
        - None nếu người dùng đã tự nhập trực tiếp trên trình duyệt và bấm Tiếp tục.
        """
        raise NotImplementedError


class ManualHiraganaHook(HiraganaHook):
    """
    Chế độ mặc định (Human-in-the-loop):
    Thông báo lên giao diện App và chờ người dùng giải trực tiếp trên cửa sổ Chrome,
    sau đó bấm nút xác nhận trên App để tiếp tục.
    """

    def __init__(
        self,
        wait_for_user_callback: Callable[[int, str], Awaitable[Optional[str]]],
    ):
        self._wait_for_user_callback = wait_for_user_callback

    async def solve(
        self,
        image_bytes: Optional[bytes],
        account_index: int,
        email: str,
    ) -> Optional[str]:
        return await self._wait_for_user_callback(account_index, email)


class OCRHiraganaHook(HiraganaHook):
    """
    Chế độ tự động thông qua dịch vụ OCR Hiragana cục bộ:
    - Lưu ảnh Hiragana vào inbox dưới tên tệp duy nhất (.part -> .png).
    - Đọc kết quả qua /results và lấy normalized_text (đã chuẩn hóa).
    - Kiểm tra tính hợp lệ độ dài 6 ký tự.
    - Tự động chuyển giao sang fallback_hook (thủ công) nếu OCR server không phản hồi,
      ảnh lỗi hoặc kết quả không đạt chuẩn 6 ký tự.
    """

    def __init__(
        self,
        ocr_client: HiraganaOCRClient,
        fallback_hook: Optional[HiraganaHook] = None,
        timeout: float = 60.0,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.ocr_client = ocr_client
        self.fallback_hook = fallback_hook
        self.timeout = timeout
        self.on_log = on_log or (lambda msg: None)

    async def solve_ocr(
        self,
        image_bytes: Optional[bytes],
        account_index: int,
        email: str,
    ) -> Optional[str]:
        """
        Thực hiện giải OCR cho 1 ảnh đơn lẻ.
        Trả về chuỗi Hiragana 6 ký tự nếu thành công, hoặc None nếu thất bại.
        Không tự động chuyển sang fallback_hook để bộ điều phối (NinjaAutomationRunner)
        có thể chủ động thử lại (retry) nhiều lần theo nghiệp vụ.
        """
        if not image_bytes:
            self.on_log(f"[{account_index}] Không có dữ liệu ảnh Hiragana.")
            return None

        # Kiểm tra nhanh trạng thái dịch vụ OCR trước khi gửi ảnh
        healthy, health_info = self.ocr_client.check_health()
        if not healthy:
            err_msg = health_info.get("error", "Dịch vụ OCR không phản hồi")
            self.on_log(f"[{account_index}] Dịch vụ OCR không khả dụng ({err_msg}).")
            return None

        self.on_log(f"[{account_index}] Đang gửi ảnh Hiragana sang dịch vụ OCR...")

        try:
            solved_text = await self.ocr_client.solve_image(
                image_bytes=image_bytes,
                account_index=account_index,
                email=email,
                timeout=self.timeout,
            )
            if solved_text and len(solved_text) == self.ocr_client.config.expected_length:
                self.on_log(f"[{account_index}] OCR giải thành công: '{solved_text}' (đã chuẩn hóa).")
                return solved_text
            else:
                length = len(solved_text) if solved_text else 0
                self.on_log(
                    f"[{account_index}] OCR trả kết quả không đủ {self.ocr_client.config.expected_length} ký tự "
                    f"({length} ký tự: '{solved_text}')."
                )
                return None
        except (OCRConnectionError, OCRValidationError, OCRJobFailedError, TimeoutError, asyncio.TimeoutError) as exc:
            self.on_log(f"[{account_index}] OCR không giải được ({type(exc).__name__}): {exc}")
            return None
        except Exception as exc:
            self.on_log(f"[{account_index}] Lỗi bất ngờ khi gọi OCR: {exc}")
            return None

    async def solve(
        self,
        image_bytes: Optional[bytes],
        account_index: int,
        email: str,
    ) -> Optional[str]:
        solved = await self.solve_ocr(image_bytes, account_index, email)
        if solved is not None:
            return solved

        # Dự phòng khi OCR thất bại: chuyển sang nhập tay trên trình duyệt
        if self.fallback_hook:
            self.on_log(f"[{account_index}] Chuyển sang chờ người dùng nhập tay Hiragana trên Chromium.")
            return await self.fallback_hook.solve(image_bytes, account_index, email)

        return None

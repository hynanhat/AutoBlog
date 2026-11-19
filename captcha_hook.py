from abc import ABC, abstractmethod
from typing import Callable, Awaitable, Optional


class CaptchaHook(ABC):
    """
    Cổng kết nối chuẩn cho bước giải CAPTCHA (画像認証).
    - Giai đoạn 1 & 2: Dùng ManualCaptchaHook (Human-in-the-loop).
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


class ManualCaptchaHook(CaptchaHook):
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
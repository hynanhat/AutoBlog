"""OCR Hiragana Package for AutoBlog.

Cung cấp dịch vụ nhận dạng và chuẩn hóa ký tự Hiragana cho Captcha của Ninja Blog
sử dụng Google Gemini AI và OpenCV.
"""

import sys
from pathlib import Path

_OCR_DIR = str(Path(__file__).resolve().parent)
if _OCR_DIR not in sys.path:
    sys.path.append(_OCR_DIR)

__all__ = ["server", "ocr_service", "folder_ocr", "formatter", "preprocess", "config"]

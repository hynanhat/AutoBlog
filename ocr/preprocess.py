"""Local image processing. Color arrays use OpenCV's BGR order."""

from pathlib import Path

import cv2
import numpy as np

import config


def decode_image(data: bytes, flags=cv2.IMREAD_COLOR) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(data, np.uint8), flags) if data else None
    if image is None:
        raise ValueError("Tệp rỗng hoặc không phải ảnh PNG/JPEG đọc được.")
    return image


def recognize_clean(data, service, **options):
    clean, _ = denoise_hiragana(decode_image(data), include_stages=False, **options)
    return service.recognize(clean)


def read_image(path: str | Path) -> np.ndarray:
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if image is None:
        raise ValueError(f"Không đọc được ảnh: {path}")
    return image


def write_image(path: str | Path, image: np.ndarray) -> None:
    extension = Path(path).suffix.lower()
    if extension not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("Chọn định dạng PNG hoặc JPEG.")
    ok, encoded = cv2.imencode(extension, image)
    if not ok:
        raise ValueError(f"Không mã hóa được ảnh: {path}")
    encoded.tofile(path)


def denoise_hiragana(
    image: np.ndarray,
    *,
    mode: str = "auto",
    thresh_val: int = config.THRESH_VAL,
    median_blur: int = config.MEDIAN_BLUR,
    morph_kernel: int = config.MORPH_KERNEL,
    min_area: int = config.MIN_AREA,
    padding: int = config.PADDING,
    include_stages: bool = True,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Return black ink on white and, when requested, six display stages.

    Input is an 8-bit grayscale or BGR array. Kernel size 1 disables that
    filter. Padding and minimum component area are measured in pixels.
    """
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or not image.size:
        raise ValueError("Ảnh đầu vào phải là mảng uint8 có dữ liệu.")
    if image.ndim == 2:
        gray = image.copy()
    elif image.ndim == 3 and image.shape[2] == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        raise ValueError("Ảnh phải có một kênh xám hoặc ba kênh BGR.")

    mode = mode.lower()
    if mode not in {"auto", "manual"}:
        raise ValueError("Chế độ phải là auto hoặc manual.")
    if mode == "manual" and not 20 <= thresh_val <= 120:
        raise ValueError("Ngưỡng thủ công phải nằm trong khoảng 20–120.")
    for size in (median_blur, morph_kernel):
        if not isinstance(size, int) or size < 1 or size % 2 == 0:
            raise ValueError("Kích thước nhân lọc phải là số nguyên dương lẻ.")
    if min_area < 1 or padding < 0:
        raise ValueError("Diện tích tối thiểu phải dương và viền phải không âm.")

    if mode == "auto":
        otsu, mask = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
        )
        # Ink is near black. A bright background can otherwise pull Otsu's
        # threshold into the gray texture when text occupies very few pixels.
        if otsu > 120:
            _, mask = cv2.threshold(gray, 120, 255, cv2.THRESH_BINARY_INV)
    else:
        _, mask = cv2.threshold(gray, thresh_val, 255, cv2.THRESH_BINARY_INV)
    smooth = cv2.medianBlur(mask, median_blur) if median_blur > 1 else mask
    opened = smooth
    if morph_kernel > 1:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (morph_kernel, morph_kernel)
        )
        opened = cv2.morphologyEx(smooth, cv2.MORPH_OPEN, kernel)
    components = opened
    if min_area > 1:
        _, labels, stats, _ = cv2.connectedComponentsWithStats(opened, connectivity=8)
        keep = (stats[:, cv2.CC_STAT_AREA] >= min_area).astype(np.uint8) * 255
        keep[0] = 0
        components = keep[labels]
    closed = (
        cv2.morphologyEx(components, cv2.MORPH_CLOSE, kernel)
        if morph_kernel > 1
        else components
    )
    ink = cv2.bitwise_not(closed)
    result = cv2.copyMakeBorder(
        ink, padding, padding, padding, padding, cv2.BORDER_CONSTANT, value=255
    )
    if not include_stages:
        return result, {}
    stages = {
        "gray": gray,
        "threshold": cv2.bitwise_not(mask),
        "remove_dots_lines": cv2.bitwise_not(opened),
        "connected_components": cv2.bitwise_not(components),
        "close": ink,
        "padding": result,
    }
    return result, stages

"""Project defaults shared by preprocessing, the UI and OCR."""
import os

DEFAULT_MODEL = "gemini-2.5-flash"
MAX_WORKERS = 10


def configured_model() -> str:
    """Resolve the local model preference after .env is loaded."""
    return os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


HIRAGANA_PATTERN = r"[\u3040-\u309F]"
EXPECTED_LENGTH = 6
THRESH_VAL = 65
MEDIAN_BLUR = 3
MORPH_KERNEL = 3
MIN_AREA = 9
PADDING = 8

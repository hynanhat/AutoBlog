"""Project defaults shared by preprocessing, the UI and OCR."""
import os

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
MAX_WORKERS = 10


def configured_model() -> str:
    """Resolve the local model preference after .env is loaded."""
    return os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODELS[0]


def configured_models() -> list[str]:
    """Resolve the model preference chain after .env is loaded."""
    raw = os.getenv("GEMINI_MODELS", "").strip()
    if raw:
        models = [item.strip() for item in raw.split(",") if item.strip()]
        if not models:
            raise ValueError("GEMINI_MODELS phải chứa ít nhất một tên model.")
        return models
    single = os.getenv("GEMINI_MODEL", "").strip()
    if single:
        return [single] + [m for m in DEFAULT_MODELS if m != single]
    return list(DEFAULT_MODELS)


HIRAGANA_PATTERN = r"[\u3040-\u309F]"
EXPECTED_LENGTH = 6
THRESH_VAL = 65
MEDIAN_BLUR = 3
MORPH_KERNEL = 3
MIN_AREA = 9
PADDING = 8

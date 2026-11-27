import sys
from importlib import import_module
from pathlib import Path


RUNTIME_MODULES = (
    "tkinter",
    "customtkinter",
    "playwright.async_api",
    "playwright.sync_api",
    "imapclient",
    "google_auth_oauthlib.flow",
    "google.genai",
    "cv2",
    "numpy",
    "filelock",
    "starlette",
    "uvicorn",
    "pydantic",
    "dotenv",
    "PIL.Image",
)


def runtime_ready():
    for name in RUNTIME_MODULES:
        try:
            import_module(name)
        except Exception as exc:
            print(f"Không nạp được {name}: {exc}", file=sys.stderr)
            return False

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if executable.is_file():
                return True
            print(f"Thiếu Chromium tại {executable}", file=sys.stderr)
    except Exception as exc:
        print(f"Lỗi khi kiểm tra Chromium: {exc}", file=sys.stderr)

    return False


if __name__ == "__main__":
    raise SystemExit(0 if runtime_ready() else 1)

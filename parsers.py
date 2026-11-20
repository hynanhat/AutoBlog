import codecs
import re
from pathlib import Path
from typing import Dict, List, Tuple

EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
BLOG_HEADER_REGEX = re.compile(r"^===\s*\[BLOG\s+(\d+)\]\s*===\s*$")


def parse_gmails(raw_text: str, max_count: int = 101) -> Tuple[List[str], List[str]]:
    """
    Phân tích danh sách Gmail dán từ giao diện:
    - Loại bỏ dòng trống, khoảng trắng thừa
    - Loại bỏ trùng lặp nhưng giữ nguyên thứ tự
    - Kiểm tra định dạng email và giới hạn tối đa 101 tài khoản
    """
    valid_gmails: List[str] = []
    errors: List[str] = []
    seen = set()

    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        email = raw_line.strip()
        if not email:
            continue
        if not EMAIL_REGEX.match(email):
            errors.append(f"Dòng {line_no}: '{email}' không đúng định dạng email.")
            continue
        lowered = email.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        valid_gmails.append(email)

    if len(valid_gmails) > max_count:
        errors.append(
            f"Số lượng Gmail ({len(valid_gmails)}) vượt quá giới hạn tối đa {max_count} tài khoản."
        )

    return valid_gmails, errors


def parse_posts_txt(file_path: str) -> Tuple[Dict[int, Dict[str, str]], List[str]]:
    """
    Đọc file TXT theo Phương án A:
      === [BLOG N] ===
      TITLE: Tiêu đề riêng (tùy chọn)
      <p>Nội dung HTML...</p>
    Trả về: (posts_map, errors)
      posts_map[N] = {"title": str, "html": str}
    """
    path = Path(file_path)
    errors: List[str] = []
    posts_map: Dict[int, Dict[str, str]] = {}

    try:
        data = path.read_bytes()
        encoding = "utf-16" if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
        content = data.decode(encoding)
    except FileNotFoundError:
        return {}, [f"Không tìm thấy file: {file_path}"]
    except UnicodeError:
        return {}, ["Không đọc được mã hóa của tệp TXT. Hãy lưu tệp dưới dạng UTF-8 hoặc UTF-16 có BOM."]
    except OSError as exc:
        return {}, [f"Không thể đọc tệp TXT {path}: {exc}"]
    current_idx: int | None = None
    current_lines: List[str] = []

    def flush_block():
        if current_idx is None:
            return
        title = ""
        html = "\n".join(current_lines).strip()
        if html[:6].upper() == "TITLE:":
            title_line, _, html = html.partition("\n")
            title = title_line[6:].strip()
            html = html.lstrip()

        if not html:
            errors.append(f"[BLOG {current_idx}] đang bị trống nội dung HTML.")
        posts_map[current_idx] = {"title": title, "html": html}

    for line in content.splitlines():
        match = BLOG_HEADER_REGEX.match(line.strip())
        if match:
            flush_block()
            idx = int(match.group(1))
            if idx in posts_map:
                errors.append(f"Trùng số thứ tự [BLOG {idx}] trong file TXT.")
            current_idx = idx
            current_lines = []
        elif current_idx is not None:
            current_lines.append(line)

    flush_block()

    if not posts_map and not errors:
        errors.append("Không tìm thấy khối nào theo chuẩn '=== [BLOG N] ===' trong file.")

    return posts_map, errors


def validate_mapping(gmail_count: int, posts_map: Dict[int, Dict[str, str]]) -> List[int]:
    """Trả về danh sách số thứ tự còn thiếu so với số lượng Gmail."""
    return [i for i in range(1, gmail_count + 1) if i not in posts_map]

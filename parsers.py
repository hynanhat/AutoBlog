import codecs
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
BLOG_HEADER_REGEX = re.compile(r"^===\s*\[BLOG\s+(\d+)\]\s*===\s*$")


@dataclass(frozen=True)
class GmailLine:
    """Một địa chỉ Gmail hợp lệ cùng số dòng nguồn trong ô nhập."""

    line_no: int
    email: str


def _parse_gmail_lines(
    raw_text: str, max_count: Optional[int] = 101
) -> Tuple[List[GmailLine], List[str]]:
    """
    Phân tích danh sách Gmail:
    - Loại bỏ dòng trống, khoảng trắng thừa
    - Loại bỏ trùng lặp (không phân biệt hoa/thường) nhưng giữ nguyên thứ tự và số dòng nguồn
    - Kiểm tra định dạng email và giới hạn tối đa (nếu có)
    """
    valid_entries: List[GmailLine] = []
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
        valid_entries.append(GmailLine(line_no=line_no, email=email))

    if max_count is not None and len(valid_entries) > max_count:
        errors.append(
            f"Số lượng Gmail ({len(valid_entries)}) vượt quá giới hạn tối đa {max_count} tài khoản."
        )

    return valid_entries, errors


def parse_gmails(raw_text: str, max_count: int = 101) -> Tuple[List[str], List[str]]:
    """
    Phân tích danh sách Gmail dán từ giao diện:
    - Loại bỏ dòng trống, khoảng trắng thừa
    - Loại bỏ trùng lặp nhưng giữ nguyên thứ tự
    - Kiểm tra định dạng email và giới hạn tối đa 101 tài khoản
    """
    entries, errors = _parse_gmail_lines(raw_text, max_count=max_count)
    return [entry.email for entry in entries], errors


def _has_invalid_local_dots(email: str) -> bool:
    local, _, _domain = email.partition("@")
    return local.startswith(".") or local.endswith(".") or ".." in local


def normalize_gmail_for_ownership(email: str) -> str:
    """
    Chuẩn hóa địa chỉ khi kiểm tra quyền sở hữu hộp thư Gmail gốc:
    chỉ bỏ dấu chấm ở phần tên với gmail.com, không dùng để gửi hay ghép thư.
    """
    cleaned = email.strip()
    local, sep, domain = cleaned.partition("@")
    if not sep:
        return cleaned.lower()
    domain_lower = domain.lower()
    if domain_lower == "gmail.com":
        local_norm = local.replace(".", "").lower()
    else:
        local_norm = local.lower()
    return f"{local_norm}@{domain_lower}"


def belongs_to_root_gmail(email: str, root_gmail: str) -> bool:
    """Kiểm tra email đăng ký có thuộc hộp thư Gmail gốc hay không."""
    candidate = email.strip()
    root = root_gmail.strip()
    if not EMAIL_REGEX.match(candidate) or not EMAIL_REGEX.match(root):
        return False
    return normalize_gmail_for_ownership(candidate) == normalize_gmail_for_ownership(root)


def parse_registration_gmails(
    raw_text: str, root_gmail: str, max_count: int = 101
) -> Tuple[List[GmailLine], List[str]]:
    """
    Phân tích danh sách Gmail cho tab Đăng ký tài khoản:
    - Tái sử dụng quy tắc bỏ dòng trống, bỏ trùng, giữ thứ tự của parse_gmails()
    - Giữ nguyên dấu chấm gốc và số dòng nguồn của từng địa chỉ
    - Kiểm tra mỗi địa chỉ có thuộc hộp thư Gmail gốc (chỉ bỏ dấu chấm khi đối chiếu gmail.com)
    """
    root_clean = (root_gmail or "").strip()
    if not root_clean or not EMAIL_REGEX.match(root_clean) or _has_invalid_local_dots(root_clean):
        return [], ["Gmail gốc không đúng định dạng email."]

    entries, errors = _parse_gmail_lines(raw_text, max_count=None)
    accepted: List[GmailLine] = []

    for entry in entries:
        if _has_invalid_local_dots(entry.email):
            errors.append(
                f"Dòng {entry.line_no}: '{entry.email}' có dấu chấm không hợp lệ trong tên email."
            )
            continue
        if not belongs_to_root_gmail(entry.email, root_clean):
            errors.append(
                f"Dòng {entry.line_no}: '{entry.email}' không thuộc hộp thư Gmail gốc ({root_clean})."
            )
            continue
        accepted.append(entry)

    if len(accepted) > max_count:
        errors.append(
            f"Số lượng Gmail ({len(accepted)}) vượt quá giới hạn tối đa {max_count} tài khoản."
        )

    return accepted, errors


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

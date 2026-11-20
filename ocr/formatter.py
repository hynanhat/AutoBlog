"""Structured OCR response and UTF-8 exports."""

import csv
import io
import json
import re
import unicodedata

from pydantic import BaseModel

from config import EXPECTED_LENGTH, HIRAGANA_PATTERN


class Transcription(BaseModel):
    raw_hiragana: str
    confidence_note: str


class Reading(Transcription):
    romaji: list[str]


def hiragana(text: str) -> str:
    return "".join(re.findall(HIRAGANA_PATTERN, unicodedata.normalize("NFC", text)))


_BASE_KANA = str.maketrans(
    "ぁぃぅぇぉっゃゅょゎゕゖ", "あいうえおつやゆよわかけ", "\u3099\u309a゛゜"
)


def normalize_hiragana(text: str) -> str:
    """Produce the optional unvoiced, regular-size view of an OCR string."""
    decomposed = unicodedata.normalize("NFD", hiragana(text))
    return unicodedata.normalize("NFC", decomposed.translate(_BASE_KANA))


_ROMAJI = {
    kana: syllable
    for row, syllables in (
        ("あいうえお", "a i u e o"),
        ("かきくけこ", "ka ki ku ke ko"),
        ("さしすせそ", "sa shi su se so"),
        ("たちつてと", "ta chi tsu te to"),
        ("なにぬねの", "na ni nu ne no"),
        ("はひふへほ", "ha hi fu he ho"),
        ("まみむめも", "ma mi mu me mo"),
        ("やゆよ", "ya yu yo"),
        ("らりるれろ", "ra ri ru re ro"),
        ("わゐゑをん", "wa wi we wo n"),
        ("がぎぐげご", "ga gi gu ge go"),
        ("ざじずぜぞ", "za ji zu ze zo"),
        ("だぢづでど", "da ji zu de do"),
        ("ばびぶべぼ", "ba bi bu be bo"),
        ("ぱぴぷぺぽ", "pa pi pu pe po"),
        ("ゔゟ", "vu yori"),
        ("ぁぃぅぇぉっゃゅょゎゕゖ", "a i u e o tsu ya yu yo wa ka ke"),
    )
    for kana, syllable in zip(row, syllables.split(), strict=True)
}


def _romanize(text: str) -> list[str]:
    previous = ""
    result = []
    for char in text:
        if char in "ゝゞ" and previous:
            base = unicodedata.normalize("NFD", previous)[0]
            char = unicodedata.normalize(
                "NFC", base + ("\u3099" if char == "ゞ" else "")
            )
        result.append(_ROMAJI.get(char, ""))
        previous = char
    return result


def format_reading(reading: Transcription) -> dict:
    text = hiragana(reading.raw_hiragana)
    romaji = reading.romaji if isinstance(reading, Reading) else _romanize(text)
    warnings = []
    if len(text) != EXPECTED_LENGTH:
        warnings.append(f"Nhận được {len(text)}/{EXPECTED_LENGTH} ký tự.")
    aligned = len(romaji) == len(text)
    if not aligned:
        warnings.append("Số mục romaji lệch với số ký tự đã lọc.")
    return {
        "raw_hiragana": reading.raw_hiragana,
        "text": text,
        "spaced": " ".join(text),
        "characters": [
            {"hiragana": char, "romaji": romaji[i] if aligned else ""}
            for i, char in enumerate(text)
        ],
        "confidence_note": reading.confidence_note,
        "warnings": warnings,
    }


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def as_csv(rows: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    fields = list(dict.fromkeys(key for row in rows for key in row))
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: as_json(val) if isinstance(val, (dict, list)) else val
                for key, val in row.items()
            }
        )
    return stream.getvalue().encode("utf-8-sig")

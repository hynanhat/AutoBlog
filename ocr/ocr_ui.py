"""Streamlit entry point for the Hiragana OCR project."""

import os
import sys
from hashlib import sha256
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2
import streamlit as st
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
if not os.getenv("GEMINI_API_KEY") and (ROOT.parent / ".env").is_file():
    load_dotenv(ROOT.parent / ".env")

import config
from evaluate import chart_png, compare_dataset, load_labels, summarize
from formatter import as_csv, as_json, normalize_hiragana
from ocr_service import OCRService, ReadingCache, ordered_map
from preprocess import decode_image, denoise_hiragana, recognize_clean
SAMPLES = ROOT / "data" / "samples"
EXTENSIONS = ["png", "jpg", "jpeg"]


def png(image):
    return cv2.imencode(".png", image)[1].tobytes()


def uploads(files):
    images = {}
    for file in files:
        name = file.name.replace("\\", "/")
        if name in images:
            raise ValueError(f"Tên ảnh bị trùng: {name}")
        images[name] = file.getvalue()
    return images


def signature(images, options, model, labels=None):
    return (
        tuple((name, sha256(data).digest()) for name, data in images.items()),
        tuple(options.items()),
        model,
        labels,
    )


def normalized_length_warning(text):
    if text is None or len(text) == config.EXPECTED_LENGTH:
        return ""
    return f"Sau chuẩn hóa có {len(text)}/{config.EXPECTED_LENGTH} ký tự."


def show_normalized(text):
    st.caption("Chữ đã chuẩn hóa")
    st.code(text, language=None)
    st.text(" ".join(text))
    warning = normalized_length_warning(text)
    if warning:
        st.warning(warning)


def show_reading(reading):
    if "normalized_text" in reading:
        show_normalized(reading["normalized_text"])
        with st.expander("Xem kết quả OCR ban đầu và romaji"):
            show_original_reading(reading)
    else:
        show_original_reading(reading)


def show_original_reading(reading):
    st.code(reading["text"] or "∅")
    st.text(reading.get("spaced", " ".join(reading["text"])))
    if reading.get("characters"):
        st.caption("Romaji của kết quả OCR")
        st.dataframe(reading["characters"], hide_index=True)
    if reading.get("confidence_note"):
        st.caption(reading["confidence_note"])
    for warning in reading.get("warnings", []):
        st.warning(warning)


def single_tab(options, model, service_factory):
    with st.expander("Định dạng lại văn bản đã có"):
        pasted = st.text_input(
            "Dán chuỗi Hiragana từ kết quả OCR", key="paste_hiragana"
        )
        if pasted:
            show_normalized(normalize_hiragana(pasted))
    file = st.file_uploader("Chọn một ảnh", type=EXTENSIONS)
    if file:
        name, data = file.name, file.getvalue()
    else:
        sample = st.selectbox(
            "Hoặc dùng ảnh mẫu", sorted(p.name for p in SAMPLES.glob("*.png"))
        )
        if not sample:
            return
        name, data = sample, (SAMPLES / sample).read_bytes()
    token = signature({name: data}, options, model)
    preview = st.session_state.get("single_preview")
    if preview is None or preview[0] != token[:2]:
        try:
            _, stages = denoise_hiragana(decode_image(data), **options)
            preview = token[:2], {stage: png(image) for stage, image in stages.items()}
            st.session_state.single_preview = preview
        except ValueError as exc:
            st.error(str(exc))
            return
    stage_pngs = preview[1]
    st.subheader("Văn bản nhận diện")
    cached = st.session_state.get("single_result")
    result = cached[1] if cached and cached[0] == token else {}
    titles = {"clean": "Ảnh sạch", "original": "Ảnh gốc"}
    requested = None
    for col, (variant, title) in zip(st.columns(2), titles.items()):
        if col.button(
            f"Đọc chữ từ {title.lower()}",
            key=f"read_{variant}",
            type="primary" if variant == "clean" else "secondary",
        ):
            requested = variant
    output = st.container()
    left, right = st.columns(2)
    left.image(data, caption="Ảnh gốc")
    right.image(stage_pngs["padding"], caption="Ảnh sạch")
    with st.expander("Xem và tải ảnh các bước xử lý"):
        for stage, label in [
            ("gray", "Ảnh xám"),
            ("threshold", "Tách ngưỡng"),
            ("remove_dots_lines", "Lọc chấm và đường mảnh"),
            ("connected_components", "Lọc thành phần liên thông"),
            ("close", "Đóng hình thái"),
            ("padding", "Thêm viền"),
        ]:
            st.image(stage_pngs[stage], caption=label)
            st.download_button(
                f"Tải {label.lower()}", stage_pngs[stage], f"{stage}.png", "image/png"
            )
    if requested and requested not in result:
        try:
            message = f"Gemini đang đọc {titles[requested].lower()} · thời gian chờ đặt ở 45 giây"
            with (
                service_factory() as service,
                output,
                st.spinner(message, show_time=True),
            ):
                reading = service.recognize(
                    decode_image(stage_pngs["padding"], cv2.IMREAD_GRAYSCALE)
                    if requested == "clean"
                    else decode_image(data),
                    interactive=True,
                )
            result = {**result, requested: reading}
            st.session_state.single_result = token, result
        except Exception as exc:  # noqa: BLE001 - keep the other successful reading visible
            output.error(str(exc))
    if not result:
        if not requested:
            output.caption(
                "Chọn ảnh sạch hoặc ảnh gốc để nhận diện. Mỗi nút đọc một ảnh."
            )
        return
    with output:
        normalized = st.checkbox(
            "Hiện chữ đã chuẩn hóa", value=True, key="single_normalized_view"
        )
        if normalized:
            result = {
                variant: {
                    **reading,
                    "normalized_text": normalize_hiragana(reading["text"]),
                }
                for variant, reading in result.items()
            }
        variants = [key for key in ("original", "clean") if key in result]
        for col, variant in zip(st.columns(len(variants)), variants):
            with col:
                st.subheader(f"OCR {titles[variant].lower()}")
                show_reading(result[variant])
        st.download_button("Tải JSON", as_json(result), "ocr.json", "application/json")
        lines = [f"{titles[key]}: {result[key]['text']}" for key in variants]
        if normalized:
            lines.extend(
                f"{titles[key]} (chuẩn hóa): {result[key]['normalized_text']}"
                for key in variants
            )
        st.download_button("Tải TXT", "\n".join(lines), "ocr.txt", "text/plain")


def batch_tab(options, model, service_factory, workers):
    source = st.radio("Nguồn ảnh", ["Nhiều tệp", "Thư mục"], horizontal=True)
    files = st.file_uploader(
        "Chọn ảnh hàng loạt",
        type=EXTENSIONS,
        accept_multiple_files="directory" if source == "Thư mục" else True,
        key="batch_" + source,
    )
    if not files:
        return
    try:
        images = uploads(files)
    except ValueError as exc:
        st.error(str(exc))
        return
    token = signature(images, options, model)
    if st.button("Nhận diện hàng loạt", type="primary"):
        st.session_state.pop("batch_result", None)
        progress = st.progress(0)
        rows = []
        try:
            with service_factory() as service:

                def read_item(item):
                    name, data = item
                    try:
                        return {
                            "filename": name,
                            **recognize_clean(data, service, **options),
                            "error": "",
                        }
                    except Exception as exc:  # noqa: BLE001 - record failures for this file
                        return {"filename": name, "text": "", "error": str(exc)}

                for i, row in enumerate(
                    ordered_map(read_item, images.items(), workers), 1
                ):
                    rows.append(row)
                    progress.progress(i / len(images))
            st.session_state.batch_result = token, rows
        except Exception as exc:  # noqa: BLE001 - present service failures in the batch tab
            st.error(str(exc))
    cached = st.session_state.get("batch_result")
    if cached and cached[0] == token:
        rows = cached[1]
        normalized = st.checkbox("Hiện chữ đã chuẩn hóa", key="normalize_batch")
        if normalized:
            rows = [
                {
                    **row,
                    "normalized_text": None
                    if row.get("error")
                    else normalize_hiragana(row["text"]),
                }
                for row in rows
            ]
        st.dataframe(
            [
                {
                    "filename": r["filename"],
                    "text": r["text"],
                    **({"normalized_text": r["normalized_text"]} if normalized else {}),
                    "warning": " ".join(
                        [
                            *r.get("warnings", []),
                            normalized_length_warning(r.get("normalized_text")),
                        ]
                    ).strip(),
                    "error": r.get("error", ""),
                }
                for r in rows
            ],
            hide_index=True,
        )
        st.download_button("Tải bảng CSV", as_csv(rows), "batch.csv", "text/csv")
        st.download_button(
            "Tải bảng JSON", as_json(rows), "batch.json", "application/json"
        )


def evaluation_tab(options, model, service_factory, workers):
    source = st.radio(
        "Dữ liệu đánh giá", ["Bộ mẫu đi kèm", "Tải dữ liệu"], horizontal=True
    )
    try:
        if source == "Bộ mẫu đi kèm":
            labels = load_labels(ROOT / "data" / "labels.csv")
            images = {p.name: p.read_bytes() for p in SAMPLES.glob("*.png")}
        else:
            csv_file = st.file_uploader("Nhãn đối chứng (CSV)", type=["csv"])
            files = st.file_uploader(
                "Ảnh đối chứng", type=EXTENSIONS, accept_multiple_files=True
            )
            if not csv_file or not files:
                return
            labels = load_labels(BytesIO(csv_file.getvalue()))
            images = uploads(files)
        missing = labels.keys() - images.keys()
        if missing:
            raise ValueError("Thiếu ảnh: " + ", ".join(sorted(missing)))
    except (OSError, ValueError) as exc:
        st.error(str(exc))
        return
    token = signature(images, options, model, tuple(labels.items()))
    st.caption(
        f"{len(labels)} ảnh có nhãn · tối đa {2 * len(labels)} lượt gọi mới trước khi thử lại"
    )
    if st.button("Chạy đánh giá đối chứng", type="primary", key="run_eval"):
        st.session_state.pop("eval_result", None)
        st.session_state.pop("eval_chart", None)
        progress = st.progress(0)
        try:
            with service_factory() as service:
                rows = compare_dataset(
                    labels,
                    images,
                    decode_image,
                    service,
                    options,
                    progress.progress,
                    workers=workers,
                )
            st.session_state.eval_result = token, rows
        except Exception as exc:  # noqa: BLE001 - present evaluation failures in this tab
            st.error(str(exc))
    cached = st.session_state.get("eval_result")
    if cached and cached[0] == token:
        rows = cached[1]
        metrics, pairs = summarize(rows)
        if st.checkbox("Hiện chữ đã chuẩn hóa", key="normalize_eval"):
            rows = [
                {
                    **row,
                    "original_normalized": normalize_hiragana(row["original"]),
                    "clean_normalized": normalize_hiragana(row["clean"]),
                }
                for row in rows
            ]
            st.caption(
                "CER, Exact Match, biểu đồ và cặp nhầm dùng kết quả OCR ban đầu."
            )
        for col, row, title in zip(st.columns(2), metrics, ("Ảnh gốc", "Ảnh sạch")):
            col.subheader(title)
            col.metric("CER", f"{row['cer']:.2%}")
            col.metric("Exact Match", f"{row['exact_match']:.2%}")
        chart = st.session_state.get("eval_chart")
        if chart is None:
            chart = chart_png(metrics)
            st.session_state.eval_chart = chart
        st.image(chart)
        st.download_button("Tải biểu đồ PNG", chart, "comparison.png", "image/png")
        st.dataframe(rows, hide_index=True)
        st.download_button(
            "Tải kết quả đối chứng", as_csv(rows), "predictions.csv", "text/csv"
        )
        st.subheader("Cặp ký tự bị nhận nhầm")
        if pairs:
            st.dataframe(pairs, hide_index=True)
        else:
            st.caption("Không có phép thay thế ký tự.")


def main():
    load_dotenv(ROOT / ".env")
    st.set_page_config(page_title="OCR Hiragana", layout="wide")
    st.title("OCR Hiragana")
    with st.sidebar:
        key = st.text_input("Gemini API key", type="password") or os.getenv(
            "GEMINI_API_KEY", ""
        )
        model = st.text_input("Model Gemini", config.configured_model())
        delay = st.number_input("Khoảng cách giữa các yêu cầu (giây)", 0.0, 60.0, 1.0)
        workers = st.selectbox(
            "Số ảnh xử lý đồng thời", range(1, config.MAX_WORKERS + 1), index=1
        )
        st.header("Khử nhiễu")
        mode = st.radio("Chế độ ngưỡng", ["Auto", "Manual"], horizontal=True)
        options = {
            "mode": mode,
            "thresh_val": st.slider(
                "Ngưỡng", 20, 120, config.THRESH_VAL, disabled=mode == "Auto"
            ),
            "median_blur": st.select_slider(
                "Nhân lọc median", [1, 3, 5, 7], config.MEDIAN_BLUR
            ),
            "morph_kernel": st.select_slider(
                "Nhân hình thái", [1, 3, 5, 7], config.MORPH_KERNEL
            ),
            "min_area": st.slider("Diện tích tối thiểu (px)", 1, 100, config.MIN_AREA),
        }

    credential = sha256(key.strip().encode()).digest()
    reset = st.sidebar.button("Xóa kết quả OCR trong phiên", key="clear_ocr")
    if reset or st.session_state.get("ocr_credential") != credential:
        for field in ("single_result", "batch_result", "eval_result", "eval_chart"):
            st.session_state.pop(field, None)
        st.session_state.ocr_cache = ReadingCache()
        st.session_state.ocr_credential = credential

    def service_factory():
        return OCRService(key, model, delay, cache=st.session_state.ocr_cache)

    one, many, experiment = st.tabs(["Một ảnh", "Hàng loạt", "Đánh giá"])
    with one:
        single_tab(options, model, service_factory)
    with many:
        batch_tab(options, model, service_factory, workers)
    with experiment:
        evaluation_tab(options, model, service_factory, workers)


if __name__ == "__main__":
    main()

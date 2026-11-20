"""Paired evaluation of original and cleaned Hiragana images."""

import argparse
import os
from collections import Counter
from dataclasses import asdict
from io import BytesIO
from pathlib import Path, PurePosixPath
from time import perf_counter

import pandas as pd
from dotenv import load_dotenv
from matplotlib.figure import Figure

from config import EXPECTED_LENGTH, MAX_WORKERS, configured_model
from formatter import as_csv, as_json, hiragana
from ocr_service import OCRService, ordered_map
from preprocess import denoise_hiragana, read_image

VARIANTS = ("original", "clean")


def load_labels(source) -> dict[str, str]:
    table = pd.read_csv(source, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if not {"filename", "ground_truth"}.issubset(table.columns) or table.empty:
        raise ValueError("CSV cần có dữ liệu và hai cột filename,ground_truth.")
    labels = {}
    for name, truth in table[["filename", "ground_truth"]].itertuples(
        index=False, name=None
    ):
        name = name.strip().replace("\\", "/")
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts or ":" in name:
            raise ValueError(f"Tên ảnh không hợp lệ: {name}")
        name = path.as_posix()
        if name in labels:
            raise ValueError(f"Tên ảnh bị trùng trong nhãn: {name}")
        if len(truth) != EXPECTED_LENGTH or hiragana(truth) != truth:
            raise ValueError(f"Nhãn của {name} cần đúng sáu ký tự Hiragana.")
        labels[name] = truth
    return labels


def align(reference: str, prediction: str):
    """Levenshtein distance and substitutions in one optimal alignment."""
    costs = [list(range(len(prediction) + 1))]
    for i, char in enumerate(reference, 1):
        row = [i]
        for j, predicted in enumerate(prediction, 1):
            row.append(
                min(
                    costs[-1][j - 1] + (char != predicted),
                    costs[-1][j] + 1,
                    row[-1] + 1,
                )
            )
        costs.append(row)
    i, j = len(reference), len(prediction)
    distance = costs[i][j]
    pairs = Counter()
    while i or j:
        if (
            i
            and j
            and costs[i][j]
            == costs[i - 1][j - 1] + (reference[i - 1] != prediction[j - 1])
        ):
            if reference[i - 1] != prediction[j - 1]:
                pairs[reference[i - 1], prediction[j - 1]] += 1
            i, j = i - 1, j - 1
        elif i and costs[i][j] == costs[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    return distance, pairs


def compare_dataset(
    labels, images, load_image, service, options=None, progress=None, *, workers=1
):
    missing = labels.keys() - images.keys()
    if missing:
        raise ValueError("Thiếu ảnh có nhãn: " + ", ".join(sorted(missing)))

    def compare(item):
        name, truth = item
        try:
            image = load_image(images[name])
            clean, _ = denoise_hiragana(image, include_stages=False, **(options or {}))
            return {
                "filename": name,
                "ground_truth": truth,
                "original": service.recognize(image)["text"],
                "clean": service.recognize(clean)["text"],
            }
        except Exception as exc:
            raise ValueError(f"Đánh giá dừng ở {name}: {exc}") from exc

    rows = []
    for index, row in enumerate(ordered_map(compare, labels.items(), workers), 1):
        rows.append(row)
        if progress:
            progress(index / len(labels))
    return rows


def summarize(rows):
    reference_count = sum(len(row["ground_truth"]) for row in rows)
    if not reference_count:
        raise ValueError("Thiếu ký tự đối chứng.")
    metrics, confusions = [], []
    for variant in VARIANTS:
        errors = exact = 0
        pairs = Counter()
        for row in rows:
            distance, substitutions = align(row["ground_truth"], row[variant])
            exact += distance == 0
            errors += distance
            pairs.update(substitutions)
        metrics.append(
            {
                "variant": variant,
                "images": len(rows),
                "edits": errors,
                "cer": errors / reference_count,
                "exact_match": exact / len(rows),
            }
        )
        for (truth, pred), count in pairs.most_common():
            confusions.append(
                {
                    "variant": variant,
                    "reference": truth,
                    "prediction": pred,
                    "count": count,
                }
            )
    return metrics, confusions


def chart_png(metrics) -> bytes:
    fig = Figure(figsize=(7, 4), layout="constrained")
    axis = fig.subplots()
    for i, row in enumerate(metrics):
        x = [i * 0.36, 1 + i * 0.36]
        bars = axis.bar(
            x,
            [100 * row["cer"], 100 * row["exact_match"]],
            width=0.34,
            label={"original": "Ảnh gốc", "clean": "Ảnh sạch"}[row["variant"]],
        )
        axis.bar_label(bars, fmt="%.1f%%", padding=2)
    axis.set_xticks([0.18, 1.18], ["CER ↓", "Exact Match ↑"])
    axis.set_ylabel("Tỷ lệ (%)")
    axis.set_ylim(0, max(110, axis.get_ylim()[1] * 1.15))
    axis.legend(loc="upper center", bbox_to_anchor=(0.5, 1.18), ncol=2)
    stream = BytesIO()
    fig.savefig(stream, format="png", dpi=300)
    return stream.getvalue()


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, default=Path("data/samples"))
    parser.add_argument("--labels", type=Path, default=Path("data/labels.csv"))
    parser.add_argument("--out", type=Path, default=Path("reports"))
    parser.add_argument("--model", default=configured_model())
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument(
        "--workers",
        type=int,
        choices=range(1, MAX_WORKERS + 1),
        default=2,
        help="Số ảnh được xử lý đồng thời (mặc định 2).",
    )
    args = parser.parse_args(argv)
    try:
        labels = load_labels(args.labels)
        images = {
            p.relative_to(args.images).as_posix(): p
            for p in args.images.rglob("*")
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"}
        }
        start = perf_counter()
        with OCRService(
            os.getenv("GEMINI_API_KEY", ""), args.model, args.delay
        ) as service:
            rows = compare_dataset(
                labels, images, read_image, service, workers=args.workers
            )
        performance = {
            "model": service.model,
            "images": len(rows),
            "workers": args.workers,
            "delay_seconds": args.delay,
            "ocr_seconds": perf_counter() - start,
            **asdict(service.usage),
        }
        metrics, pairs = summarize(rows)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "predictions.csv").write_bytes(as_csv(rows))
        (args.out / "metrics.csv").write_bytes(
            as_csv([{"model": args.model, **row} for row in metrics])
        )
        pd.DataFrame(
            pairs, columns=["variant", "reference", "prediction", "count"]
        ).to_csv(args.out / "confusions.csv", index=False, encoding="utf-8-sig")
        (args.out / "comparison.png").write_bytes(chart_png(metrics))
        (args.out / "performance.json").write_text(
            as_json(performance), encoding="utf-8"
        )
        print(pd.DataFrame(metrics).to_string(index=False))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

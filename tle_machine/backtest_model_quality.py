from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PREDICTIONS_CSV_GZ = Path("data/backtest/historical_predictions.csv.gz")
BACKTEST_DIR = Path("data/backtest")
REPORT_DIR = Path("data/reports/backtest")

SUMMARY_CSV = BACKTEST_DIR / "model_quality_summary.csv"
BY_LEVEL_CSV = BACKTEST_DIR / "model_quality_by_level.csv"
BY_SURFACE_CSV = BACKTEST_DIR / "model_quality_by_surface.csv"
BY_LEVEL_SURFACE_CSV = BACKTEST_DIR / "model_quality_by_level_surface.csv"
BY_THRESHOLDS_CSV = BACKTEST_DIR / "model_quality_by_thresholds.csv"
BY_PROB_BUCKET_CSV = BACKTEST_DIR / "model_quality_by_probability_bucket.csv"
REPORT_JSON = REPORT_DIR / "model_quality_report.json"

MODELS = {
    "level_only": "prob_level_only_winner",
    "surface_only": "prob_surface_only_winner",
    "blend_80_20": "prob_blend_80_20_winner",
    "blend_70_30": "prob_blend_70_30_winner",
    "blend_60_40": "prob_blend_60_40_winner",
    "sample_aware": "prob_sample_aware_winner",
}

MIN_LEVEL_THRESHOLDS = [0, 3, 5, 10, 20, 30, 50]
MIN_SURFACE_THRESHOLDS = [0, 3, 5, 10, 20, 30, 50]

PROB_BUCKETS = [
    (0.50, 0.55),
    (0.55, 0.60),
    (0.60, 0.65),
    (0.65, 0.70),
    (0.70, 0.75),
    (0.75, 0.80),
    (0.80, 0.85),
    (0.85, 0.90),
    (0.90, 0.95),
    (0.95, 1.01),
]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_float(value: Any) -> float | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    return v


def safe_int(value: Any) -> int:
    s = str(value or "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except Exception:
        return 0


def log_loss_prob(p: float, y: int) -> float:
    p = min(max(p, 1e-12), 1 - 1e-12)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def probability_bucket(p_winner: float) -> str:
    # We evaluate selected side = higher-probability side.
    p_pick = max(p_winner, 1.0 - p_winner)
    for lo, hi in PROB_BUCKETS:
        if lo <= p_pick < hi:
            return f"{lo:.2f}-{hi if hi < 1.0 else 1.0:.2f}"
    return "other"


class Metrics:
    __slots__ = ("n", "correct", "brier_sum", "logloss_sum", "prob_winner_sum", "pick_prob_sum")

    def __init__(self) -> None:
        self.n = 0
        self.correct = 0
        self.brier_sum = 0.0
        self.logloss_sum = 0.0
        self.prob_winner_sum = 0.0
        self.pick_prob_sum = 0.0

    def add(self, p_winner: float) -> None:
        # The row is always from winner perspective, so y=1 for p_winner.
        y = 1
        pick_winner = p_winner >= 0.5
        self.n += 1
        self.correct += 1 if pick_winner else 0
        self.brier_sum += (p_winner - y) ** 2
        self.logloss_sum += log_loss_prob(p_winner, y)
        self.prob_winner_sum += p_winner
        self.pick_prob_sum += max(p_winner, 1.0 - p_winner)

    def row(self, extra: dict[str, Any]) -> dict[str, Any]:
        n = self.n
        if n <= 0:
            return {
                **extra,
                "matches": 0,
                "accuracy": "",
                "brier": "",
                "log_loss": "",
                "avg_prob_winner": "",
                "avg_pick_prob": "",
            }

        return {
            **extra,
            "matches": n,
            "accuracy": round(self.correct / n, 6),
            "brier": round(self.brier_sum / n, 6),
            "log_loss": round(self.logloss_sum / n, 6),
            "avg_prob_winner": round(self.prob_winner_sum / n, 6),
            "avg_pick_prob": round(self.pick_prob_sum / n, 6),
        }


def read_predictions(path: Path) -> Iterable[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def min_count(row: dict[str, str], a: str, b: str) -> int:
    return min(safe_int(row.get(a)), safe_int(row.get(b)))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def add_metric(groups: dict[tuple[Any, ...], Metrics], key: tuple[Any, ...], p: float) -> None:
    groups[key].add(p)


def model_is_eligible(model: str, row: dict[str, str]) -> bool:
    # Surface models need a known surface and a probability.
    if model in {"surface_only", "blend_80_20", "blend_70_30", "blend_60_40", "sample_aware"}:
        surface = str(row.get("surface", "")).strip().lower()
        if surface in {"", "unknown"}:
            return False
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, default=PREDICTIONS_CSV_GZ)
    args = parser.parse_args(argv)

    if not args.predictions.exists():
        raise FileNotFoundError(f"Missing predictions file: {args.predictions}")

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    overall: dict[tuple[Any, ...], Metrics] = defaultdict(Metrics)
    by_level: dict[tuple[Any, ...], Metrics] = defaultdict(Metrics)
    by_surface: dict[tuple[Any, ...], Metrics] = defaultdict(Metrics)
    by_level_surface: dict[tuple[Any, ...], Metrics] = defaultdict(Metrics)
    by_thresholds: dict[tuple[Any, ...], Metrics] = defaultdict(Metrics)
    by_prob_bucket: dict[tuple[Any, ...], Metrics] = defaultdict(Metrics)

    counters = Counter()

    for row in read_predictions(args.predictions):
        counters["rows"] += 1
        level = str(row.get("level", "")).strip().lower()
        surface = str(row.get("surface", "")).strip().lower()
        gender = str(row.get("gender", "")).strip().lower()

        level_min = min_count(row, "winner_level_matches", "loser_level_matches")
        surface_min = min_count(row, "winner_surface_matches", "loser_surface_matches")

        for model, col in MODELS.items():
            if not model_is_eligible(model, row):
                counters[f"skipped_{model}_not_eligible"] += 1
                continue

            p = safe_float(row.get(col))
            if p is None:
                counters[f"skipped_{model}_missing_prob"] += 1
                continue

            counters[f"model_rows_{model}"] += 1

            add_metric(overall, (model,), p)
            add_metric(by_level, (model, level), p)
            add_metric(by_surface, (model, surface), p)
            add_metric(by_level_surface, (model, level, surface), p)
            add_metric(by_prob_bucket, (model, probability_bucket(p)), p)

            for min_level in MIN_LEVEL_THRESHOLDS:
                if level_min < min_level:
                    continue
                for min_surface in MIN_SURFACE_THRESHOLDS:
                    # For level_only, surface threshold is not meaningful but still useful
                    # to compare whether surface experience improves performance.
                    if surface_min < min_surface:
                        continue
                    add_metric(by_thresholds, (model, min_level, min_surface), p)

    summary_rows = [
        metrics.row({"model": model})
        for (model,), metrics in sorted(overall.items())
    ]
    summary_rows.sort(key=lambda r: (float(r["log_loss"]) if r["log_loss"] != "" else 999, float(r["brier"]) if r["brier"] != "" else 999))

    by_level_rows = [
        metrics.row({"model": model, "level": level})
        for (model, level), metrics in sorted(by_level.items())
    ]
    by_surface_rows = [
        metrics.row({"model": model, "surface": surface})
        for (model, surface), metrics in sorted(by_surface.items())
    ]
    by_level_surface_rows = [
        metrics.row({"model": model, "level": level, "surface": surface})
        for (model, level, surface), metrics in sorted(by_level_surface.items())
    ]
    by_threshold_rows = [
        metrics.row({"model": model, "min_level_matches": min_level, "min_surface_matches": min_surface})
        for (model, min_level, min_surface), metrics in sorted(by_thresholds.items())
    ]
    by_threshold_rows.sort(key=lambda r: (
        r["model"],
        int(r["min_level_matches"]),
        int(r["min_surface_matches"]),
        float(r["log_loss"]) if r["log_loss"] != "" else 999,
    ))

    by_prob_bucket_rows = [
        metrics.row({"model": model, "probability_bucket": bucket})
        for (model, bucket), metrics in sorted(by_prob_bucket.items())
    ]

    metric_fields = ["matches", "accuracy", "brier", "log_loss", "avg_prob_winner", "avg_pick_prob"]

    write_csv(SUMMARY_CSV, summary_rows, ["model", *metric_fields])
    write_csv(BY_LEVEL_CSV, by_level_rows, ["model", "level", *metric_fields])
    write_csv(BY_SURFACE_CSV, by_surface_rows, ["model", "surface", *metric_fields])
    write_csv(BY_LEVEL_SURFACE_CSV, by_level_surface_rows, ["model", "level", "surface", *metric_fields])
    write_csv(BY_THRESHOLDS_CSV, by_threshold_rows, ["model", "min_level_matches", "min_surface_matches", *metric_fields])
    write_csv(BY_PROB_BUCKET_CSV, by_prob_bucket_rows, ["model", "probability_bucket", *metric_fields])

    best_by_logloss = summary_rows[0] if summary_rows else {}
    best_by_brier = sorted(summary_rows, key=lambda r: float(r["brier"]) if r["brier"] != "" else 999)[0] if summary_rows else {}
    best_by_accuracy = sorted(summary_rows, key=lambda r: float(r["accuracy"]) if r["accuracy"] != "" else -1, reverse=True)[0] if summary_rows else {}

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "predictions": str(args.predictions),
        "models": list(MODELS.keys()),
        "counters": dict(sorted(counters.items())),
        "best": {
            "log_loss": best_by_logloss,
            "brier": best_by_brier,
            "accuracy": best_by_accuracy,
        },
        "outputs": {
            "summary_csv": str(SUMMARY_CSV),
            "by_level_csv": str(BY_LEVEL_CSV),
            "by_surface_csv": str(BY_SURFACE_CSV),
            "by_level_surface_csv": str(BY_LEVEL_SURFACE_CSV),
            "by_thresholds_csv": str(BY_THRESHOLDS_CSV),
            "by_probability_bucket_csv": str(BY_PROB_BUCKET_CSV),
            "report_json": str(REPORT_JSON),
        },
        "notes": [
            "Rows are evaluated from winner perspective. Accuracy is whether the model gave the winner probability >= 0.5.",
            "No odds/ROI here: this is model-quality backtest only.",
            "Surface models skip unknown-surface matches.",
            "Threshold table uses minimum pre-match level/surface match count across the two players.",
        ],
    }

    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

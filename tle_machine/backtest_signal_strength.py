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

OUT_CSV = BACKTEST_DIR / "signal_strength_blend_80_20.csv"
OUT_BY_LEVEL_CSV = BACKTEST_DIR / "signal_strength_blend_80_20_by_level.csv"
OUT_BY_SURFACE_CSV = BACKTEST_DIR / "signal_strength_blend_80_20_by_surface.csv"
REPORT_JSON = REPORT_DIR / "signal_strength_blend_80_20_report.json"

MODEL_COL = "prob_blend_80_20_winner"
MODEL_NAME = "blend_80_20"

DEFAULT_MIN_LEVEL = 10
DEFAULT_MIN_SURFACE = 5

PROB_THRESHOLDS = [0.50, 0.52, 0.55, 0.57, 0.60, 0.62, 0.65, 0.67, 0.70, 0.72, 0.75, 0.80]


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


class Metrics:
    __slots__ = ("n", "correct", "brier_sum", "logloss_sum", "pick_prob_sum", "winner_prob_sum")

    def __init__(self) -> None:
        self.n = 0
        self.correct = 0
        self.brier_sum = 0.0
        self.logloss_sum = 0.0
        self.pick_prob_sum = 0.0
        self.winner_prob_sum = 0.0

    def add(self, p_winner: float) -> None:
        pick_prob = max(p_winner, 1.0 - p_winner)
        pick_is_winner = p_winner >= 0.5
        self.n += 1
        self.correct += 1 if pick_is_winner else 0
        self.brier_sum += (p_winner - 1.0) ** 2
        self.logloss_sum += log_loss_prob(p_winner, 1)
        self.pick_prob_sum += pick_prob
        self.winner_prob_sum += p_winner

    def as_row(self, extra: dict[str, Any]) -> dict[str, Any]:
        if self.n <= 0:
            return {
                **extra,
                "matches": 0,
                "accuracy": "",
                "brier": "",
                "log_loss": "",
                "avg_pick_prob": "",
                "avg_winner_prob": "",
            }
        return {
            **extra,
            "matches": self.n,
            "accuracy": round(self.correct / self.n, 6),
            "brier": round(self.brier_sum / self.n, 6),
            "log_loss": round(self.logloss_sum / self.n, 6),
            "avg_pick_prob": round(self.pick_prob_sum / self.n, 6),
            "avg_winner_prob": round(self.winner_prob_sum / self.n, 6),
        }


def iter_predictions(path: Path) -> Iterable[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def min_count(row: dict[str, str], a: str, b: str) -> int:
    return min(safe_int(row.get(a)), safe_int(row.get(b)))


def is_eligible(row: dict[str, str], min_level: int, min_surface: int) -> tuple[bool, str]:
    surface = str(row.get("surface", "")).strip().lower()
    if surface in {"", "unknown"}:
        return False, "unknown_surface"

    p = safe_float(row.get(MODEL_COL))
    if p is None:
        return False, "missing_probability"

    level_min = min_count(row, "winner_level_matches", "loser_level_matches")
    surface_min = min_count(row, "winner_surface_matches", "loser_surface_matches")

    if level_min < min_level:
        return False, "min_level_not_met"
    if surface_min < min_surface:
        return False, "min_surface_not_met"

    return True, ""


def selected_prob(p_winner: float) -> float:
    return max(p_winner, 1.0 - p_winner)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, default=PREDICTIONS_CSV_GZ)
    parser.add_argument("--min-level-matches", type=int, default=DEFAULT_MIN_LEVEL)
    parser.add_argument("--min-surface-matches", type=int, default=DEFAULT_MIN_SURFACE)
    args = parser.parse_args(argv)

    if not args.predictions.exists():
        raise FileNotFoundError(f"Missing predictions file: {args.predictions}")

    overall: dict[float, Metrics] = defaultdict(Metrics)
    by_level: dict[tuple[float, str], Metrics] = defaultdict(Metrics)
    by_surface: dict[tuple[float, str], Metrics] = defaultdict(Metrics)

    counters = Counter()
    level_counts = Counter()
    surface_counts = Counter()

    for row in iter_predictions(args.predictions):
        counters["rows"] += 1
        ok, reason = is_eligible(row, args.min_level_matches, args.min_surface_matches)
        if not ok:
            counters[f"skipped_{reason}"] += 1
            continue

        p_winner = safe_float(row.get(MODEL_COL))
        if p_winner is None:
            counters["skipped_missing_probability"] += 1
            continue

        p_pick = selected_prob(p_winner)
        level = str(row.get("level", "")).strip().lower()
        surface = str(row.get("surface", "")).strip().lower()

        counters["eligible"] += 1
        level_counts[level] += 1
        surface_counts[surface] += 1

        for threshold in PROB_THRESHOLDS:
            if p_pick < threshold:
                continue
            overall[threshold].add(p_winner)
            by_level[(threshold, level)].add(p_winner)
            by_surface[(threshold, surface)].add(p_winner)

    rows = [
        metrics.as_row({"model": MODEL_NAME, "min_level_matches": args.min_level_matches, "min_surface_matches": args.min_surface_matches, "pick_prob_threshold": threshold})
        for threshold, metrics in sorted(overall.items())
    ]

    by_level_rows = [
        metrics.as_row({"model": MODEL_NAME, "level": level, "min_level_matches": args.min_level_matches, "min_surface_matches": args.min_surface_matches, "pick_prob_threshold": threshold})
        for (threshold, level), metrics in sorted(by_level.items())
    ]

    by_surface_rows = [
        metrics.as_row({"model": MODEL_NAME, "surface": surface, "min_level_matches": args.min_level_matches, "min_surface_matches": args.min_surface_matches, "pick_prob_threshold": threshold})
        for (threshold, surface), metrics in sorted(by_surface.items())
    ]

    metric_fields = ["matches", "accuracy", "brier", "log_loss", "avg_pick_prob", "avg_winner_prob"]

    write_csv(OUT_CSV, rows, ["model", "min_level_matches", "min_surface_matches", "pick_prob_threshold", *metric_fields])
    write_csv(OUT_BY_LEVEL_CSV, by_level_rows, ["model", "level", "min_level_matches", "min_surface_matches", "pick_prob_threshold", *metric_fields])
    write_csv(OUT_BY_SURFACE_CSV, by_surface_rows, ["model", "surface", "min_level_matches", "min_surface_matches", "pick_prob_threshold", *metric_fields])

    best_accuracy = sorted(rows, key=lambda r: (float(r["accuracy"]) if r["accuracy"] != "" else -1, int(r["matches"])), reverse=True)[0] if rows else {}
    best_logloss = sorted(rows, key=lambda r: (float(r["log_loss"]) if r["log_loss"] != "" else 999, -int(r["matches"])))[0] if rows else {}

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "predictions": str(args.predictions),
        "model": MODEL_NAME,
        "model_col": MODEL_COL,
        "min_level_matches": args.min_level_matches,
        "min_surface_matches": args.min_surface_matches,
        "thresholds": PROB_THRESHOLDS,
        "counters": dict(sorted(counters.items())),
        "eligible_by_level": dict(sorted(level_counts.items())),
        "eligible_by_surface": dict(sorted(surface_counts.items())),
        "best": {
            "accuracy": best_accuracy,
            "log_loss": best_logloss,
        },
        "outputs": {
            "summary_csv": str(OUT_CSV),
            "by_level_csv": str(OUT_BY_LEVEL_CSV),
            "by_surface_csv": str(OUT_BY_SURFACE_CSV),
            "report_json": str(REPORT_JSON),
        },
        "notes": [
            "Signal threshold uses selected-side probability: max(p_winner, 1-p_winner).",
            "Rows are still evaluated from winner perspective, so accuracy means selected side was the winner.",
            "No odds/ROI here. This checks whether stronger model probability corresponds to higher hit rate.",
        ],
    }

    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DETAIL_CSV = Path("data/backtest/external_odds_comparison.csv")
OUT_MISSES_CSV = Path("data/backtest/external_odds_misses.csv")
OUT_UNMAPPED_CSV = Path("data/backtest/external_odds_unmapped_players.csv")
OUT_NO_MATCH_CSV = Path("data/backtest/external_odds_no_prediction_match.csv")
OUT_REPORT_JSON = Path("data/reports/backtest/external_odds_misses_report.json")


MISS_FIELDS = [
    "category",
    "date",
    "time",
    "gender",
    "tour_level",
    "tournament",
    "round",
    "match",
    "player_name",
    "opponent_name",
    "player_key",
    "opponent_key",
    "player_api_key",
    "opponent_api_key",
    "player_canonical_key",
    "opponent_canonical_key",
    "mapping_status",
    "match_status",
    "elo_no_bet_reason",
    "odds",
    "result",
    "old_profit",
    "stake",
]

UNMAPPED_FIELDS = [
    "side",
    "api_key",
    "api_player_id",
    "gender",
    "player_name",
    "appearances",
    "wins",
    "losses",
    "total_stake",
    "total_profit",
    "sample_matches",
]

NO_MATCH_FIELDS = [
    "date",
    "gender",
    "tour_level",
    "tournament",
    "round",
    "match",
    "player_name",
    "opponent_name",
    "player_key",
    "opponent_key",
    "player_api_key",
    "opponent_api_key",
    "player_canonical_key",
    "opponent_canonical_key",
    "odds",
    "result",
    "old_profit",
    "stake",
]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_float(x: Any) -> float:
    try:
        s = str(x or "").strip()
        if not s:
            return 0.0
        return float(s)
    except Exception:
        return 0.0


def safe_str(x: Any) -> str:
    return str(x or "").strip()


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing detail CSV: {path}. Run TLE 16 first.")
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def category_for(row: dict[str, str]) -> str:
    reason = safe_str(row.get("elo_no_bet_reason"))
    mapping = safe_str(row.get("mapping_status"))
    match_status = safe_str(row.get("match_status"))

    if mapping != "mapped":
        return "unmapped_player"
    if match_status == "no_prediction_match" or reason == "no_prediction_match":
        return "no_prediction_match"
    if reason:
        return reason
    if safe_str(row.get("elo_eligible")).lower() != "true":
        return "other_no_bet"
    return "eligible"


def api_id_from_key(api_key: str) -> str:
    # men:api:123 -> 123
    parts = safe_str(api_key).split(":")
    return parts[-1] if parts else ""


def add_unmapped(summary: dict[tuple[str, str], dict[str, Any]], *, side: str, row: dict[str, str]) -> None:
    api_key = safe_str(row.get(f"{side}_api_key"))
    name = safe_str(row.get(f"{side}_name"))
    if not api_key:
        return

    key = (side, api_key)
    item = summary.setdefault(
        key,
        {
            "side": side,
            "api_key": api_key,
            "api_player_id": api_id_from_key(api_key),
            "gender": safe_str(row.get("gender")),
            "player_name": name,
            "appearances": 0,
            "wins": 0,
            "losses": 0,
            "total_stake": 0.0,
            "total_profit": 0.0,
            "sample_matches": [],
        },
    )

    item["appearances"] += 1
    if safe_str(row.get("result")).lower() == "win":
        item["wins"] += 1
    elif safe_str(row.get("result")).lower() == "loss":
        item["losses"] += 1

    item["total_stake"] += safe_float(row.get("stake"))
    item["total_profit"] += safe_float(row.get("old_profit"))

    if len(item["sample_matches"]) < 5:
        item["sample_matches"].append(
            f"{safe_str(row.get('date'))} | {safe_str(row.get('match'))} | pick={safe_str(row.get('player_name'))}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--detail-csv", type=Path, default=DETAIL_CSV)
    args = parser.parse_args()

    rows = read_rows(args.detail_csv)

    counters = Counter()
    by_level = Counter()
    by_mapping_status = Counter()
    by_match_status = Counter()
    by_reason = Counter()
    misses: list[dict[str, Any]] = []
    no_match_rows: list[dict[str, Any]] = []
    unmapped: dict[tuple[str, str], dict[str, Any]] = {}

    for row in rows:
        cat = category_for(row)
        counters[cat] += 1
        by_level[(cat, safe_str(row.get("tour_level") or row.get("level") or "unknown"))] += 1
        by_mapping_status[safe_str(row.get("mapping_status") or "unknown")] += 1
        by_match_status[safe_str(row.get("match_status") or "unknown")] += 1
        by_reason[safe_str(row.get("elo_no_bet_reason") or "eligible")] += 1

        if cat != "eligible":
            m = {"category": cat, **row}
            misses.append(m)

        mapping = safe_str(row.get("mapping_status"))
        if mapping != "mapped":
            if "player_unmapped" in mapping or "both_unmapped" in mapping:
                add_unmapped(unmapped, side="player", row=row)
            if "opponent_unmapped" in mapping or "both_unmapped" in mapping:
                add_unmapped(unmapped, side="opponent", row=row)

        if cat == "no_prediction_match":
            no_match_rows.append(row)

    unmapped_rows = []
    for item in unmapped.values():
        item = dict(item)
        item["total_stake"] = round(item["total_stake"], 6)
        item["total_profit"] = round(item["total_profit"], 6)
        item["sample_matches"] = " || ".join(item["sample_matches"])
        unmapped_rows.append(item)
    unmapped_rows.sort(key=lambda r: (-int(r["appearances"]), safe_str(r["player_name"]).lower()))

    no_match_rows.sort(key=lambda r: (safe_str(r.get("date")), safe_str(r.get("gender")), safe_str(r.get("match"))))
    misses.sort(key=lambda r: (safe_str(r.get("category")), safe_str(r.get("date")), safe_str(r.get("match"))))

    write_csv(OUT_MISSES_CSV, misses, MISS_FIELDS)
    write_csv(OUT_UNMAPPED_CSV, unmapped_rows, UNMAPPED_FIELDS)
    write_csv(OUT_NO_MATCH_CSV, no_match_rows, NO_MATCH_FIELDS)

    top_no_match_dates = Counter(safe_str(r.get("date")) for r in no_match_rows).most_common(20)
    top_no_match_tournaments = Counter(safe_str(r.get("tournament") or "unknown") for r in no_match_rows).most_common(20)
    top_no_match_levels = Counter(safe_str(r.get("tour_level") or "unknown") for r in no_match_rows).most_common()

    report = {
        "status": "ok",
        "generated_at": now_utc_iso(),
        "input_detail_csv": str(args.detail_csv),
        "rows": len(rows),
        "category_counts": dict(sorted(counters.items())),
        "mapping_status_counts": dict(sorted(by_mapping_status.items())),
        "match_status_counts": dict(sorted(by_match_status.items())),
        "elo_no_bet_reason_counts": dict(sorted(by_reason.items())),
        "category_by_level": {
            f"{cat}|{level}": count
            for (cat, level), count in sorted(by_level.items())
        },
        "unmapped_players_total": len(unmapped_rows),
        "top_unmapped_players": unmapped_rows[:30],
        "no_prediction_match_total": len(no_match_rows),
        "top_no_prediction_match_dates": top_no_match_dates,
        "top_no_prediction_match_tournaments": top_no_match_tournaments,
        "top_no_prediction_match_levels": top_no_match_levels,
        "outputs": {
            "misses_csv": str(OUT_MISSES_CSV),
            "unmapped_players_csv": str(OUT_UNMAPPED_CSV),
            "no_prediction_match_csv": str(OUT_NO_MATCH_CSV),
            "report_json": str(OUT_REPORT_JSON),
        },
        "notes": [
            "Use unmapped_players_csv to decide manual API->Sackmann overrides.",
            "Use no_prediction_match_csv to see whether misses are missing historical API results, date/fixture mismatches, or level/gender mapping issues.",
            "This script only analyzes outputs from TLE 16; it does not recompute Elo.",
        ],
    }

    write_json(OUT_REPORT_JSON, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

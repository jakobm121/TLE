from __future__ import annotations

import argparse
import csv
import json
import math
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_RESULTS_URL = "https://raw.githubusercontent.com/jakobm121/Ai/refs/heads/main/data/tennis_results.json"

BASE_DIR = Path("data/elo_filter")
REPORT_DIR = Path("data/reports/elo_filter")

PREDICTIONS_JSON = BASE_DIR / "predictions.json"
RESULTS_JSON = BASE_DIR / "results.json"
ACTIVE_CSV = BASE_DIR / "active_predictions.csv"
RESULTS_CSV = BASE_DIR / "results.csv"
REPORT_JSON = REPORT_DIR / "settle_report.json"

ACTIVE_FIELDS = [
    "pick_id", "status", "decision", "confidence", "reason",
    "date", "time", "gender", "level", "surface", "tournament", "round",
    "match", "pick", "opponent", "side",
    "odds", "implied_prob", "old_model_prob", "old_edge",
    "tle_model", "tle_prob", "tle_edge",
    "tle_min_level_matches", "tle_min_surface_matches",
    "stake", "stake_label", "best_bookmaker", "market_median_odds", "bookmakers_used",
    "player_key", "opponent_key", "player_canonical_key", "opponent_canonical_key",
    "created_at", "tle_created_at",
]

RESULTS_FIELDS = [
    *ACTIVE_FIELDS,
    "result", "profit", "roi", "settled_at", "final_score", "event_winner",
    "running_wins", "running_losses", "running_w_l", "running_total_stake",
    "running_total_profit", "running_roi",
]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_str(x: Any) -> str:
    return str(x or "").strip()


def safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(str(x).strip())
    except Exception:
        return None
    return v if math.isfinite(v) else None


def read_json_url_or_path(source: str | Path) -> Any:
    s = str(source)
    if s.startswith("http://") or s.startswith("https://"):
        req = urllib.request.Request(
            s,
            headers={
                "User-Agent": "TLE-elo-filter-settle/1.0",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    path = Path(s)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("picks"), list):
        return [x for x in payload["picks"] if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def extract_source_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("results", "picks", "settled", "predictions"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def key_variants(item: dict[str, Any]) -> list[str]:
    keys = []
    for k in ("pick_id", "event_key", "fixture_id"):
        v = safe_str(item.get(k))
        if v:
            keys.append(f"{k}:{v}")

    combo = "|".join([
        safe_str(item.get("date")),
        safe_str(item.get("player_key")),
        safe_str(item.get("opponent_key")),
        safe_str(item.get("side")),
    ])
    if combo.strip("|"):
        keys.append(f"combo:{combo}")

    return keys


def build_source_index(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for item in items:
        result = safe_str(item.get("result")).lower()
        if result not in {"win", "loss", "void", "push"}:
            continue
        for k in key_variants(item):
            out[k] = item
    return out


def compute_profit(row: dict[str, Any], src: dict[str, Any]) -> tuple[str, float | None, float | None]:
    result = safe_str(src.get("result") or row.get("result")).lower()
    stake = safe_float(row.get("stake"))
    odds = safe_float(row.get("odds"))
    profit = safe_float(src.get("profit"))

    if profit is None and stake is not None and odds is not None:
        if result == "win":
            profit = stake * (odds - 1.0)
        elif result == "loss":
            profit = -stake
        elif result in {"void", "push"}:
            profit = 0.0

    roi = profit / stake if profit is not None and stake not in {None, 0} else None
    return result, profit, roi


def sort_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        safe_str(row.get("date")),
        safe_str(row.get("time")),
        safe_str(row.get("tournament")),
        safe_str(row.get("match")),
    )


def add_running_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add cumulative W-L, stake, profit, and ROI to settled rows in chronological order.

    Pending rows remain in the ledger but do not change running totals.
    """
    running_wins = 0
    running_losses = 0
    running_stake = 0.0
    running_profit = 0.0

    out = []
    for row in sorted(rows, key=sort_key):
        r = dict(row)
        status = safe_str(r.get("status") or r.get("result")).lower()

        if status in {"win", "loss", "void", "push"}:
            if status == "win":
                running_wins += 1
            elif status == "loss":
                running_losses += 1

            stake = safe_float(r.get("stake")) or 0.0
            profit = safe_float(r.get("profit")) or 0.0

            # Void/push can have stake in the row but should not distort ROI if profit is 0.
            # We keep the original stake accounting, same as the rest of this project.
            running_stake += stake
            running_profit += profit

            r["running_wins"] = running_wins
            r["running_losses"] = running_losses
            r["running_w_l"] = f"{running_wins}-{running_losses}"
            r["running_total_stake"] = round(running_stake, 6)
            r["running_total_profit"] = round(running_profit, 6)
            r["running_roi"] = round(running_profit / running_stake, 6) if running_stake else None
        else:
            r["running_wins"] = running_wins
            r["running_losses"] = running_losses
            r["running_w_l"] = f"{running_wins}-{running_losses}"
            r["running_total_stake"] = round(running_stake, 6)
            r["running_total_profit"] = round(running_profit, 6)
            r["running_roi"] = round(running_profit / running_stake, 6) if running_stake else None

        out.append(r)

    return out


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-source", default=DEFAULT_RESULTS_URL)
    args = parser.parse_args()

    predictions_payload = read_json_url_or_path(PREDICTIONS_JSON) or {"picks": []}
    results_payload = read_json_url_or_path(RESULTS_JSON) or {"picks": []}

    active = payload_items(predictions_payload)
    historical = payload_items(results_payload)

    source_payload = read_json_url_or_path(args.results_source)
    source_items = extract_source_results(source_payload)
    source_index = build_source_index(source_items)

    historical_by_id = {safe_str(r.get("pick_id")): r for r in historical if safe_str(r.get("pick_id"))}
    still_active = []
    settled_now = []
    counters = Counter()

    for row in active:
        pid = safe_str(row.get("pick_id"))
        match = None
        for k in key_variants(row):
            if k in source_index:
                match = source_index[k]
                break

        if not match:
            still_active.append(row)
            counters["still_pending"] += 1
            continue

        result, profit, roi = compute_profit(row, match)
        updated = {
            **row,
            "status": result,
            "result": result,
            "profit": None if profit is None else round(profit, 6),
            "roi": None if roi is None else round(roi, 6),
            "settled_at": match.get("settled_at") or now_utc_iso(),
            "final_score": match.get("final_score"),
            "event_winner": match.get("event_winner"),
        }
        historical_by_id[pid] = updated
        settled_now.append(updated)
        counters[f"settled_{result}"] += 1

    for row in still_active:
        pid = safe_str(row.get("pick_id"))
        if pid and pid not in historical_by_id:
            historical_by_id[pid] = row

    all_results_raw = list(historical_by_id.values())
    all_results = add_running_totals(all_results_raw)
    still_active = sorted(still_active, key=sort_key)

    settled_all = [r for r in all_results if safe_str(r.get("status")).lower() in {"win", "loss", "void", "push"}]
    wins = sum(1 for r in settled_all if safe_str(r.get("status")).lower() == "win")
    losses = sum(1 for r in settled_all if safe_str(r.get("status")).lower() == "loss")
    stake = sum(safe_float(r.get("stake")) or 0.0 for r in settled_all)
    profit = sum(safe_float(r.get("profit")) or 0.0 for r in settled_all)
    settled_count = wins + losses

    predictions_out = {
        "generated_at": now_utc_iso(),
        "model": "TLE Elo Filter v1",
        "summary": {
            "active_picks": len(still_active),
            "settled_removed_this_run": len(settled_now),
        },
        "picks": still_active,
    }
    results_out = {
        "generated_at": now_utc_iso(),
        "model": "TLE Elo Filter v1",
        "summary": {
            "total_tracked": len(all_results),
            "pending": len(still_active),
            "settled": len(settled_all),
            "wins": wins,
            "losses": losses,
            "w_l": f"{wins}-{losses}",
            "hit_rate": round(wins / settled_count, 6) if settled_count else None,
            "total_stake": round(stake, 6),
            "total_profit": round(profit, 6),
            "roi": round(profit / stake, 6) if stake else None,
        },
        "picks": all_results,
    }

    write_json(PREDICTIONS_JSON, predictions_out)
    write_json(RESULTS_JSON, results_out)
    write_csv(ACTIVE_CSV, still_active, ACTIVE_FIELDS)
    write_csv(RESULTS_CSV, all_results, RESULTS_FIELDS)

    report = {
        "status": "ok",
        "generated_at": now_utc_iso(),
        "source": args.results_source,
        "active_before": len(active),
        "settled_this_run": len(settled_now),
        "active_after": len(still_active),
        "source_settled_rows": len(source_index),
        "settle_counts": dict(sorted(counters.items())),
        "roi_summary": results_out["summary"],
        "outputs": {
            "predictions_json": str(PREDICTIONS_JSON),
            "results_json": str(RESULTS_JSON),
            "active_csv": str(ACTIVE_CSV),
            "results_csv": str(RESULTS_CSV),
            "report_json": str(REPORT_JSON),
        },
        "notes": [
            "Settled picks are removed from predictions.json.",
            "results.json is the ledger and keeps both pending and settled tracked picks.",
            "results.csv includes running W-L, running total profit, and running ROI after each row.",
        ],
    }
    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


BASE_URL = "https://api.api-tennis.com/tennis/"
API_KEY = (
    os.getenv("TENNIS_API_KEY")
    or os.getenv("API_KEY")
    or os.getenv("API_TENNIS_KEY")
    or os.getenv("APITENNIS_KEY")
    or os.getenv("API_TENNIS_API_KEY")
    or os.getenv("TENNIS_VALUE_API_KEY")
)

BASE_DIR = Path("data/elo_standalone")
REPORT_DIR = Path("data/reports/elo_standalone")

PREDICTIONS_JSON = BASE_DIR / "predictions.json"
RESULTS_JSON = BASE_DIR / "results.json"
ACTIVE_CSV = BASE_DIR / "active_predictions.csv"
RESULTS_CSV = BASE_DIR / "results.csv"
RESULTS_MD = BASE_DIR / "results.md"
REPORT_JSON = REPORT_DIR / "settle_report.json"

DEFAULT_ACTIVE_FIELDS = [
    "pick_id", "status", "decision", "confidence", "reason",
    "date", "time", "gender", "level", "surface", "surface_source",
    "tournament", "round", "match", "pick", "opponent", "side",
    "odds", "avg_odds", "implied_prob",
    "tle_model", "tle_prob", "tle_edge",
    "tle_min_level_matches", "tle_min_surface_matches",
    "stake", "stake_label", "best_bookmaker",
    "player_key", "opponent_key", "player_api_key", "opponent_api_key",
    "player_canonical_key", "opponent_canonical_key",
    "created_at", "tle_created_at",
]

RESULT_EXTRA_FIELDS = [
    "result", "profit", "roi", "settled_at", "final_score", "event_status",
    "event_winner", "event_winner_key", "running_wins", "running_losses",
    "running_w_l", "running_total_stake", "running_total_profit", "running_roi",
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


def safe_int(x: Any) -> int | None:
    try:
        s = str(x).strip()
        if not s:
            return None
        if "." in s:
            s = s.split(".", 1)[0]
        return int(s)
    except Exception:
        return None


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
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


def existing_csv_fields(path: Path, fallback: list[str]) -> list[str]:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.reader(fh)
                header = next(reader, None)
                if header:
                    return [h for h in header if h]
        except Exception:
            pass
    return list(fallback)


def merge_fields(preferred: list[str], rows: list[dict[str, Any]], extras: list[str] | None = None) -> list[str]:
    out = []
    seen = set()
    for f in preferred + (extras or []):
        if f and f not in seen:
            out.append(f)
            seen.add(f)
    for r in rows:
        for k in r.keys():
            if k not in seen:
                out.append(k)
                seen.add(k)
    return out


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})


def api_call(params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
    if not API_KEY:
        raise RuntimeError(
            "Missing API key. Expose API_TENNIS_KEY or TENNIS_API_KEY in the settle workflow env."
        )

    p = {k: v for k, v in params.items() if v is not None}
    p["APIkey"] = API_KEY
    url = BASE_URL + "?" + urllib.parse.urlencode(p)

    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "TLE-elo-standalone-settle/4.0",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))

    return {}


def fetch_fixtures_for_date(date_s: str) -> list[dict[str, Any]]:
    data = api_call({
        "method": "get_fixtures",
        "date_start": date_s,
        "date_stop": date_s,
    })
    if data.get("success") != 1:
        return []
    result = data.get("result") or []
    return result if isinstance(result, list) else []


def build_fixture_index(dates: list[str]) -> tuple[dict[str, dict[str, Any]], Counter]:
    index: dict[str, dict[str, Any]] = {}
    counters = Counter()
    for date_s in sorted(set(d for d in dates if d)):
        fixtures = fetch_fixtures_for_date(date_s)
        counters["dates_fetched"] += 1
        counters["fixtures_fetched"] += len(fixtures)
        for fx in fixtures:
            event_key = safe_str(fx.get("event_key"))
            if event_key:
                index[event_key] = fx
        time.sleep(0.25)
    return index, counters


def normalize_status(fixture: dict[str, Any]) -> str:
    raw = " ".join([
        safe_str(fixture.get("event_status")),
        safe_str(fixture.get("event_status_info")),
        safe_str(fixture.get("event_status_type")),
        safe_str(fixture.get("status")),
    ]).lower()
    raw = raw.strip()
    if not raw:
        return "unknown"

    if any(x in raw for x in ["finished", "ended", "complete", "completed", "final"]):
        return "finished"
    if any(x in raw for x in ["cancel", "canceled", "cancelled", "postpon", "suspend", "interrupt", "abandon"]):
        return "void"
    if any(x in raw for x in ["retired", "walkover", "w/o", "wo"]):
        return "void"
    if any(x in raw for x in ["not started", "not_started", "scheduled", "upcoming", "pending"]):
        return "pending"
    if any(x in raw for x in ["live", "inplay", "in play", "set", "break"]):
        return "pending"
    return raw


def fixture_score(fixture: dict[str, Any]) -> str:
    for key in ("event_final_result", "event_result", "final_score", "score", "result"):
        v = safe_str(fixture.get(key))
        if v:
            return v

    scores = fixture.get("scores")
    if isinstance(scores, list):
        parts = []
        for s in scores:
            if not isinstance(s, dict):
                continue
            a = s.get("score_first") or s.get("home_score") or s.get("first_score")
            b = s.get("score_second") or s.get("away_score") or s.get("second_score")
            if a is not None and b is not None:
                parts.append(f"{a}-{b}")
        if parts:
            return " ".join(parts)

    return ""


def winner_side_from_fixture(fixture: dict[str, Any]) -> tuple[str | None, int | None, str]:
    winner = safe_str(fixture.get("event_winner"))
    first_key = safe_int(fixture.get("first_player_key"))
    second_key = safe_int(fixture.get("second_player_key"))

    w_lower = winner.lower()
    if w_lower in {"first player", "first", "home", "player 1", "1"}:
        return "home", first_key, winner
    if w_lower in {"second player", "second", "away", "player 2", "2"}:
        return "away", second_key, winner

    winner_key = safe_int(fixture.get("event_winner_key") or fixture.get("winner_key") or fixture.get("winner_player_key"))
    if winner_key is not None:
        if first_key is not None and winner_key == first_key:
            return "home", winner_key, winner
        if second_key is not None and winner_key == second_key:
            return "away", winner_key, winner

    winner_name = safe_str(fixture.get("event_winner_name") or fixture.get("winner_name") or fixture.get("winner"))
    if winner_name:
        first_name = safe_str(fixture.get("event_first_player")).lower()
        second_name = safe_str(fixture.get("event_second_player")).lower()
        wn = winner_name.lower()
        if first_name and wn == first_name:
            return "home", first_key, winner_name
        if second_name and wn == second_name:
            return "away", second_key, winner_name

    return None, None, winner or winner_name


def compute_profit(row: dict[str, Any], result: str) -> tuple[float | None, float | None]:
    stake = safe_float(row.get("stake"))
    odds = safe_float(row.get("odds") or row.get("avg_odds"))
    if stake is None:
        stake = 1.0

    profit = None
    if result == "win" and odds is not None:
        profit = stake * (odds - 1.0)
    elif result == "loss":
        profit = -stake
    elif result in {"void", "push"}:
        profit = 0.0

    roi = profit / stake if profit is not None and stake else None
    return profit, roi


def settle_row(row: dict[str, Any], fixture: dict[str, Any] | None) -> tuple[dict[str, Any], bool, str]:
    if fixture is None:
        return row, False, "fixture_not_found"

    status = normalize_status(fixture)
    if status == "pending" or status == "unknown":
        return row, False, status

    updated = dict(row)
    updated["event_status"] = safe_str(fixture.get("event_status") or fixture.get("status"))
    updated["final_score"] = fixture_score(fixture)

    if status == "void":
        profit, roi = compute_profit(updated, "void")
        updated.update({
            "status": "void",
            "result": "void",
            "profit": profit,
            "roi": roi,
            "settled_at": now_utc_iso(),
            "event_winner": safe_str(fixture.get("event_winner")),
            "event_winner_key": fixture.get("event_winner_key") or fixture.get("winner_key"),
        })
        return updated, True, "settled_void"

    winner_side, winner_key, winner_label = winner_side_from_fixture(fixture)
    if status == "finished" and not winner_side:
        return row, False, "finished_without_winner"

    picked_side = safe_str(row.get("side")).lower()
    if picked_side in {"home", "first", "first player", "1"}:
        picked_side = "home"
    elif picked_side in {"away", "second", "second player", "2"}:
        picked_side = "away"

    result = "win" if picked_side and picked_side == winner_side else "loss"
    profit, roi = compute_profit(updated, result)
    updated.update({
        "status": result,
        "result": result,
        "profit": None if profit is None else round(profit, 6),
        "roi": None if roi is None else round(roi, 6),
        "settled_at": now_utc_iso(),
        "event_winner": winner_label,
        "event_winner_key": winner_key,
    })
    return updated, True, f"settled_{result}"


def sort_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        safe_str(row.get("date")),
        safe_str(row.get("time")),
        safe_str(row.get("tournament")),
        safe_str(row.get("match")),
    )


def add_running_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            running_stake += stake
            running_profit += profit

            r["running_wins"] = running_wins
            r["running_losses"] = running_losses
            r["running_w_l"] = f"{running_wins}-{running_losses}"
            r["running_total_stake"] = round(running_stake, 6)
            r["running_total_profit"] = round(running_profit, 6)
            r["running_roi"] = round(running_profit / running_stake, 6) if running_stake else None

        out.append(r)

    return out


def fmt_pct(x: Any) -> str:
    v = safe_float(x)
    return "-" if v is None else f"{v * 100:.2f}%"


def fmt_num(x: Any) -> str:
    v = safe_float(x)
    return "-" if v is None else f"{v:.2f}"


def fmt_cell(x: Any) -> str:
    s = safe_str(x)
    return "-" if not s else s.replace("|", "\\|")


def write_results_markdown(path: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    settled = [r for r in rows if safe_str(r.get("status")).lower() in {"win", "loss", "void", "push"}]
    display_rows = sorted(settled, key=sort_key, reverse=True)[:500]

    lines = [
        "# Standalone Elo Results",
        "",
        f"- Settled: {summary.get('settled')}",
        f"- W-L: {summary.get('w_l')}",
        f"- Hit rate: {fmt_pct(summary.get('hit_rate'))}",
        f"- Total stake: {fmt_num(summary.get('total_stake'))}",
        f"- Total profit: {fmt_num(summary.get('total_profit'))}",
        f"- ROI: {fmt_pct(summary.get('roi'))}",
        "",
        "## Settled picks",
        "",
        "| Date | Time | Result | W-L | ROI | Pick | Opponent | Odds | Stake | Profit | Total Profit | Level | Surface | TLE Prob | TLE Edge | Conf | Score |",
        "|---|---:|---|---:|---:|---|---|---:|---:|---:|---:|---|---|---:|---:|---|---|",
    ]

    for r in display_rows:
        lines.append(
            "| "
            + " | ".join([
                fmt_cell(r.get("date")),
                fmt_cell(r.get("time")),
                fmt_cell(r.get("status") or r.get("result")),
                fmt_cell(r.get("running_w_l")),
                fmt_pct(r.get("running_roi")),
                fmt_cell(r.get("pick")),
                fmt_cell(r.get("opponent")),
                fmt_num(r.get("odds") or r.get("avg_odds")),
                fmt_num(r.get("stake")),
                fmt_num(r.get("profit")),
                fmt_num(r.get("running_total_profit")),
                fmt_cell(r.get("level")),
                fmt_cell(r.get("surface")),
                fmt_pct(r.get("tle_prob")),
                fmt_pct(r.get("tle_edge")),
                fmt_cell(r.get("confidence")),
                fmt_cell(r.get("final_score")),
            ])
            + " |"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="Optional YYYY-MM-DD. If set, only fetch this date.")
    parser.add_argument("--lookback-days", type=int, default=7, help="Also fetch recent dates for delayed settlement.")
    args = parser.parse_args()

    predictions_payload = read_json(PREDICTIONS_JSON, {"picks": []})
    results_payload = read_json(RESULTS_JSON, {"picks": []})

    active = payload_items(predictions_payload)
    historical = payload_items(results_payload)

    active_dates = {safe_str(r.get("date")) for r in active if safe_str(r.get("date"))}
    if args.date:
        active_dates = {args.date}

    # Small safety net: if some old active picks have wrong/missing date, fetch recent dates too.
    today = datetime.now(timezone.utc).date()
    for i in range(max(0, args.lookback_days) + 1):
        active_dates.add((today - timedelta(days=i)).isoformat())

    fixture_index, fetch_counts = build_fixture_index(sorted(active_dates))

    historical_by_id = {safe_str(r.get("pick_id")): r for r in historical if safe_str(r.get("pick_id"))}
    still_active = []
    settled_now = []
    counters = Counter(fetch_counts)

    for row in active:
        pid = safe_str(row.get("pick_id"))
        event_key = safe_str(row.get("event_key") or row.get("fixture_id"))
        fixture = fixture_index.get(event_key) if event_key else None

        updated, did_settle, reason = settle_row(row, fixture)
        counters[reason] += 1

        if did_settle:
            if pid:
                historical_by_id[pid] = updated
            settled_now.append(updated)
        else:
            still_active.append(row)
            if pid and pid not in historical_by_id:
                historical_by_id[pid] = row

    all_results = add_running_totals(list(historical_by_id.values()))
    still_active = sorted(still_active, key=sort_key)

    settled_all = [r for r in all_results if safe_str(r.get("status")).lower() in {"win", "loss", "void", "push"}]
    wins = sum(1 for r in settled_all if safe_str(r.get("status")).lower() == "win")
    losses = sum(1 for r in settled_all if safe_str(r.get("status")).lower() == "loss")
    stake = sum(safe_float(r.get("stake")) or 0.0 for r in settled_all)
    profit = sum(safe_float(r.get("profit")) or 0.0 for r in settled_all)
    settled_count = wins + losses

    predictions_out = {
        "generated_at": now_utc_iso(),
        "model": "TLE Standalone Elo Scanner v4.0",
        "summary": {
            "active_picks": len(still_active),
            "settled_removed_this_run": len(settled_now),
        },
        "picks": still_active,
    }
    results_out = {
        "generated_at": now_utc_iso(),
        "model": "TLE Standalone Elo Scanner v4.0",
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

    active_fields = existing_csv_fields(ACTIVE_CSV, DEFAULT_ACTIVE_FIELDS)
    active_fields = merge_fields(active_fields, still_active)
    results_fields = merge_fields(active_fields, all_results, RESULT_EXTRA_FIELDS)

    write_json(PREDICTIONS_JSON, predictions_out)
    write_json(RESULTS_JSON, results_out)
    write_csv(ACTIVE_CSV, still_active, active_fields)
    write_csv(RESULTS_CSV, all_results, results_fields)
    write_results_markdown(RESULTS_MD, all_results, results_out["summary"])

    report = {
        "status": "ok",
        "generated_at": now_utc_iso(),
        "source": "API-Tennis get_fixtures",
        "active_before": len(active),
        "settled_this_run": len(settled_now),
        "active_after": len(still_active),
        "settle_counts": dict(sorted(counters.items())),
        "roi_summary": results_out["summary"],
        "outputs": {
            "predictions_json": str(PREDICTIONS_JSON),
            "results_json": str(RESULTS_JSON),
            "active_csv": str(ACTIVE_CSV),
            "results_csv": str(RESULTS_CSV),
            "results_md": str(RESULTS_MD),
            "report_json": str(REPORT_JSON),
        },
        "notes": [
            "Standalone settle uses API-Tennis get_fixtures and matches by event_key.",
            "Settled picks are removed from predictions.json.",
            "results.json is the ledger and keeps pending and settled tracked picks.",
        ],
    }
    write_json(REPORT_JSON, report)

    print(
        f"status=ok source=api active_before={len(active)} settled_this_run={len(settled_now)} "
        f"active_after={len(still_active)} wl={wins}-{losses} "
        f"profit={round(profit, 6)} roi={round(profit / stake, 6) if stake else None}"
    )


if __name__ == "__main__":
    main()

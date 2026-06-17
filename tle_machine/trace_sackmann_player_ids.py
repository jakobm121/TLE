from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


RAW_SACKMANN_DIR = Path("data/raw/sackmann")
SOURCE_SACKMANN_DIR = Path("data/source/sackmann")
REPORT_DIR = Path("data/reports/sackmann")
TRACE_CSV = REPORT_DIR / "trace_sackmann_player_ids.csv"
TRACE_JSON = REPORT_DIR / "trace_sackmann_player_ids.json"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(v: Any) -> str:
    return str(v or "").strip()


def strip_accents(s: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", s or "")
        if not unicodedata.combining(ch)
    )


def norm_name(s: str) -> str:
    s = strip_accents(clean(s)).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm_id(s: Any) -> str:
    s = clean(s)
    m = re.search(r"(\d+)$", s)
    return m.group(1) if m else s


def player_key_to_id(s: str) -> str:
    return norm_id(s)


def detect_gender_from_path(path: Path) -> str:
    low = str(path).lower()
    if "women" in low or "wta" in low:
        return "women"
    if "men" in low or "atp" in low:
        return "men"
    return ""


def iter_csv_rows(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            yield from csv.DictReader(f)
    except UnicodeDecodeError:
        with path.open("r", encoding="latin-1", newline="") as f:
            yield from csv.DictReader(f)


def iter_jsonl_gz(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def first_existing(row: dict[str, Any], keys: list[str]) -> str:
    for k in keys:
        v = clean(row.get(k))
        if v:
            return v
    return ""


def raw_match_side(row: dict[str, Any], side: str) -> dict[str, str]:
    # Sackmann raw files usually use winner_id/winner_name and loser_id/loser_name.
    # Keep extra variants because the pipeline may contain normalized CSVs too.
    if side == "winner":
        pid = first_existing(row, ["winner_id", "winner_player_id", "winner_sackmann_id", "winner_key", "winner_player_key"])
        name = first_existing(row, ["winner_name", "winner", "winner_full_name"])
        opp_id = first_existing(row, ["loser_id", "loser_player_id", "loser_sackmann_id", "loser_key", "loser_player_key"])
        opp_name = first_existing(row, ["loser_name", "loser", "loser_full_name"])
        result = "win"
    else:
        pid = first_existing(row, ["loser_id", "loser_player_id", "loser_sackmann_id", "loser_key", "loser_player_key"])
        name = first_existing(row, ["loser_name", "loser", "loser_full_name"])
        opp_id = first_existing(row, ["winner_id", "winner_player_id", "winner_sackmann_id", "winner_key", "winner_player_key"])
        opp_name = first_existing(row, ["winner_name", "winner", "winner_full_name"])
        result = "loss"

    return {
        "player_id": norm_id(pid),
        "player_name": name,
        "opponent_id": norm_id(opp_id),
        "opponent_name": opp_name,
        "result": result,
    }


def source_match_side(row: dict[str, Any], side: str) -> dict[str, str]:
    if side == "winner":
        key = first_existing(row, ["winner_player_key", "winner_key"])
        name = first_existing(row, ["winner_name", "winner_player_name"])
        opp_key = first_existing(row, ["loser_player_key", "loser_key"])
        opp_name = first_existing(row, ["loser_name", "loser_player_name"])
        result = "win"
    else:
        key = first_existing(row, ["loser_player_key", "loser_key"])
        name = first_existing(row, ["loser_name", "loser_player_name"])
        opp_key = first_existing(row, ["winner_player_key", "winner_key"])
        opp_name = first_existing(row, ["winner_name", "winner_player_name"])
        result = "loss"

    return {
        "player_id": player_key_to_id(key),
        "player_name": name,
        "opponent_id": player_key_to_id(opp_key),
        "opponent_name": opp_name,
        "result": result,
        "player_key": key,
        "opponent_key": opp_key,
    }


def row_date(row: dict[str, Any]) -> str:
    return first_existing(row, ["tourney_date", "date", "match_date", "start_date"])


def row_tournament(row: dict[str, Any]) -> str:
    return first_existing(row, ["tourney_name", "tournament", "event_name", "league_name"])


def row_level(row: dict[str, Any]) -> str:
    return first_existing(row, ["level", "tourney_level", "event_type_type", "event_type"])


def row_surface(row: dict[str, Any]) -> str:
    return first_existing(row, ["surface"])


def row_round(row: dict[str, Any]) -> str:
    return first_existing(row, ["round", "round_name"])


def row_score(row: dict[str, Any]) -> str:
    return first_existing(row, ["score", "result_score"])


def matches_query(side: dict[str, str], query_name_norm: str, query_ids: set[str]) -> bool:
    pid = norm_id(side.get("player_id"))
    pname_norm = norm_name(side.get("player_name"))
    if query_ids and pid in query_ids:
        return True
    if query_name_norm and pname_norm == query_name_norm:
        return True
    return False


def scan_raw_csvs(query_name_norm: str, query_ids: set[str], limit: int) -> tuple[list[dict[str, Any]], Counter]:
    rows: list[dict[str, Any]] = []
    stats: Counter = Counter()

    files = sorted(RAW_SACKMANN_DIR.rglob("*.csv"))
    stats["raw_csv_files_seen"] = len(files)

    for path in files:
        gender = detect_gender_from_path(path)
        for row in iter_csv_rows(path):
            stats["raw_rows_seen"] += 1
            for side_name in ["winner", "loser"]:
                side = raw_match_side(row, side_name)
                if not matches_query(side, query_name_norm, query_ids):
                    continue

                stats["raw_hits"] += 1
                rows.append({
                    "source": "raw_csv",
                    "file": str(path),
                    "gender": gender,
                    "player_id": side["player_id"],
                    "player_key": f"{gender}:sackmann:{side['player_id']}" if gender and side["player_id"] else "",
                    "player_name": side["player_name"],
                    "result": side["result"],
                    "opponent_id": side["opponent_id"],
                    "opponent_key": f"{gender}:sackmann:{side['opponent_id']}" if gender and side["opponent_id"] else "",
                    "opponent_name": side["opponent_name"],
                    "date": row_date(row),
                    "tournament": row_tournament(row),
                    "level": row_level(row),
                    "surface": row_surface(row),
                    "round": row_round(row),
                    "score": row_score(row),
                })

                if limit and len(rows) >= limit:
                    return rows, stats

    return rows, stats


def scan_source_jsonl(query_name_norm: str, query_ids: set[str], limit: int) -> tuple[list[dict[str, Any]], Counter]:
    rows: list[dict[str, Any]] = []
    stats: Counter = Counter()

    files = sorted(SOURCE_SACKMANN_DIR.rglob("*.jsonl.gz"))
    stats["source_jsonl_files_seen"] = len(files)

    for path in files:
        for row in iter_jsonl_gz(path):
            stats["source_rows_seen"] += 1
            gender = clean(row.get("gender")) or detect_gender_from_path(path)

            for side_name in ["winner", "loser"]:
                side = source_match_side(row, side_name)
                if not matches_query(side, query_name_norm, query_ids):
                    continue

                stats["source_hits"] += 1
                rows.append({
                    "source": "source_jsonl",
                    "file": str(path),
                    "gender": gender,
                    "player_id": side["player_id"],
                    "player_key": side.get("player_key") or (f"{gender}:sackmann:{side['player_id']}" if gender and side["player_id"] else ""),
                    "player_name": side["player_name"],
                    "result": side["result"],
                    "opponent_id": side["opponent_id"],
                    "opponent_key": side.get("opponent_key") or (f"{gender}:sackmann:{side['opponent_id']}" if gender and side["opponent_id"] else ""),
                    "opponent_name": side["opponent_name"],
                    "date": row_date(row),
                    "tournament": row_tournament(row),
                    "level": row_level(row),
                    "surface": row_surface(row),
                    "round": row_round(row),
                    "score": row_score(row),
                })

                if limit and len(rows) >= limit:
                    return rows, stats

    return rows, stats


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="", help="Exact player name to trace, e.g. 'Bruno Kuzuhara'")
    parser.add_argument("--ids", default="", help="Comma-separated Sackmann ids or keys, e.g. '210029,210044'")
    parser.add_argument("--limit", type=int, default=0, help="Optional max rows across each source scan; 0 means no limit.")
    args = parser.parse_args()

    query_name_norm = norm_name(args.name)
    query_ids = {norm_id(x) for x in re.split(r"[,\s]+", args.ids or "") if clean(x)}

    if not query_name_norm and not query_ids:
        raise SystemExit("Provide --name and/or --ids")

    raw_rows, raw_stats = scan_raw_csvs(query_name_norm, query_ids, args.limit)
    source_rows, source_stats = scan_source_jsonl(query_name_norm, query_ids, args.limit)

    all_rows = raw_rows + source_rows

    fieldnames = [
        "source",
        "file",
        "gender",
        "player_id",
        "player_key",
        "player_name",
        "result",
        "opponent_id",
        "opponent_key",
        "opponent_name",
        "date",
        "tournament",
        "level",
        "surface",
        "round",
        "score",
    ]

    write_csv(TRACE_CSV, all_rows, fieldnames)

    by_player_key = Counter(r.get("player_key", "") for r in all_rows if r.get("player_key"))
    by_player_id = Counter(r.get("player_id", "") for r in all_rows if r.get("player_id"))
    by_source = Counter(r.get("source", "") for r in all_rows if r.get("source"))

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "query": {
            "name": args.name,
            "name_norm": query_name_norm,
            "ids": sorted(query_ids),
            "limit": args.limit,
        },
        "outputs": {
            "trace_csv": str(TRACE_CSV),
            "trace_json": str(TRACE_JSON),
        },
        "counts": {
            "rows_total": len(all_rows),
            "raw_rows": len(raw_rows),
            "source_rows": len(source_rows),
            **dict(raw_stats),
            **dict(source_stats),
        },
        "by_player_key": dict(by_player_key),
        "by_player_id": dict(by_player_id),
        "by_source": dict(by_source),
        "interpretation": [
            "If the same real player appears under multiple player_id values already in raw_csv rows, the duplicate is present in raw Sackmann input.",
            "If only source_jsonl shows the duplicate but raw_csv does not, the issue likely happened during import/normalization.",
            "Use the trace CSV to inspect dates, opponents, tournaments, and files for each id.",
        ],
    }
    write_json(TRACE_JSON, report)

    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

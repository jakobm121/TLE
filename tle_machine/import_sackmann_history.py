from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import RAW_ATP_DIR, RAW_WTA_DIR, SACKMANN_FILES, SOURCE_SACKMANN_DIR, START_YEAR, SURFACES
from .utils import ensure_dirs, now_utc_iso, player_key, write_json, write_jsonl_gz


def parse_date(value: str) -> str | None:
    value = (value or "").strip()
    if len(value) != 8 or not value.isdigit():
        return None
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def normalize_surface(surface: str | None) -> str:
    s = (surface or "").strip().lower()
    if s in SURFACES:
        return s
    return "unknown"


def infer_level(row: dict[str, str], source_name: str, level_hint: str | None) -> str:
    tourney_level = (row.get("tourney_level") or "").strip().upper()
    name = (row.get("tourney_name") or "").lower()
    if tourney_level == "G":
        return "grand_slam"
    if tourney_level in {"M", "A", "F", "D"}:
        return "atp_wta"
    if tourney_level == "C":
        return "challenger"
    if tourney_level == "Q":
        return "qualifying"
    if "qual" in name:
        return "qualifying"
    if level_hint:
        return level_hint
    if "chall" in source_name:
        return "challenger"
    return "itf"


def iter_raw_rows(start_year: int, end_year: int):
    for year in range(start_year, end_year + 1):
        for source_name, cfg in SACKMANN_FILES.items():
            filename = cfg["template"].format(year=year)
            path = cfg["raw_dir"] / filename
            if not path.exists() or path.stat().st_size == 0:
                continue
            with path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    yield year, source_name, cfg, row


def convert_row(year: int, source_name: str, cfg: dict, row: dict[str, str]) -> tuple[dict | None, str | None]:
    date = parse_date(row.get("tourney_date", ""))
    winner_name = row.get("winner_name") or ""
    loser_name = row.get("loser_name") or ""
    if not date or not winner_name or not loser_name:
        return None, "missing_required"
    gender = cfg["gender"]
    level = infer_level(row, source_name, cfg.get("level_hint"))
    surface = normalize_surface(row.get("surface"))
    if surface == "unknown" and level not in {"itf", "qualifying"}:
        return None, "unknown_surface_not_allowed"

    winner_id = row.get("winner_id") or ""
    loser_id = row.get("loser_id") or ""
    match = {
        "match_id": f"sackmann:{gender}:{row.get('tourney_id','')}:{row.get('match_num','')}:{winner_id}:{loser_id}",
        "source": "sackmann",
        "source_file": cfg["template"].format(year=year),
        "date": date,
        "year": int(date[:4]),
        "gender": gender,
        "level": level,
        "surface": surface,
        "tourney_id": row.get("tourney_id") or "",
        "tourney_name": row.get("tourney_name") or "",
        "round": row.get("round") or "",
        "best_of": row.get("best_of") or "",
        "winner": {
            "name": winner_name,
            "sackmann_player_id": winner_id,
            "player_key": player_key(gender, winner_id, winner_name),
        },
        "loser": {
            "name": loser_name,
            "sackmann_player_id": loser_id,
            "player_key": player_key(gender, loser_id, loser_name),
        },
    }
    return match, None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--end-year", type=int, default=datetime.now(timezone.utc).year)
    args = parser.parse_args(argv)

    ensure_dirs(SOURCE_SACKMANN_DIR)
    by_year: dict[int, list[dict]] = defaultdict(list)
    counters = Counter()
    for year, source_name, cfg, row in iter_raw_rows(args.start_year, args.end_year):
        counters["input_rows"] += 1
        match, reason = convert_row(year, source_name, cfg, row)
        if reason:
            counters[f"skipped_{reason}"] += 1
            continue
        counters["imported"] += 1
        counters[f"level_{match['level']}"] += 1
        counters[f"surface_{match['surface']}"] += 1
        counters[f"gender_{match['gender']}"] += 1
        by_year[match["year"]].append(match)

    year_files = []
    for year, rows in sorted(by_year.items()):
        rows.sort(key=lambda r: (r["date"], r["match_id"]))
        path = SOURCE_SACKMANN_DIR / f"tle_sackmann_matches_{year}.jsonl.gz"
        count = write_jsonl_gz(path, rows)
        year_files.append({"year": year, "path": str(path), "matches": count, "created_at": now_utc_iso()})

    manifest = {
        "generated_at": now_utc_iso(),
        "source": "sackmann",
        "start_year": args.start_year,
        "end_year": args.end_year,
        "matches": counters["imported"],
        "year_files": year_files,
        "counters": dict(counters),
    }
    write_json(SOURCE_SACKMANN_DIR / "manifest.json", manifest)
    write_json(Path("data/reports/sackmann/import_report.json"), manifest)
    print(manifest)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import RAW_ATP_DIR, RAW_WTA_DIR, SACKMANN_FILES, SOURCE_SACKMANN_DIR, START_YEAR, SURFACES
from .utils import ensure_dirs, now_utc_iso, player_key, write_json, write_jsonl_gz

REPORT_DIR = Path("data/reports/sackmann")
SKIPPED_CSV = REPORT_DIR / "skipped_sackmann_matches.csv"
SKIPPED_SUMMARY_JSON = REPORT_DIR / "skipped_sackmann_matches_summary.json"
SOURCE_FILE_LEVEL_AUDIT_JSON = REPORT_DIR / "source_file_level_audit.json"
IMPORT_REPORT_JSON = REPORT_DIR / "import_report.json"


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
    round_name = (row.get("round") or "").strip().upper()

    if tourney_level == "G":
        return "grand_slam"
    if tourney_level == "D":
        return "team_cup"
    if tourney_level in {"M", "A", "F"}:
        return "atp_wta"
    if tourney_level == "C":
        return "challenger"
    if tourney_level == "Q":
        return "qualifying"
    if round_name == "Q" or "qual" in name:
        return "qualifying"
    if level_hint:
        return level_hint
    if "chall" in source_name:
        return "challenger"
    return "itf"




def is_team_competition(row: dict[str, str], level: str) -> bool:
    """Return True for Davis Cup / BJK Cup / Fed Cup style team competitions.

    Sackmann marks these with tourney_level == "D" and often leaves surface blank.
    We keep them out of TLE ratings because they are not standard tour/level events.
    """
    tourney_level = (row.get("tourney_level") or "").strip().upper()
    tourney_id = (row.get("tourney_id") or "").upper()
    name = (row.get("tourney_name") or "").lower()
    return (
        level == "team_cup"
        or tourney_level == "D"
        or "-DC-" in tourney_id
        or "-FC-" in tourney_id
        or "davis cup" in name
        or "bjk cup" in name
        or "fed cup" in name
    )

def iter_raw_rows(start_year: int, end_year: int):
    for year in range(start_year, end_year + 1):
        for source_name, cfg in SACKMANN_FILES.items():
            filename = cfg["template"].format(year=year)
            path = cfg["raw_dir"] / filename
            if not path.exists() or path.stat().st_size == 0:
                continue
            with path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row_number, row in enumerate(reader, start=2):
                    yield year, source_name, filename, row_number, cfg, row


def build_match(year: int, filename: str, gender: str, level: str, surface: str, date: str, row: dict[str, str]) -> dict[str, Any]:
    winner_name = row.get("winner_name") or ""
    loser_name = row.get("loser_name") or ""
    winner_id = row.get("winner_id") or ""
    loser_id = row.get("loser_id") or ""
    return {
        "match_id": f"sackmann:{gender}:{row.get('tourney_id','')}:{row.get('match_num','')}:{winner_id}:{loser_id}",
        "source": "sackmann",
        "source_file": filename,
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


def skip_row(
    *,
    skipped_rows: list[dict[str, Any]],
    counters: Counter,
    source_file_audit: dict[str, Any],
    filename: str,
    row_number: int,
    year: int,
    source_name: str,
    cfg: dict[str, Any],
    row: dict[str, str],
    reason: str,
    date: str | None,
    level: str | None,
    surface: str | None,
) -> None:
    counters[f"skipped_{reason}"] += 1
    source_file_audit[filename]["skipped_by_reason"][reason] += 1
    skipped_rows.append(
        {
            "source_file": filename,
            "source_name": source_name,
            "row_number": row_number,
            "year": year,
            "gender": cfg.get("gender", ""),
            "date": date or parse_date(row.get("tourney_date", "")) or "",
            "raw_tourney_date": row.get("tourney_date", ""),
            "tourney_id": row.get("tourney_id", ""),
            "tourney_name": row.get("tourney_name", ""),
            "raw_tourney_level": row.get("tourney_level", ""),
            "raw_round": row.get("round", ""),
            "raw_surface": row.get("surface", ""),
            "normalized_level": level or "",
            "normalized_surface": surface or "",
            "winner_id": row.get("winner_id", ""),
            "winner_name": row.get("winner_name", ""),
            "loser_id": row.get("loser_id", ""),
            "loser_name": row.get("loser_name", ""),
            "skip_reason": reason,
        }
    )


def write_skipped_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "source_file",
        "source_name",
        "row_number",
        "year",
        "gender",
        "date",
        "raw_tourney_date",
        "tourney_id",
        "tourney_name",
        "raw_tourney_level",
        "raw_round",
        "raw_surface",
        "normalized_level",
        "normalized_surface",
        "winner_id",
        "winner_name",
        "loser_id",
        "loser_name",
        "skip_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def counter_to_dict(obj: Any) -> Any:
    if isinstance(obj, Counter):
        return dict(obj)
    if isinstance(obj, defaultdict):
        return {k: counter_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, dict):
        return {k: counter_to_dict(v) for k, v in obj.items()}
    return obj


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--end-year", type=int, default=datetime.now(timezone.utc).year)
    args = parser.parse_args(argv)

    ensure_dirs(SOURCE_SACKMANN_DIR, REPORT_DIR)

    by_year: dict[int, list[dict[str, Any]]] = defaultdict(list)
    counters = Counter()
    skipped_rows: list[dict[str, Any]] = []

    source_file_audit: dict[str, Any] = defaultdict(
        lambda: {
            "input_rows": 0,
            "imported": 0,
            "gender_counts": Counter(),
            "raw_tourney_level_counts": Counter(),
            "raw_round_counts": Counter(),
            "raw_surface_counts": Counter(),
            "normalized_level_counts": Counter(),
            "normalized_surface_counts": Counter(),
            "skipped_by_reason": Counter(),
        }
    )

    for year, source_name, filename, row_number, cfg, row in iter_raw_rows(args.start_year, args.end_year):
        counters["input_rows"] += 1
        audit = source_file_audit[filename]
        audit["input_rows"] += 1
        audit["gender_counts"][cfg["gender"]] += 1
        audit["raw_tourney_level_counts"][(row.get("tourney_level") or "").strip() or "<blank>"] += 1
        audit["raw_round_counts"][(row.get("round") or "").strip() or "<blank>"] += 1
        audit["raw_surface_counts"][(row.get("surface") or "").strip() or "<blank>"] += 1

        date = parse_date(row.get("tourney_date", ""))
        gender = cfg["gender"]
        level = infer_level(row, source_name, cfg.get("level_hint"))
        surface = normalize_surface(row.get("surface"))
        audit["normalized_level_counts"][level] += 1
        audit["normalized_surface_counts"][surface] += 1

        winner_name = row.get("winner_name") or ""
        loser_name = row.get("loser_name") or ""
        if not date:
            skip_row(
                skipped_rows=skipped_rows,
                counters=counters,
                source_file_audit=source_file_audit,
                filename=filename,
                row_number=row_number,
                year=year,
                source_name=source_name,
                cfg=cfg,
                row=row,
                reason="missing_or_invalid_date",
                date=date,
                level=level,
                surface=surface,
            )
            continue
        if not winner_name or not loser_name:
            skip_row(
                skipped_rows=skipped_rows,
                counters=counters,
                source_file_audit=source_file_audit,
                filename=filename,
                row_number=row_number,
                year=year,
                source_name=source_name,
                cfg=cfg,
                row=row,
                reason="missing_player_name",
                date=date,
                level=level,
                surface=surface,
            )
            continue
        if is_team_competition(row, level):
            skip_row(
                skipped_rows=skipped_rows,
                counters=counters,
                source_file_audit=source_file_audit,
                filename=filename,
                row_number=row_number,
                year=year,
                source_name=source_name,
                cfg=cfg,
                row=row,
                reason="team_competition_excluded",
                date=date,
                level=level,
                surface=surface,
            )
            continue

        if surface == "unknown" and level not in {"itf", "qualifying"}:
            skip_row(
                skipped_rows=skipped_rows,
                counters=counters,
                source_file_audit=source_file_audit,
                filename=filename,
                row_number=row_number,
                year=year,
                source_name=source_name,
                cfg=cfg,
                row=row,
                reason="unknown_surface_not_allowed",
                date=date,
                level=level,
                surface=surface,
            )
            continue

        match = build_match(year, filename, gender, level, surface, date, row)
        counters["imported"] += 1
        counters[f"level_{level}"] += 1
        counters[f"surface_{surface}"] += 1
        counters[f"gender_{gender}"] += 1
        audit["imported"] += 1
        by_year[match["year"]].append(match)

    year_files = []
    for year, rows in sorted(by_year.items()):
        rows.sort(key=lambda r: (r["date"], r["match_id"]))
        path = SOURCE_SACKMANN_DIR / f"tle_sackmann_matches_{year}.jsonl.gz"
        count = write_jsonl_gz(path, rows)
        year_files.append({"year": year, "path": str(path), "matches": count, "created_at": now_utc_iso()})

    skipped_summary = {
        "generated_at": now_utc_iso(),
        "skipped_total": len(skipped_rows),
        "skipped_by_reason": {k.replace("skipped_", ""): v for k, v in counters.items() if k.startswith("skipped_")},
        "skipped_by_file": {},
        "skipped_by_level_surface": Counter(),
        "sample_limit_note": "Full skipped rows are in skipped_sackmann_matches.csv",
    }
    for row in skipped_rows:
        skipped_summary["skipped_by_file"].setdefault(row["source_file"], Counter())
        skipped_summary["skipped_by_file"][row["source_file"]][row["skip_reason"]] += 1
        skipped_summary["skipped_by_level_surface"][f"{row['normalized_level']}|{row['normalized_surface']}"] += 1
    skipped_summary = counter_to_dict(skipped_summary)

    source_file_audit_dict = counter_to_dict(source_file_audit)
    manifest = {
        "generated_at": now_utc_iso(),
        "source": "sackmann",
        "start_year": args.start_year,
        "end_year": args.end_year,
        "matches": counters["imported"],
        "year_files": year_files,
        "counters": dict(counters),
        "audit_outputs": {
            "import_report": str(IMPORT_REPORT_JSON),
            "skipped_matches_csv": str(SKIPPED_CSV),
            "skipped_summary": str(SKIPPED_SUMMARY_JSON),
            "source_file_level_audit": str(SOURCE_FILE_LEVEL_AUDIT_JSON),
        },
    }

    write_json(SOURCE_SACKMANN_DIR / "manifest.json", manifest)
    write_json(IMPORT_REPORT_JSON, manifest)
    write_json(SKIPPED_SUMMARY_JSON, skipped_summary)
    write_json(SOURCE_FILE_LEVEL_AUDIT_JSON, source_file_audit_dict)
    write_skipped_csv(SKIPPED_CSV, skipped_rows)

    print(manifest)


if __name__ == "__main__":
    main()

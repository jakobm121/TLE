from __future__ import annotations

import csv
import gzip
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SOURCE_API_DIR = Path("data/source/api_tennis")
SOURCE_API_MANIFEST = SOURCE_API_DIR / "manifest.json"
CANONICAL_MANIFEST = Path("data/canonical/manifest.json")
REPORT_DIR = Path("data/reports/api_tennis")
IMPORT_REPORT = REPORT_DIR / "import_api_results_report.json"
SKIPPED_CSV = REPORT_DIR / "skipped_api_matches.csv"
DUPLICATES_CSV = REPORT_DIR / "duplicate_api_matches.csv"

AUDIT_JSON = REPORT_DIR / "api_source_audit.json"
ISSUES_CSV = REPORT_DIR / "api_source_issues.csv"
OVERLAP_CSV = REPORT_DIR / "api_sackmann_overlap_candidates.csv"

VALID_GENDERS = {"men", "women"}
VALID_LEVELS = {"atp_wta", "grand_slam", "challenger", "itf", "qualifying"}
VALID_SURFACES = {"hard", "clay", "grass", "carpet", "unknown"}

ISSUE_FIELDS = [
    "severity",
    "issue",
    "source_file",
    "match_id",
    "api_event_key",
    "date",
    "gender",
    "level",
    "surface",
    "tourney_name",
    "winner_name",
    "loser_name",
    "detail",
]

OVERLAP_FIELDS = [
    "confidence",
    "api_match_id",
    "api_event_key",
    "date",
    "gender",
    "api_level",
    "api_surface",
    "api_tourney_name",
    "api_winner",
    "api_loser",
    "sackmann_match_id",
    "sackmann_level",
    "sackmann_surface",
    "sackmann_tourney_name",
    "sackmann_winner",
    "sackmann_loser",
    "match_key",
]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def iter_jsonl_gz(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def ascii_lower(value: Any) -> str:
    s = unicodedata.normalize("NFKD", text(value)).encode("ascii", "ignore").decode("ascii")
    return s.lower()


def slug(value: Any) -> str:
    s = ascii_lower(value)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def family_name(value: Any) -> str:
    s = ascii_lower(value)
    s = re.sub(r"[.,]", " ", s)
    parts = [p for p in re.split(r"\s+", s) if p and len(p) > 1]
    if not parts:
        return ""
    # API names often look like "F. Auger-Aliassime"; Sackmann names are full.
    # The last token is the safest low-cost cross-source comparison key.
    return parts[-1]


def read_source_files(manifest_path: Path) -> list[Path]:
    if manifest_path.exists():
        manifest = load_json(manifest_path)
        files = []
        for item in manifest.get("year_files", []):
            p = Path(item.get("path", ""))
            if p.exists():
                files.append(p)
        if files:
            return sorted(files)
    return sorted(SOURCE_API_DIR.glob("tle_api_matches_*.jsonl.gz"))


def issue(
    rows: list[dict[str, Any]],
    issue_counts: Counter,
    row: dict[str, Any] | None,
    issue_name: str,
    detail: str,
    severity: str = "error",
) -> None:
    issue_counts[f"{severity}:{issue_name}"] += 1
    row = row or {}
    winner = row.get("winner") or {}
    loser = row.get("loser") or {}
    rows.append(
        {
            "severity": severity,
            "issue": issue_name,
            "source_file": row.get("source_file", ""),
            "match_id": row.get("match_id", ""),
            "api_event_key": row.get("api_event_key", ""),
            "date": row.get("date", ""),
            "gender": row.get("gender", ""),
            "level": row.get("level", ""),
            "surface": row.get("surface", ""),
            "tourney_name": row.get("tourney_name", ""),
            "winner_name": winner.get("name", ""),
            "loser_name": loser.get("name", ""),
            "detail": detail,
        }
    )


def candidate_key(row: dict[str, Any]) -> str:
    winner = row.get("winner") or {}
    loser = row.get("loser") or {}
    return "|".join(
        [
            text(row.get("gender")),
            text(row.get("date")),
            family_name(winner.get("name")),
            family_name(loser.get("name")),
        ]
    )


def build_sackmann_overlap_index() -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not CANONICAL_MANIFEST.exists():
        return index

    try:
        manifest = load_json(CANONICAL_MANIFEST)
    except Exception:
        return index

    for item in manifest.get("year_files", []):
        path = Path(item.get("path", ""))
        if not path.exists():
            continue
        for row in iter_jsonl_gz(path):
            key = candidate_key(row)
            if key.count("|") == 3 and not key.endswith("|"):
                index[key].append(row)
    return index


def audit() -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    issues: list[dict[str, Any]] = []
    issue_counts: Counter = Counter()
    counters: Counter = Counter()
    level_surface_counts: Counter = Counter()
    event_keys: dict[str, str] = {}
    match_ids: dict[str, str] = {}
    canonical_hint_keys: dict[str, str] = {}

    files = read_source_files(SOURCE_API_MANIFEST)
    if not files:
        issue(issues, issue_counts, None, "missing_api_source_files", "No data/source/api_tennis/tle_api_matches_*.jsonl.gz files found")

    rows_for_overlap: list[dict[str, Any]] = []

    for path in files:
        file_count = 0
        for row in iter_jsonl_gz(path):
            file_count += 1
            counters["matches_checked"] += 1
            counters[f"file_{path.name}"] += 1

            match_id = text(row.get("match_id"))
            event_key = text(row.get("api_event_key"))
            date = text(row.get("date"))
            gender = text(row.get("gender"))
            level = text(row.get("level"))
            surface = text(row.get("surface"))
            winner = row.get("winner") or {}
            loser = row.get("loser") or {}
            winner_name = text(winner.get("name"))
            loser_name = text(loser.get("name"))

            if not match_id:
                issue(issues, issue_counts, row, "missing_match_id", "match_id is blank")
            elif match_id in match_ids:
                issue(issues, issue_counts, row, "duplicate_match_id", f"first seen in {match_ids[match_id]}")
            else:
                match_ids[match_id] = path.name

            if event_key:
                if event_key in event_keys:
                    issue(issues, issue_counts, row, "duplicate_api_event_key", f"first seen in {event_keys[event_key]}")
                else:
                    event_keys[event_key] = path.name
            else:
                issue(issues, issue_counts, row, "missing_api_event_key", "API event key is blank", severity="warning")

            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                issue(issues, issue_counts, row, "invalid_date", f"date={date!r}")

            if gender not in VALID_GENDERS:
                issue(issues, issue_counts, row, "invalid_gender", f"gender={gender!r}")
            else:
                counters[f"gender_{gender}"] += 1

            if level not in VALID_LEVELS:
                issue(issues, issue_counts, row, "invalid_level", f"level={level!r}")
            else:
                counters[f"level_{level}"] += 1

            if surface not in VALID_SURFACES:
                issue(issues, issue_counts, row, "invalid_surface", f"surface={surface!r}")
            else:
                counters[f"surface_{surface}"] += 1

            if surface == "unknown" and level not in {"itf", "qualifying"}:
                issue(issues, issue_counts, row, "unknown_surface_not_allowed", f"level={level!r}, surface={surface!r}")

            if not winner_name or not loser_name:
                issue(issues, issue_counts, row, "missing_player_name", "winner or loser name is blank")
            if winner_name and loser_name and slug(winner_name) == slug(loser_name):
                issue(issues, issue_counts, row, "same_winner_loser_name", "winner and loser names normalize to the same value")

            if not text(winner.get("player_key")) or not text(loser.get("player_key")):
                issue(issues, issue_counts, row, "missing_player_key", "winner or loser player_key is blank")
            elif text(winner.get("player_key")) == text(loser.get("player_key")):
                issue(issues, issue_counts, row, "same_winner_loser_key", "winner and loser player_key are identical")

            hint = text(row.get("canonical_hint_key"))
            if hint:
                if hint in canonical_hint_keys:
                    issue(issues, issue_counts, row, "duplicate_canonical_hint_key", f"first seen in {canonical_hint_keys[hint]}", severity="warning")
                else:
                    canonical_hint_keys[hint] = path.name
            else:
                issue(issues, issue_counts, row, "missing_canonical_hint_key", "canonical_hint_key is blank", severity="warning")

            level_surface_counts[f"{level}|{surface}"] += 1
            rows_for_overlap.append(row)

        counters["files_read"] += 1
        counters[f"rows_in_{path.name}"] = file_count

    overlap_rows: list[dict[str, Any]] = []
    sackmann_index = build_sackmann_overlap_index()
    if sackmann_index:
        counters["sackmann_overlap_index_keys"] = len(sackmann_index)
        for api_row in rows_for_overlap:
            key = candidate_key(api_row)
            candidates = sackmann_index.get(key) or []
            if not candidates:
                continue
            counters["possible_sackmann_overlaps"] += 1
            api_w = (api_row.get("winner") or {}).get("name", "")
            api_l = (api_row.get("loser") or {}).get("name", "")
            for sm in candidates[:3]:
                sw = (sm.get("winner") or {}).get("name", "")
                sl = (sm.get("loser") or {}).get("name", "")
                same_tourney_token = bool(set(slug(api_row.get("tourney_name")).split("-")) & set(slug(sm.get("tourney_name")).split("-")))
                confidence = "medium" if same_tourney_token else "low"
                overlap_rows.append(
                    {
                        "confidence": confidence,
                        "api_match_id": api_row.get("match_id", ""),
                        "api_event_key": api_row.get("api_event_key", ""),
                        "date": api_row.get("date", ""),
                        "gender": api_row.get("gender", ""),
                        "api_level": api_row.get("level", ""),
                        "api_surface": api_row.get("surface", ""),
                        "api_tourney_name": api_row.get("tourney_name", ""),
                        "api_winner": api_w,
                        "api_loser": api_l,
                        "sackmann_match_id": sm.get("match_id", ""),
                        "sackmann_level": sm.get("level", ""),
                        "sackmann_surface": sm.get("surface", ""),
                        "sackmann_tourney_name": sm.get("tourney_name", ""),
                        "sackmann_winner": sw,
                        "sackmann_loser": sl,
                        "match_key": key,
                    }
                )
    else:
        counters["sackmann_overlap_index_keys"] = 0

    import_report = load_json(IMPORT_REPORT) if IMPORT_REPORT.exists() else {}
    manifest = load_json(SOURCE_API_MANIFEST) if SOURCE_API_MANIFEST.exists() else {}
    skipped_reasons = Counter()
    if SKIPPED_CSV.exists():
        with SKIPPED_CSV.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                reason = row.get("skip_reason") or "<blank>"
                skipped_reasons[reason] += 1

    duplicate_count = 0
    if DUPLICATES_CSV.exists():
        with DUPLICATES_CSV.open("r", encoding="utf-8", newline="") as fh:
            duplicate_count = max(sum(1 for _ in csv.DictReader(fh)), 0)

    expected = int((manifest.get("matches") or import_report.get("matches") or 0) or 0)
    if expected and expected != counters["matches_checked"]:
        issue(issues, issue_counts, None, "manifest_count_mismatch", f"manifest matches={expected}, rows checked={counters['matches_checked']}")

    error_count = sum(v for k, v in issue_counts.items() if k.startswith("error:"))
    warning_count = sum(v for k, v in issue_counts.items() if k.startswith("warning:"))
    status = "ok" if error_count == 0 else "fail"

    audit_payload = {
        "generated_at": now_utc_iso(),
        "status": status,
        "source": "api_tennis",
        "source_manifest": str(SOURCE_API_MANIFEST),
        "files": [str(p) for p in files],
        "matches_checked": counters["matches_checked"],
        "issues_total": len(issues),
        "errors_total": error_count,
        "warnings_total": warning_count,
        "issue_counts": dict(issue_counts),
        "counters": dict(counters),
        "level_surface_counts": dict(level_surface_counts),
        "skipped_reasons_from_import": dict(skipped_reasons),
        "duplicate_rows_from_import": duplicate_count,
        "possible_sackmann_overlap_candidates": len(overlap_rows),
        "notes": [
            "Sackmann overlap candidates are heuristic only because API player names are abbreviated and not yet canonically mapped.",
            "Authoritative Sackmann/API dedup will be done later in combined canonical merge after player mapping.",
        ],
        "outputs": {
            "audit_json": str(AUDIT_JSON),
            "issues_csv": str(ISSUES_CSV),
            "sackmann_overlap_candidates_csv": str(OVERLAP_CSV),
        },
    }

    write_json(AUDIT_JSON, audit_payload)
    write_csv(ISSUES_CSV, issues, ISSUE_FIELDS)
    write_csv(OVERLAP_CSV, overlap_rows[:5000], OVERLAP_FIELDS)
    print(json.dumps(audit_payload, indent=2, ensure_ascii=False, sort_keys=True))
    return audit_payload


if __name__ == "__main__":
    audit()

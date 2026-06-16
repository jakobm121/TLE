from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

RAW_RESULTS_DIR = Path("data/raw/api_tennis/results")
SOURCE_API_DIR = Path("data/source/api_tennis")
REPORT_DIR = Path("data/reports/api_tennis")

MANIFEST_JSON = SOURCE_API_DIR / "manifest.json"
IMPORT_REPORT_JSON = REPORT_DIR / "import_api_results_report.json"
SKIPPED_CSV = REPORT_DIR / "skipped_api_matches.csv"
FIELD_AUDIT_JSON = REPORT_DIR / "api_field_audit.json"
DUPLICATES_CSV = REPORT_DIR / "duplicate_api_matches.csv"

RAW_METADATA_DIR = Path("data/raw/api_tennis/metadata")
TOURNAMENTS_METADATA_JSON = RAW_METADATA_DIR / "get_tournaments.json"

RAW_PLAYERS_DIR = Path("data/raw/api_tennis/players")
API_PLAYER_CACHE_JSON = RAW_PLAYERS_DIR / "api_players.json"

SURFACES = {"hard", "clay", "grass", "carpet"}

GRAND_SLAM_PATTERNS = (
    "australian open",
    "roland garros",
    "french open",
    "wimbledon",
    "us open",
    "u.s. open",
    "united states open",
)

ITF_PATTERNS = (
    " itf ",
    "itf ",
    " itf",
    "m15",
    "m25",
    "m35",
    "w15",
    "w25",
    "w35",
    "w50",
    "w75",
    "w100",
    "w125",
)

QUAL_PATTERNS = (
    "qualification",
    "qualifying",
    " qualifiers",
    " qualifier",
    " qual ",
    " qual.",
    " - q",
)

FINISHED_STATUSES = {
    "finished",
    "ended",
    "complete",
    "completed",
    "final",
    "retired",
    "walkover",
    "wo",
    "w/o",
}

SKIP_FIELDS = [
    "source_file",
    "raw_index",
    "api_event_key",
    "date",
    "gender",
    "event_status",
    "event_type_type",
    "tournament_name",
    "league_name",
    "round",
    "surface_raw",
    "normalized_level",
    "normalized_surface",
    "first_player_key",
    "first_player_name",
    "second_player_key",
    "second_player_name",
    "winner_side",
    "winner_name",
    "loser_name",
    "score",
    "skip_reason",
]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl_gz(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text)


def lower_text(value: Any) -> str:
    return norm_text(value).lower()


def slug(value: Any) -> str:
    text = lower_text(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


def get_first(obj: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = obj.get(key)
        if value is not None and str(value).strip() != "":
            return norm_text(value)
    return ""


def get_first_raw(obj: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = obj.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def parse_date(value: Any, fallback: str = "") -> str:
    text = norm_text(value) or fallback
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except Exception:
        return ""


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def extract_fixtures(payload: Any) -> list[dict[str, Any]]:
    """Extract fixtures from raw API-Tennis payloads.

    Fetch step stores a wrapper like:
      {schema_version, source, method, date, request, response: {success, result}}

    This importer also accepts direct API responses and a few common alternate
    shapes so it can be rerun after future fetch changes.
    """
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    # Our raw fetch wrapper puts the real API response under "response".
    # Unwrap once, then continue with the same generic extraction logic.
    response = payload.get("response")
    if isinstance(response, dict):
        return extract_fixtures(response)

    result = payload.get("result")
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        fixtures: list[dict[str, Any]] = []
        for value in result.values():
            if isinstance(value, list):
                fixtures.extend(x for x in value if isinstance(x, dict))
            elif isinstance(value, dict):
                fixtures.append(value)
        return fixtures

    for key in ("fixtures", "events", "data", "matches"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = extract_fixtures(value)
            if nested:
                return nested

    return []


def is_doubles(fixture: dict[str, Any]) -> bool:
    joined = " | ".join(
        lower_text(fixture.get(k))
        for k in (
            "event_type_type",
            "event_type",
            "event_name",
            "league_name",
            "tournament_name",
        )
    )
    if "double" in joined or "doubles" in joined:
        return True

    p1 = lower_text(get_first(fixture, ("event_first_player", "first_player", "player1", "home_team")))
    p2 = lower_text(get_first(fixture, ("event_second_player", "second_player", "player2", "away_team")))
    return "/" in p1 or " & " in p1 or "/" in p2 or " & " in p2


def infer_gender(fixture: dict[str, Any]) -> str:
    text = " | ".join(
        lower_text(fixture.get(k))
        for k in (
            "event_type_type",
            "event_type",
            "league_name",
            "tournament_name",
            "event_name",
        )
    )
    if re.search(r"\bwta\b|women|female|girls", text):
        return "women"
    if re.search(r"\batp\b|men|male|boys", text):
        return "men"
    return "unknown"


def normalize_surface_value(raw: Any) -> str:
    text = lower_text(raw)
    if "hard" in text:
        return "hard"
    if "clay" in text:
        return "clay"
    if "grass" in text:
        return "grass"
    if "carpet" in text:
        return "carpet"
    return "unknown"


def load_tournament_surface_map(path: Path = TOURNAMENTS_METADATA_JSON) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    report = {
        "path": str(path),
        "exists": path.exists(),
        "rows": 0,
        "mapped_tournaments": 0,
        "surface_counts": Counter(),
        "raw_surface_field_counts": Counter(),
        "missing_key": 0,
    }
    surface_map: dict[str, dict[str, str]] = {}
    if not path.exists():
        return surface_map, report

    try:
        payload = load_json(path)
        rows = extract_fixtures(payload)
    except Exception as exc:
        report["error"] = str(exc)
        return surface_map, report

    report["rows"] = len(rows)
    for row in rows:
        key = get_first(row, ("tournament_key", "league_key", "id"))
        if not key:
            report["missing_key"] += 1
            continue

        raw_surface = get_first(
            row,
            (
                "tournament_sourface",  # API-Tennis typo, confirmed by live inspect.
                "tournament_surface",
                "surface",
                "court_surface",
                "court_type",
            ),
        )
        surface = normalize_surface_value(raw_surface)
        report["raw_surface_field_counts"][raw_surface or "<blank>"] += 1
        report["surface_counts"][surface] += 1

        if surface != "unknown":
            surface_map[str(key)] = {
                "surface": surface,
                "raw_surface": raw_surface,
                "tournament_name": get_first(row, ("tournament_name", "league_name")),
                "event_type_type": get_first(row, ("event_type_type", "event_type")),
            }

    report["mapped_tournaments"] = len(surface_map)
    return surface_map, report


def normalize_surface(fixture: dict[str, Any], tournament_surface_map: dict[str, dict[str, str]] | None = None) -> tuple[str, str]:
    raw = get_first(
        fixture,
        (
            "event_surface",
            "surface",
            "tournament_surface",
            "tournament_sourface",
            "league_surface",
            "court_surface",
            "court_type",
        ),
    )

    surface = normalize_surface_value(raw)
    if surface != "unknown":
        return surface, raw

    tournament_key = get_first(fixture, ("tournament_key", "league_key"))
    if tournament_surface_map and tournament_key:
        meta = tournament_surface_map.get(str(tournament_key))
        if meta and meta.get("surface") in SURFACES:
            return meta["surface"], f"api_tournament_metadata:{meta.get('raw_surface', '')}"

    joined = " | ".join(
        lower_text(fixture.get(k))
        for k in ("league_name", "tournament_name", "event_name")
    )
    guessed = normalize_surface_value(joined)
    if guessed != "unknown":
        return guessed, f"name_guess:{joined[:120]}"

    return "unknown", raw


def infer_level(fixture: dict[str, Any], gender: str) -> str:
    text = " " + " | ".join(
        lower_text(fixture.get(k))
        for k in (
            "league_name",
            "tournament_name",
            "event_name",
            "event_type_type",
            "event_type",
            "tournament_type",
            "event_round",
            "tournament_round",
        )
    ) + " "

    if any(pattern in text for pattern in GRAND_SLAM_PATTERNS):
        return "grand_slam"

    if any(pattern in text for pattern in QUAL_PATTERNS):
        return "qualifying"

    if "challenger" in text or " chall " in text:
        return "challenger"

    if any(pattern in text for pattern in ITF_PATTERNS):
        return "itf"

    if gender in {"men", "women"}:
        if re.search(r"\batp\b|\bwta\b|tour finals|united cup|davis cup|bjk cup|fed cup", text):
            return "atp_wta"

    return "unknown"


def is_finished(fixture: dict[str, Any]) -> bool:
    status = lower_text(get_first(fixture, ("event_status", "status", "match_status", "event_status_type")))
    if any(x in status for x in FINISHED_STATUSES):
        return True
    if get_first(fixture, ("event_winner", "winner", "event_winner_code", "event_winner_type")):
        return True
    score = get_first(fixture, ("event_final_result", "final_result", "score"))
    return bool(score and re.search(r"\d", score))


def player_fields(fixture: dict[str, Any]) -> dict[str, str]:
    return {
        "first_id": get_first(fixture, ("first_player_key", "event_first_player_key", "player1_key", "home_team_key")),
        "first_name": get_first(fixture, ("event_first_player", "first_player", "player1", "home_team")),
        "second_id": get_first(fixture, ("second_player_key", "event_second_player_key", "player2_key", "away_team_key")),
        "second_name": get_first(fixture, ("event_second_player", "second_player", "player2", "away_team")),
    }


def load_api_player_cache(path: Path = API_PLAYER_CACHE_JSON) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    report = {
        "path": str(path),
        "exists": path.exists(),
        "entries": 0,
        "with_full_name": 0,
        "with_short_name": 0,
    }

    payload = read_json(path, {})
    if not isinstance(payload, dict):
        report["error"] = "cache is not a JSON object"
        return {}, report

    cache: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        pid = norm_text(value.get("player_key")) or norm_text(key)
        if not pid:
            continue
        cache[pid] = value

    report["entries"] = len(cache)
    report["with_full_name"] = sum(1 for v in cache.values() if norm_text(v.get("player_full_name")))
    report["with_short_name"] = sum(1 for v in cache.values() if norm_text(v.get("player_name")))

    return cache, report


def cached_player_name(api_player_cache: dict[str, dict[str, Any]], api_player_id: str, fallback_name: str) -> tuple[str, str]:
    """Return best API-Tennis name for a player.

    Priority:
    1. player_full_name from 06b cache
    2. player_name from 06b cache
    3. raw fixture name
    """
    pid = norm_text(api_player_id)
    fallback = norm_text(fallback_name)

    if not pid:
        return fallback, "fixture"

    row = api_player_cache.get(pid)
    if not isinstance(row, dict):
        return fallback, "fixture"

    full_name = norm_text(row.get("player_full_name"))
    if full_name:
        return full_name, "api_cache_full_name"

    short_name = norm_text(row.get("player_name"))
    if short_name:
        return short_name, "api_cache_short_name"

    return fallback, "fixture"


def enrich_players_from_api_cache(
    players: dict[str, str],
    api_player_cache: dict[str, dict[str, Any]],
    counters: Counter,
) -> dict[str, str]:
    enriched = dict(players)

    first_name, first_source = cached_player_name(api_player_cache, players.get("first_id", ""), players.get("first_name", ""))
    second_name, second_source = cached_player_name(api_player_cache, players.get("second_id", ""), players.get("second_name", ""))

    if first_source == "api_cache_full_name":
        counters["api_player_cache_first_full_name_used"] += 1
    elif first_source == "api_cache_short_name":
        counters["api_player_cache_first_short_name_used"] += 1

    if second_source == "api_cache_full_name":
        counters["api_player_cache_second_full_name_used"] += 1
    elif second_source == "api_cache_short_name":
        counters["api_player_cache_second_short_name_used"] += 1

    if first_name and first_name != players.get("first_name", ""):
        counters["api_player_cache_name_changed"] += 1
        counters["api_player_cache_first_name_changed"] += 1
    if second_name and second_name != players.get("second_name", ""):
        counters["api_player_cache_name_changed"] += 1
        counters["api_player_cache_second_name_changed"] += 1

    if first_name and not players.get("first_name"):
        counters["api_player_cache_filled_missing_name"] += 1
    if second_name and not players.get("second_name"):
        counters["api_player_cache_filled_missing_name"] += 1

    enriched["first_name"] = first_name
    enriched["second_name"] = second_name
    return enriched


def infer_winner_loser(fixture: dict[str, Any], players: dict[str, str]) -> tuple[str, str, str, str, str]:
    winner_raw = get_first(fixture, ("event_winner", "winner", "event_winner_code", "event_winner_type"))
    wr = lower_text(winner_raw)

    first_name = players["first_name"]
    second_name = players["second_name"]
    first_id = players["first_id"]
    second_id = players["second_id"]

    first_tokens = {"first player", "first", "1", "home", "event_first_player", first_id.lower()}
    second_tokens = {"second player", "second", "2", "away", "event_second_player", second_id.lower()}

    if wr in first_tokens or wr == lower_text(first_name):
        return "first", first_id, first_name, second_id, second_name
    if wr in second_tokens or wr == lower_text(second_name):
        return "second", second_id, second_name, first_id, first_name

    # API-Tennis commonly uses event_winner = "First Player" / "Second Player".
    if "first" in wr:
        return "first", first_id, first_name, second_id, second_name
    if "second" in wr:
        return "second", second_id, second_name, first_id, first_name

    return "", "", "", "", ""


def api_player_key(gender: str, api_id: str, name: str) -> str:
    if api_id:
        return f"{gender}:api:{api_id}"
    return f"{gender}:api_name:{slug(name)}"


def stable_api_match_key(fixture: dict[str, Any], date: str, winner_name: str, loser_name: str) -> str:
    event_key = get_first(fixture, ("event_key", "fixture_key", "match_key", "id"))
    if event_key:
        return f"api_event:{event_key}"

    tournament = get_first(fixture, ("tournament_key", "league_key", "tournament_name", "league_name"))
    round_name = get_first(fixture, ("event_round", "tournament_round", "round"))
    score = get_first(fixture, ("event_final_result", "final_result", "score"))
    return "fallback:" + "|".join(
        [date, slug(tournament), slug(round_name), slug(winner_name), slug(loser_name), slug(score)]
    )


def build_match(
    fixture: dict[str, Any],
    source_file: str,
    date: str,
    gender: str,
    level: str,
    surface: str,
    surface_raw: str,
    winner_side: str,
    winner_id: str,
    winner_name: str,
    loser_id: str,
    loser_name: str,
) -> dict[str, Any]:
    event_key = get_first(fixture, ("event_key", "fixture_key", "match_key", "id"))
    score = get_first(fixture, ("event_final_result", "final_result", "score"))
    tournament_name = get_first(fixture, ("tournament_name", "league_name", "event_name"))
    tournament_key = get_first(fixture, ("tournament_key", "league_key"))
    round_name = get_first(fixture, ("event_round", "tournament_round", "round"))

    return {
        "match_id": f"api_tennis:{event_key}" if event_key else stable_api_match_key(fixture, date, winner_name, loser_name),
        "canonical_hint_key": "|".join(
            [gender, date, slug(tournament_name or tournament_key), slug(round_name), slug(winner_name), slug(loser_name)]
        ),
        "source": "api_tennis",
        "source_file": source_file,
        "api_event_key": event_key,
        "date": date,
        "year": int(date[:4]),
        "gender": gender,
        "level": level,
        "surface": surface,
        "surface_raw": surface_raw,
        "tourney_id": tournament_key,
        "tourney_name": tournament_name,
        "league_name": get_first(fixture, ("league_name",)),
        "round": round_name,
        "score": score,
        "event_status": get_first(fixture, ("event_status", "status", "match_status", "event_status_type")),
        "event_type_type": get_first(fixture, ("event_type_type", "event_type")),
        "winner_side": winner_side,
        "winner": {
            "name": winner_name,
            "api_player_id": winner_id,
            "player_key": api_player_key(gender, winner_id, winner_name),
        },
        "loser": {
            "name": loser_name,
            "api_player_id": loser_id,
            "player_key": api_player_key(gender, loser_id, loser_name),
        },
        "raw": fixture,
    }


def skip_row(skipped: list[dict[str, Any]], counters: Counter, source_file: str, raw_index: int, fixture: dict[str, Any], reason: str, *, date: str = "", gender: str = "", level: str = "", surface: str = "", surface_raw: str = "", winner_side: str = "", winner_name: str = "", loser_name: str = "") -> None:
    counters[f"skipped_{reason}"] += 1
    players = player_fields(fixture)
    skipped.append(
        {
            "source_file": source_file,
            "raw_index": raw_index,
            "api_event_key": get_first(fixture, ("event_key", "fixture_key", "match_key", "id")),
            "date": date or get_first(fixture, ("event_date", "date", "match_date")),
            "gender": gender,
            "event_status": get_first(fixture, ("event_status", "status", "match_status", "event_status_type")),
            "event_type_type": get_first(fixture, ("event_type_type", "event_type")),
            "tournament_name": get_first(fixture, ("tournament_name",)),
            "league_name": get_first(fixture, ("league_name",)),
            "round": get_first(fixture, ("event_round", "tournament_round", "round")),
            "surface_raw": surface_raw or get_first(fixture, ("event_surface", "surface", "tournament_surface", "league_surface")),
            "normalized_level": level,
            "normalized_surface": surface,
            "first_player_key": players["first_id"],
            "first_player_name": players["first_name"],
            "second_player_key": players["second_id"],
            "second_player_name": players["second_name"],
            "winner_side": winner_side,
            "winner_name": winner_name,
            "loser_name": loser_name,
            "score": get_first(fixture, ("event_final_result", "final_result", "score")),
            "skip_reason": reason,
        }
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, default=RAW_RESULTS_DIR)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_API_DIR)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--tournaments-metadata", type=Path, default=TOURNAMENTS_METADATA_JSON)
    parser.add_argument("--api-player-cache", type=Path, default=API_PLAYER_CACHE_JSON)
    args = parser.parse_args(argv)

    ensure_dirs(args.source_dir, REPORT_DIR)

    tournament_surface_map, tournament_surface_map_report = load_tournament_surface_map(args.tournaments_metadata)
    api_player_cache, api_player_cache_report = load_api_player_cache(args.api_player_cache)

    counters = Counter()
    counters["tournament_surface_map_rows"] = tournament_surface_map_report.get("rows", 0)
    counters["tournament_surface_map_entries"] = len(tournament_surface_map)
    counters["api_player_cache_entries"] = api_player_cache_report.get("entries", 0)
    counters["api_player_cache_with_full_name"] = api_player_cache_report.get("with_full_name", 0)
    counters["api_player_cache_with_short_name"] = api_player_cache_report.get("with_short_name", 0)

    skipped_rows: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []
    by_year: dict[int, list[dict[str, Any]]] = defaultdict(list)
    seen_keys: dict[str, str] = {}

    field_audit: dict[str, Any] = {
        "files": {},
        "gender_counts": Counter(),
        "level_counts": Counter(),
        "surface_counts": Counter(),
        "status_counts": Counter(),
        "event_type_counts": Counter(),
        "raw_surface_counts": Counter(),
    }

    files = sorted(args.raw_dir.glob("*.json"))
    for path in files:
        fallback_date = path.stem
        if args.start_date and fallback_date < args.start_date:
            continue
        if args.end_date and fallback_date > args.end_date:
            continue

        payload = load_json(path)
        fixtures = extract_fixtures(payload)
        field_audit["files"][path.name] = {"raw_fixtures": len(fixtures), "imported": 0, "skipped": 0}
        counters["raw_fixtures"] += len(fixtures)

        for raw_index, fixture in enumerate(fixtures):
            if not isinstance(fixture, dict):
                continue

            counters["input_fixtures"] += 1
            status = get_first(fixture, ("event_status", "status", "match_status", "event_status_type")) or "<blank>"
            event_type = get_first(fixture, ("event_type_type", "event_type")) or "<blank>"
            field_audit["status_counts"][status] += 1
            field_audit["event_type_counts"][event_type] += 1

            date = parse_date(get_first(fixture, ("event_date", "date", "match_date")), fallback=fallback_date)
            if not date:
                skip_row(skipped_rows, counters, path.name, raw_index, fixture, "missing_or_invalid_date")
                field_audit["files"][path.name]["skipped"] += 1
                continue

            if is_doubles(fixture):
                skip_row(skipped_rows, counters, path.name, raw_index, fixture, "doubles_or_team_match", date=date)
                field_audit["files"][path.name]["skipped"] += 1
                continue

            if not is_finished(fixture):
                skip_row(skipped_rows, counters, path.name, raw_index, fixture, "not_finished", date=date)
                field_audit["files"][path.name]["skipped"] += 1
                continue

            gender = infer_gender(fixture)
            if gender == "unknown":
                skip_row(skipped_rows, counters, path.name, raw_index, fixture, "unknown_gender", date=date, gender=gender)
                field_audit["files"][path.name]["skipped"] += 1
                continue

            players = player_fields(fixture)
            players = enrich_players_from_api_cache(players, api_player_cache, counters)

            if not players["first_name"] or not players["second_name"]:
                skip_row(skipped_rows, counters, path.name, raw_index, fixture, "missing_player_name", date=date, gender=gender)
                field_audit["files"][path.name]["skipped"] += 1
                continue

            winner_side, winner_id, winner_name, loser_id, loser_name = infer_winner_loser(fixture, players)
            if not winner_name or not loser_name:
                skip_row(skipped_rows, counters, path.name, raw_index, fixture, "missing_or_unresolved_winner", date=date, gender=gender, winner_side=winner_side)
                field_audit["files"][path.name]["skipped"] += 1
                continue

            level = infer_level(fixture, gender)
            if level == "unknown":
                skip_row(skipped_rows, counters, path.name, raw_index, fixture, "unknown_level", date=date, gender=gender, level=level, winner_side=winner_side, winner_name=winner_name, loser_name=loser_name)
                field_audit["files"][path.name]["skipped"] += 1
                continue

            surface, surface_raw = normalize_surface(fixture, tournament_surface_map)
            field_audit["raw_surface_counts"][surface_raw or "<blank>"] += 1
            if surface == "unknown" and level not in {"itf", "qualifying"}:
                skip_row(skipped_rows, counters, path.name, raw_index, fixture, "unknown_surface_not_allowed", date=date, gender=gender, level=level, surface=surface, surface_raw=surface_raw, winner_side=winner_side, winner_name=winner_name, loser_name=loser_name)
                field_audit["files"][path.name]["skipped"] += 1
                continue

            key = stable_api_match_key(fixture, date, winner_name, loser_name)
            if key in seen_keys:
                counters["duplicate_fixtures"] += 1
                duplicate_rows.append(
                    {
                        "duplicate_key": key,
                        "first_source_file": seen_keys[key],
                        "duplicate_source_file": path.name,
                        "date": date,
                        "gender": gender,
                        "level": level,
                        "surface": surface,
                        "winner_name": winner_name,
                        "loser_name": loser_name,
                        "tournament_name": get_first(fixture, ("tournament_name", "league_name")),
                        "api_event_key": get_first(fixture, ("event_key", "fixture_key", "match_key", "id")),
                    }
                )
                continue
            seen_keys[key] = path.name

            match = build_match(
                fixture,
                path.name,
                date,
                gender,
                level,
                surface,
                surface_raw,
                winner_side,
                winner_id,
                winner_name,
                loser_id,
                loser_name,
            )

            counters["imported"] += 1
            counters[f"gender_{gender}"] += 1
            counters[f"level_{level}"] += 1
            counters[f"surface_{surface}"] += 1
            field_audit["gender_counts"][gender] += 1
            field_audit["level_counts"][level] += 1
            field_audit["surface_counts"][surface] += 1
            field_audit["files"][path.name]["imported"] += 1
            by_year[match["year"]].append(match)

    year_files = []
    for year, rows in sorted(by_year.items()):
        rows.sort(key=lambda r: (r["date"], r["match_id"]))
        out = args.source_dir / f"tle_api_matches_{year}.jsonl.gz"
        count = write_jsonl_gz(out, rows)
        year_files.append({"year": year, "path": str(out), "matches": count, "created_at": now_utc_iso()})

    manifest = {
        "generated_at": now_utc_iso(),
        "source": "api_tennis",
        "raw_results_dir": str(args.raw_dir),
        "tournaments_metadata": tournament_surface_map_report,
        "api_player_cache": api_player_cache_report,
        "matches": counters["imported"],
        "year_files": year_files,
        "counters": dict(counters),
        "outputs": {
            "manifest": str(MANIFEST_JSON),
            "import_report": str(IMPORT_REPORT_JSON),
            "skipped_csv": str(SKIPPED_CSV),
            "duplicates_csv": str(DUPLICATES_CSV),
            "field_audit": str(FIELD_AUDIT_JSON),
        },
    }

    field_audit["tournament_surface_map"] = tournament_surface_map_report
    field_audit["api_player_cache"] = api_player_cache_report
    serial_field_audit = json.loads(json.dumps(field_audit, default=dict))
    write_json(MANIFEST_JSON, manifest)
    write_json(IMPORT_REPORT_JSON, manifest)
    write_json(FIELD_AUDIT_JSON, serial_field_audit)
    write_csv(SKIPPED_CSV, skipped_rows, SKIP_FIELDS)
    write_csv(
        DUPLICATES_CSV,
        duplicate_rows,
        [
            "duplicate_key",
            "first_source_file",
            "duplicate_source_file",
            "date",
            "gender",
            "level",
            "surface",
            "winner_name",
            "loser_name",
            "tournament_name",
            "api_event_key",
        ],
    )

    print(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

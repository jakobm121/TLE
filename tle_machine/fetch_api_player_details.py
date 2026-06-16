from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_BASE_URL = "https://api.api-tennis.com/tennis/"
RAW_RESULTS_DIR = Path("data/raw/api_tennis/results")
RAW_PLAYERS_DIR = Path("data/raw/api_tennis/players")
PLAYER_CACHE_JSON = RAW_PLAYERS_DIR / "api_players.json"
REPORT_DIR = Path("data/reports/api_tennis")
REPORT_JSON = REPORT_DIR / "fetch_api_player_details_report.json"
RAW_RESPONSES_JSON = REPORT_DIR / "fetch_api_player_details_raw_responses.json"

# Daily production defaults. Full backfill should be done by explicit workflow inputs.
DEFAULT_SLEEP_SECONDS = 0.05
DEFAULT_TIMEOUT_SECONDS = 10
MAX_RAW_RESPONSES_TO_SAVE = 50


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def extract_fixtures(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []

    if isinstance(payload.get("response"), dict):
        return extract_fixtures(payload["response"])

    result = payload.get("result")
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        rows: list[dict[str, Any]] = []
        for value in result.values():
            if isinstance(value, list):
                rows.extend(x for x in value if isinstance(x, dict))
            elif isinstance(value, dict):
                rows.append(value)
        return rows

    for key in ("fixtures", "events", "data", "matches"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = extract_fixtures(value)
            if nested:
                return nested

    return []


def norm_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return ""
    return text


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def is_doubles_fixture(row: dict[str, Any]) -> bool:
    """
    Best-effort doubles filter for API-Tennis raw fixtures.

    Conservative rule:
    - if clearly doubles, skip
    - if uncertain, keep

    This prevents doubles/team entities from polluting api_players.json and from
    inflating fetched_missing_full_name.
    """
    text_fields = (
        "event_type",
        "event_name",
        "event_title",
        "league_name",
        "league",
        "tournament_name",
        "tournament",
        "competition_name",
        "event_first_player",
        "event_second_player",
        "first_player",
        "second_player",
        "home_team",
        "away_team",
        "home_team_name",
        "away_team_name",
    )

    for key in text_fields:
        value = norm_text(row.get(key)).lower()
        if not value:
            continue
        if "doubles" in value:
            return True
        if "double" in value:
            return True

    name_fields = (
        "event_first_player",
        "event_second_player",
        "first_player",
        "second_player",
        "home_team",
        "away_team",
        "home_team_name",
        "away_team_name",
    )

    for key in name_fields:
        value = norm_text(row.get(key))
        if not value:
            continue

        # API-Tennis / sports feeds commonly represent doubles teams as "A / B".
        if "/" in value:
            return True

        # Defensive check for HTML/alternate separators sometimes seen in feeds.
        lowered = value.lower()
        if " & " in lowered:
            return True
        if " + " in lowered:
            return True

    return False


def collect_player_ids(raw_dir: Path, start_date: str = "", end_date: str = "") -> tuple[set[str], dict[str, int], dict[str, int]]:
    ids: set[str] = set()
    file_counts: dict[str, int] = {}
    stats = {
        "raw_fixtures_seen": 0,
        "skipped_doubles_fixtures": 0,
        "singles_fixtures_used": 0,
        "files_seen": 0,
    }

    for path in sorted(raw_dir.glob("*.json")):
        day = path.stem
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue

        stats["files_seen"] += 1
        payload = read_json(path, {})
        fixtures = extract_fixtures(payload)
        file_counts[path.name] = len(fixtures)

        for row in fixtures:
            stats["raw_fixtures_seen"] += 1

            if is_doubles_fixture(row):
                stats["skipped_doubles_fixtures"] += 1
                continue

            stats["singles_fixtures_used"] += 1

            for key in ("first_player_key", "event_first_player_key", "player1_key", "home_team_key"):
                pid = norm_id(row.get(key))
                if pid:
                    ids.add(pid)
                    break

            for key in ("second_player_key", "event_second_player_key", "player2_key", "away_team_key"):
                pid = norm_id(row.get(key))
                if pid:
                    ids.add(pid)
                    break

    return ids, file_counts, stats


def api_success(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    return response.get("success") in (1, "1", True, "true", "True")


def extract_player_rows(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    result = response.get("result")
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        rows: list[dict[str, Any]] = []
        for value in result.values():
            if isinstance(value, dict):
                rows.append(value)
            elif isinstance(value, list):
                rows.extend(x for x in value if isinstance(x, dict))
        return rows
    return []


def fetch_player(api_key: str, player_key: str, timeout: int) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "method": "get_players",
            "APIkey": api_key,
            "player_key": player_key,
        }
    )
    url = f"{API_BASE_URL}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "tle-machine/1.0"})

    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"API returned non-JSON for player_key={player_key}: {raw[:300]}") from exc

    if not isinstance(decoded, dict):
        raise RuntimeError(f"API returned unexpected JSON type for player_key={player_key}: {type(decoded).__name__}")

    return decoded


def pick_player_row(rows: list[dict[str, Any]], player_key: str) -> dict[str, Any] | None:
    for row in rows:
        if norm_id(row.get("player_key")) == player_key:
            return row
    if len(rows) == 1:
        return rows[0]
    return None


def simplify_player_row(row: dict[str, Any], player_key: str) -> dict[str, Any]:
    full_name = norm_text(row.get("player_full_name"))
    short_name = norm_text(row.get("player_name"))

    return {
        "player_key": norm_id(row.get("player_key")) or player_key,
        "player_name": short_name,
        "player_full_name": full_name,
        "player_country": norm_text(row.get("player_country")),
        "player_bday": norm_text(row.get("player_bday")),
        "player_logo": norm_text(row.get("player_logo")),
        "updated_at": now_utc_iso(),
        "source": "api_tennis:get_players",
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetch/cache API-Tennis player details for player ids found in raw fixtures.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_RESULTS_DIR)
    parser.add_argument("--cache-path", type=Path, default=PLAYER_CACHE_JSON)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--player-keys", default="", help="Optional comma-separated player keys. If omitted, collect from raw fixtures.")
    parser.add_argument("--refresh-existing", action="store_true", help="Refetch ids already present in cache.")
    parser.add_argument("--max-players", type=int, default=0, help="Optional cap for debugging; 0 = no cap.")
    parser.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--fail-on-any-error", action="store_true")
    parser.add_argument(
        "--save-raw-responses",
        action="store_true",
        help=f"Save up to {MAX_RAW_RESPONSES_TO_SAVE} raw API responses for debugging.",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("API_TENNIS_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing API_TENNIS_KEY environment variable / GitHub Actions secret.")

    ensure_dirs(args.cache_path.parent, REPORT_DIR)

    cache: dict[str, Any] = read_json(args.cache_path, {})
    if not isinstance(cache, dict):
        cache = {}

    collect_stats: dict[str, int] = {}

    if args.player_keys.strip():
        player_ids = {norm_id(x) for x in args.player_keys.split(",") if norm_id(x)}
        file_counts: dict[str, int] = {}
    else:
        player_ids, file_counts, collect_stats = collect_player_ids(args.raw_dir, args.start_date, args.end_date)

    ids_sorted = sorted(player_ids, key=lambda x: int(x) if x.isdigit() else x)
    if args.max_players and args.max_players > 0:
        ids_sorted = ids_sorted[: args.max_players]

    counters = Counter()
    counters["candidate_player_ids"] = len(ids_sorted)
    counters["cache_existing_before"] = len(cache)

    raw_responses: dict[str, Any] = {}
    errors: list[str] = []
    fetched_details: list[dict[str, Any]] = []

    # Intentional:
    # - players with full name are kept and not refetched
    # - players without full name are refetched when they appear in the selected date range,
    #   because API-Tennis may add full names later
    to_fetch = [
        pid
        for pid in ids_sorted
        if args.refresh_existing
        or pid not in cache
        or not isinstance(cache.get(pid), dict)
        or not cache.get(pid, {}).get("player_full_name")
    ]
    counters["to_fetch"] = len(to_fetch)

    for index, player_key in enumerate(to_fetch, start=1):
        if index == 1 or index % 25 == 0 or index == len(to_fetch):
            print(f"[06b] fetching {index}/{len(to_fetch)} player_key={player_key}", flush=True)

        try:
            response = fetch_player(api_key, player_key, args.timeout)

            if args.save_raw_responses and len(raw_responses) < MAX_RAW_RESPONSES_TO_SAVE:
                raw_responses[player_key] = response

            if not api_success(response):
                counters["api_not_success"] += 1
                errors.append(f"{player_key}: API success flag not true")
                continue

            rows = extract_player_rows(response)
            row = pick_player_row(rows, player_key)
            if not row:
                counters["no_matching_row"] += 1
                errors.append(f"{player_key}: no matching player row in {len(rows)} rows")
                continue

            simple = simplify_player_row(row, player_key)
            cache[player_key] = simple
            fetched_details.append(simple)
            counters["fetched"] += 1

            if simple.get("player_full_name"):
                counters["fetched_with_full_name"] += 1
            else:
                counters["fetched_missing_full_name"] += 1

        except Exception as exc:
            counters["errors"] += 1
            errors.append(f"{player_key}: {exc}")

        if index < len(to_fetch) and args.sleep_seconds:
            time.sleep(max(args.sleep_seconds, 0.0))

    write_json(args.cache_path, cache)

    if args.save_raw_responses:
        write_json(RAW_RESPONSES_JSON, raw_responses)

    full_name_count = sum(1 for v in cache.values() if isinstance(v, dict) and v.get("player_full_name"))

    report = {
        "generated_at": now_utc_iso(),
        "source": "api_tennis",
        "method": "get_players",
        "raw_results_dir": str(args.raw_dir),
        "cache_path": str(args.cache_path),
        "date_filter": {"start_date": args.start_date, "end_date": args.end_date},
        "raw_files": file_counts,
        "collect_stats": collect_stats,
        "counters": dict(counters),
        "cache_total_after": len(cache),
        "cache_with_full_name_after": full_name_count,
        "errors": errors,
        "sample_fetched": fetched_details[:20],
        "outputs": {
            "cache": str(args.cache_path),
            "report": str(REPORT_JSON),
            "raw_responses": str(RAW_RESPONSES_JSON) if args.save_raw_responses else "",
        },
    }
    write_json(REPORT_JSON, report)

    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))

    if args.fail_on_any_error and errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

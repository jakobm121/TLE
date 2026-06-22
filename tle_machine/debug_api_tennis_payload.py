from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_KEY = (
    os.getenv("TENNIS_API_KEY")
    or os.getenv("API_KEY")
    or os.getenv("API_TENNIS_KEY")
    or os.getenv("APITENNIS_KEY")
    or os.getenv("API_TENNIS_API_KEY")
    or os.getenv("TENNIS_VALUE_API_KEY")
)

BASE_URL = "https://api.api-tennis.com/tennis/"
REQUEST_TIMEOUT = 45
API_SLEEP_SECONDS = 0.35

OUT_DIR = Path("data/reports/api_tennis_debug")
SURFACE_HINTS = (
    "surface",
    "court",
    "ground",
    "terrain",
    "floor",
    "venue",
    "stadium",
    "location",
    "place",
    "city",
    "country",
    "tournament",
    "competition",
    "league",
)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def safe_str(x: Any) -> str:
    return str(x or "").strip()


def api_call(params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
    if not API_KEY:
        raise RuntimeError(
            "Missing API key. Set TENNIS_API_KEY, API_KEY, API_TENNIS_KEY, "
            "APITENNIS_KEY, API_TENNIS_API_KEY, or TENNIS_VALUE_API_KEY."
        )

    p = {k: v for k, v in params.items() if v is not None}
    p["APIkey"] = API_KEY
    url = BASE_URL + "?" + urllib.parse.urlencode(p)

    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "TLE-api-debug/1.0"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return {
                    "_debug_url_without_key": BASE_URL + "?" + urllib.parse.urlencode({k: v for k, v in p.items() if k != "APIkey"}),
                    "_raw_text": raw[:5000],
                    "_json_error": True,
                }

            if isinstance(data, dict):
                data["_debug_url_without_key"] = BASE_URL + "?" + urllib.parse.urlencode({k: v for k, v in p.items() if k != "APIkey"})
                return data
            return {"_non_dict_response": data}

        except Exception as exc:
            if attempt == retries - 1:
                raise
            print(f"retry {attempt + 1}/{retries} after error: {exc}")
            time.sleep(2 * (attempt + 1))

    return {}


def iter_nested_values(obj: Any, prefix: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = safe_str(k)
            next_prefix = f"{prefix}.{key}" if prefix else key
            yield next_prefix, v
            yield from iter_nested_values(v, next_prefix)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            next_prefix = f"{prefix}[{i}]"
            yield next_prefix, v
            yield from iter_nested_values(v, next_prefix)


def extract_result(data: dict[str, Any]) -> Any:
    return data.get("result")


def flatten_items(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]

    if isinstance(result, dict):
        rows = []

        # Some endpoints return {"event_key": {...}}
        for value in result.values():
            if isinstance(value, dict):
                rows.append(value)
            elif isinstance(value, list):
                rows.extend([x for x in value if isinstance(x, dict)])

        return rows

    return []


def summarize_payload(name: str, data: dict[str, Any]) -> dict[str, Any]:
    result = extract_result(data)
    items = flatten_items(result)

    top_level_keys = sorted(data.keys())
    result_type = type(result).__name__
    item_keys = sorted({k for item in items for k in item.keys()})

    nested_key_counter = Counter()
    hint_hits = []

    for idx, item in enumerate(items):
        for key, value in iter_nested_values(item):
            nested_key_counter[key] += 1
            lk = key.lower()
            lv = safe_str(value).lower()

            if any(h in lk for h in SURFACE_HINTS) or any(h in lv for h in ("hard", "clay", "grass", "carpet", "court", "surface")):
                hint_hits.append({
                    "item_index": idx,
                    "path": key,
                    "value": value,
                    "event_key": item.get("event_key"),
                    "tournament_key": item.get("tournament_key"),
                    "tournament_name": item.get("tournament_name"),
                    "event_type_type": item.get("event_type_type"),
                    "event_first_player": item.get("event_first_player"),
                    "event_second_player": item.get("event_second_player"),
                })

    return {
        "endpoint": name,
        "generated_at": now_utc_iso(),
        "success": data.get("success"),
        "top_level_keys": top_level_keys,
        "result_type": result_type,
        "items_count": len(items),
        "item_keys": item_keys,
        "nested_keys_top_300": [
            {"path": k, "count": v}
            for k, v in nested_key_counter.most_common(300)
        ],
        "surface_hint_hits_count": len(hint_hits),
        "surface_hint_hits_first_200": hint_hits[:200],
        "sample_items_first_20": items[:20],
        "debug_url_without_key": data.get("_debug_url_without_key"),
    }


def call_and_dump(name: str, params: dict[str, Any]) -> dict[str, Any]:
    print(f"Calling {name}: {params}")
    data = api_call(params)
    time.sleep(API_SLEEP_SECONDS)

    write_json(OUT_DIR / f"raw_{name}.json", data)
    summary = summarize_payload(name, data)
    write_json(OUT_DIR / f"summary_{name}.json", summary)

    print(
        f"{name}: success={summary.get('success')} "
        f"items={summary.get('items_count')} "
        f"surface_hint_hits={summary.get('surface_hint_hits_count')}"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date YYYY-MM-DD")
    parser.add_argument("--event-key", default=None, help="Optional event_key/match_key for deeper fixture lookup")
    parser.add_argument("--tournament-key", default=None, help="Optional tournament_key for tournament-specific fixture lookup")
    parser.add_argument("--player-key", default=None, help="Optional player_key for player-specific testing")
    parser.add_argument("--include-tournaments", action="store_true", help="Also call get_tournaments")
    parser.add_argument("--include-standings", action="store_true", help="Try get_standings if available")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    summaries = []

    # Main current source used by predictions.py
    summaries.append(call_and_dump("fixtures_date", {
        "method": "get_fixtures",
        "date_start": args.date,
        "date_stop": args.date,
    }))

    # Same endpoint but with timezone, in case API enriches/changes response.
    summaries.append(call_and_dump("fixtures_date_timezone", {
        "method": "get_fixtures",
        "date_start": args.date,
        "date_stop": args.date,
        "timezone": "Europe/Ljubljana",
    }))

    # Tournament list may contain metadata not present in fixtures.
    if args.include_tournaments:
        summaries.append(call_and_dump("tournaments", {
            "method": "get_tournaments",
        }))

    # Specific tournament fixture lookup, useful when we copy tournament_key from raw_fixtures.
    if args.tournament_key:
        summaries.append(call_and_dump("fixtures_tournament", {
            "method": "get_fixtures",
            "date_start": args.date,
            "date_stop": args.date,
            "tournament_key": args.tournament_key,
        }))

    # Specific event/match lookup. Some APIs return richer details when match_key is passed.
    if args.event_key:
        summaries.append(call_and_dump("fixtures_event_key", {
            "method": "get_fixtures",
            "match_key": args.event_key,
        }))

        summaries.append(call_and_dump("odds_event_key", {
            "method": "get_odds",
            "event_key": args.event_key,
        }))

    if args.player_key:
        summaries.append(call_and_dump("fixtures_player", {
            "method": "get_fixtures",
            "date_start": args.date,
            "date_stop": args.date,
            "player_key": args.player_key,
        }))

    if args.include_standings:
        summaries.append(call_and_dump("standings", {
            "method": "get_standings",
        }))

    write_json(OUT_DIR / "index.json", {
        "generated_at": now_utc_iso(),
        "date": args.date,
        "event_key": args.event_key,
        "tournament_key": args.tournament_key,
        "player_key": args.player_key,
        "outputs_dir": str(OUT_DIR),
        "summaries": summaries,
        "next_step": (
            "Open summary_*.json and check surface_hint_hits_first_200. "
            "If hits exist, update predictions.py infer_surface fields. "
            "If no hits exist in fixtures but tournaments has useful fields, add tournament metadata cache."
        ),
    })

    print(f"Done. Wrote debug files to {OUT_DIR}")


if __name__ == "__main__":
    main()

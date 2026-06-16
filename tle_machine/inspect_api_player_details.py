from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RAW_RESULTS_DIR = Path("data/raw/api_tennis/results")
REPORT_DIR = Path("data/reports/api_tennis")
OUT_JSON = REPORT_DIR / "api_player_details_inspect.json"
OUT_RAW_JSON = REPORT_DIR / "api_player_details_raw_responses.json"
API_BASE_URL = "https://api.api-tennis.com/tennis/"

DEFAULT_PLAYER_KEYS = ["53732", "2188", "11781", "1056", "17463", "9514"]
DEFAULT_NAME_PATTERNS = ["a. blus", "a. huertas", "p. dev", "a. martin", "dar. blanch", "i. buse"]
METHODS = ["get_players", "get_player", "get_rankings"]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


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
        out = []
        for v in result.values():
            if isinstance(v, list):
                out.extend(x for x in v if isinstance(x, dict))
            elif isinstance(v, dict):
                out.append(v)
        return out
    return []


def fetch(api_key: str, method: str, player_key: str, timeout: int) -> dict[str, Any]:
    params = {"method": method, "APIkey": api_key, "player_key": player_key}
    url = API_BASE_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "tle-machine/inspect/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"_non_json": raw[:2000]}
    return decoded


def find_interesting_fixtures(date_s: str, patterns: list[str]) -> list[dict[str, Any]]:
    path = RAW_RESULTS_DIR / f"{date_s}.json"
    if not path.exists():
        return []
    rows = extract_fixtures(read_json(path))
    hits = []
    for idx, r in enumerate(rows):
        blob = json.dumps(r, ensure_ascii=False).lower()
        if any(p.lower() in blob for p in patterns):
            hits.append({"raw_index": idx, "fixture": r})
    return hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--player-keys", default=",".join(DEFAULT_PLAYER_KEYS), help="Comma-separated API player keys")
    ap.add_argument("--patterns", default=",".join(DEFAULT_NAME_PATTERNS), help="Comma-separated lowercase raw search patterns")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    date_s = args.date or datetime.now(timezone.utc).date().isoformat()
    player_keys = [x.strip() for x in args.player_keys.split(",") if x.strip()]
    patterns = [x.strip() for x in args.patterns.split(",") if x.strip()]

    api_key = os.environ.get("API_TENNIS_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing API_TENNIS_KEY environment variable / GitHub Actions secret.")

    raw_fixture_hits = find_interesting_fixtures(date_s, patterns)
    endpoint_results: dict[str, Any] = {}
    raw_responses: dict[str, Any] = {}

    for player_key in player_keys:
        endpoint_results[player_key] = {}
        raw_responses[player_key] = {}
        for method in METHODS:
            try:
                response = fetch(api_key, method, player_key, args.timeout)
                raw_responses[player_key][method] = response
                # Summarize likely name fields without hiding raw response in companion file.
                rows = extract_fixtures(response)
                fields = []
                for row in rows[:5]:
                    if isinstance(row, dict):
                        fields.append({k: v for k, v in row.items() if "player" in str(k).lower() or "name" in str(k).lower()})
                endpoint_results[player_key][method] = {
                    "ok": True,
                    "top_level_keys": list(response.keys()) if isinstance(response, dict) else [],
                    "result_count": len(rows),
                    "name_like_fields_sample": fields,
                }
            except Exception as exc:
                endpoint_results[player_key][method] = {"ok": False, "error": str(exc)}
                raw_responses[player_key][method] = {"error": str(exc)}

    report = {
        "generated_at": now_utc_iso(),
        "date": date_s,
        "purpose": "Check whether API-Tennis raw fixtures or player endpoints expose full player names for mapping.",
        "raw_fixture_hits_count": len(raw_fixture_hits),
        "raw_fixture_hits": raw_fixture_hits[:20],
        "player_keys_checked": player_keys,
        "methods_checked": METHODS,
        "endpoint_summary": endpoint_results,
        "outputs": {"summary_json": str(OUT_JSON), "raw_responses_json": str(OUT_RAW_JSON)},
    }
    write_json(OUT_JSON, report)
    write_json(OUT_RAW_JSON, raw_responses)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_BASE_URL = "https://api.api-tennis.com/tennis/"

OUT_PATH = Path("data/raw/api_tennis/metadata/get_tournaments.json")
REPORT_PATH = Path("data/reports/api_tennis/fetch_api_tournaments_report.json")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def fetch_get_tournaments(api_key: str, timeout: int) -> dict[str, Any]:
    query = urllib.parse.urlencode({
        "method": "get_tournaments",
        "APIkey": api_key,
    })
    url = f"{API_BASE_URL}?{query}"

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "tle-machine/1.0",
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected API response type: {type(data).__name__}")

    return data


def get_result_count(response: dict[str, Any]) -> int:
    result = response.get("result")
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        return len(result)
    return 0


def count_surface_rows(response: dict[str, Any]) -> dict[str, int]:
    result = response.get("result")
    if isinstance(result, dict):
        rows = list(result.values())
    elif isinstance(result, list):
        rows = result
    else:
        rows = []

    counters = {
        "rows_total": 0,
        "rows_with_surface": 0,
        "rows_without_surface": 0,
    }

    surface_fields = [
        "surface",
        "tournament_surface",
        "tournament_sourface",
        "court_surface",
        "court_type",
    ]

    for row in rows:
        if not isinstance(row, dict):
            continue
        counters["rows_total"] += 1
        has_surface = any(str(row.get(k) or "").strip() for k in surface_fields)
        if has_surface:
            counters["rows_with_surface"] += 1
        else:
            counters["rows_without_surface"] += 1

    return counters


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch API-Tennis tournaments metadata.")
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()

    api_key = os.environ.get("API_TENNIS_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing API_TENNIS_KEY environment variable.")

    response = fetch_get_tournaments(api_key, args.timeout)

    payload = {
        "schema_version": 1,
        "source": "api_tennis",
        "method": "get_tournaments",
        "fetched_at": now_utc_iso(),
        "response": response,
    }

    write_json(OUT_PATH, payload)

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "output": str(OUT_PATH),
        "response_success": response.get("success"),
        "result_count": get_result_count(response),
        "surface_counts": count_surface_rows(response),
    }

    write_json(REPORT_PATH, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen, Request

API_BASE = "https://api.api-tennis.com/tennis/"
RAW_RESULTS_DIR = Path("data/raw/api_tennis/results")
RAW_META_DIR = Path("data/raw/api_tennis/metadata")
REPORT_DIR = Path("data/reports/api_tennis")

REPORT_JSON = REPORT_DIR / "api_surface_endpoint_inspect.json"
FIELD_VALUES_CSV = REPORT_DIR / "api_surface_endpoint_field_values.csv"
TOURNAMENTS_RAW_JSON = RAW_META_DIR / "get_tournaments.json"
EVENTS_RAW_JSON = RAW_META_DIR / "get_events.json"
MATCH_DETAIL_SAMPLES_JSON = RAW_META_DIR / "get_fixtures_match_key_samples.json"

SURFACE_FIELD_TOKENS = ("surface", "court", "ground", "floor", "indoor", "outdoor")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def api_call(method: str, api_key: str, **params: str) -> dict[str, Any]:
    query = {"method": method, "APIkey": api_key}
    for k, v in params.items():
        if v is not None and str(v) != "":
            query[k] = str(v)
    url = f"{API_BASE}?{urlencode(query)}"
    req = Request(url, headers={"User-Agent": "TLE surface endpoint inspector"})
    with urlopen(req, timeout=45) as resp:
        body = resp.read().decode("utf-8")
    try:
        return json.loads(body)
    except Exception:
        return {"success": 0, "raw_text": body[:5000], "_url_method": method}


def unwrap_result(payload: Any) -> list[dict[str, Any]]:
    # Handles direct API response and our raw fetch wrapper.
    if isinstance(payload, dict) and "response" in payload and isinstance(payload["response"], dict):
        payload = payload["response"]
    if isinstance(payload, dict):
        result = payload.get("result") or payload.get("data") or payload.get("fixtures") or payload.get("events")
        if isinstance(result, list):
            return [x for x in result if isinstance(x, dict)]
        if isinstance(result, dict):
            return [result]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def collect_field_stats(rows: list[dict[str, Any]]) -> tuple[Counter, dict[str, Counter], list[str]]:
    presence = Counter()
    values: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        flat = flatten(row)
        for k, v in flat.items():
            presence[k] += 1
            if isinstance(v, (str, int, float, bool)) or v is None:
                val = "<null>" if v is None else str(v)
                if len(val) > 120:
                    val = val[:117] + "..."
                values[k][val] += 1
    surface_like = [k for k in presence if any(tok in k.lower() for tok in SURFACE_FIELD_TOKENS)]
    return presence, values, sorted(surface_like)


def write_field_values_csv(path: Path, groups: dict[str, tuple[Counter, dict[str, Counter], int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["group", "field", "presence", "coverage_pct", "value", "count"])
        writer.writeheader()
        for group, (presence, values, total) in groups.items():
            for field, count in presence.most_common():
                coverage = round((count / total * 100), 3) if total else 0
                for value, vcount in values.get(field, Counter()).most_common(20):
                    writer.writerow({
                        "group": group,
                        "field": field,
                        "presence": count,
                        "coverage_pct": coverage,
                        "value": value,
                        "count": vcount,
                    })


def get_sample_match_keys(limit: int) -> list[str]:
    keys: list[str] = []
    for path in sorted(RAW_RESULTS_DIR.glob("*.json")):
        rows = unwrap_result(read_json(path))
        for r in rows:
            status = str(r.get("event_status") or "").lower()
            winner = str(r.get("event_winner") or "").strip()
            event_type = str(r.get("event_type_type") or "").lower()
            if "doubles" in event_type or "teams" in event_type or "/" in str(r.get("event_first_player") or ""):
                continue
            if status == "finished" and winner and r.get("event_key"):
                keys.append(str(r["event_key"]))
            if len(keys) >= limit:
                return keys
    return keys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-matches", type=int, default=10)
    parser.add_argument("--sleep", type=float, default=0.25)
    args = parser.parse_args(argv)

    api_key = os.environ.get("API_TENNIS_KEY", "").strip()
    if not api_key:
        raise SystemExit("Missing API_TENNIS_KEY environment variable")

    ensure_dirs(RAW_META_DIR, REPORT_DIR)

    errors: list[dict[str, Any]] = []

    events_payload = api_call("get_events", api_key)
    write_json(EVENTS_RAW_JSON, events_payload)
    events_rows = unwrap_result(events_payload)
    time.sleep(args.sleep)

    tournaments_payload = api_call("get_tournaments", api_key)
    write_json(TOURNAMENTS_RAW_JSON, tournaments_payload)
    tournaments_rows = unwrap_result(tournaments_payload)
    time.sleep(args.sleep)

    match_samples: list[dict[str, Any]] = []
    sample_keys = get_sample_match_keys(args.sample_matches)
    for key in sample_keys:
        try:
            payload = api_call("get_fixtures", api_key, match_key=key)
            rows = unwrap_result(payload)
            match_samples.append({"match_key": key, "response": payload, "fixtures": rows})
        except Exception as e:
            errors.append({"method": "get_fixtures", "match_key": key, "error": repr(e)})
        time.sleep(args.sleep)
    write_json(MATCH_DETAIL_SAMPLES_JSON, match_samples)

    match_detail_rows: list[dict[str, Any]] = []
    for item in match_samples:
        for row in item.get("fixtures", []):
            if isinstance(row, dict):
                match_detail_rows.append(row)

    groups: dict[str, tuple[Counter, dict[str, Counter], int]] = {}
    report_groups: dict[str, Any] = {}
    for name, rows in {
        "get_events": events_rows,
        "get_tournaments": tournaments_rows,
        "get_fixtures_match_key": match_detail_rows,
    }.items():
        presence, values, surface_like = collect_field_stats(rows)
        groups[name] = (presence, values, len(rows))
        report_groups[name] = {
            "rows": len(rows),
            "surface_like_fields": [
                {
                    "field": f,
                    "presence": presence[f],
                    "coverage_pct": round((presence[f] / len(rows) * 100), 3) if rows else 0,
                    "top_values": values.get(f, Counter()).most_common(20),
                }
                for f in surface_like
            ],
            "top_fields": [
                {
                    "field": f,
                    "presence": c,
                    "coverage_pct": round((c / len(rows) * 100), 3) if rows else 0,
                }
                for f, c in presence.most_common(40)
            ],
        }

    write_field_values_csv(FIELD_VALUES_CSV, groups)

    report = {
        "generated_at": now_utc_iso(),
        "source": "api_tennis",
        "purpose": "Check whether API-Tennis exposes surface/court fields directly in metadata or match detail endpoints.",
        "raw_outputs": {
            "events": str(EVENTS_RAW_JSON),
            "tournaments": str(TOURNAMENTS_RAW_JSON),
            "match_detail_samples": str(MATCH_DETAIL_SAMPLES_JSON),
        },
        "csv_outputs": {
            "field_values": str(FIELD_VALUES_CSV),
        },
        "sample_match_keys": sample_keys,
        "groups": report_groups,
        "errors": errors,
        "conclusion_hint": "If surface_like_fields contains only event_type_type or empty lists, API does not expose usable surface directly in these endpoints.",
    }
    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

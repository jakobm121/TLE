from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from .utils import ensure_dirs, now_utc_iso, write_json
except Exception:  # pragma: no cover
    from tle_machine.utils import ensure_dirs, now_utc_iso, write_json

RAW_DIR = Path("data/raw/api_tennis/results")
REPORT_DIR = Path("data/reports/api_tennis")
SUMMARY_JSON = REPORT_DIR / "api_raw_fields_inspect.json"
FIELD_VALUES_CSV = REPORT_DIR / "api_raw_field_values.csv"
SAMPLES_JSON = REPORT_DIR / "api_raw_fixture_samples.json"
UNKNOWN_SURFACE_SAMPLES_CSV = REPORT_DIR / "api_unknown_surface_samples.csv"

SURFACE_HINTS = ("surface", "court", "ground", "type")
TOURNAMENT_HINTS = ("league", "tournament", "event", "country", "round", "season")
STATUS_HINTS = ("status", "result", "winner", "final", "finished")
PLAYER_HINTS = ("player", "first", "second", "home", "away", "winner")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def unwrap_fixtures(payload: Any) -> list[dict[str, Any]]:
    """Support wrappers from our fetcher and possible API variants."""
    obj = payload
    if isinstance(obj, dict) and "response" in obj:
        obj = obj.get("response")

    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]

    if isinstance(obj, dict):
        for key in ("result", "results", "data", "events", "fixtures"):
            val = obj.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):
                nested = unwrap_fixtures(val)
                if nested:
                    return nested

    return []


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(flatten(v, key))
            elif isinstance(v, list):
                out[key] = f"<list:{len(v)}>"
                # Keep simple scalar list preview if present
                if v and all(not isinstance(x, (dict, list)) for x in v[:5]):
                    out[key + ".sample"] = ", ".join(str(x) for x in v[:5])
            else:
                out[key] = v
    else:
        out[prefix or "value"] = obj
    return out


def norm(value: Any) -> str:
    if value is None:
        return "<null>"
    s = str(value).strip()
    if s == "":
        return "<blank>"
    return s[:200]


def field_group(field: str) -> str:
    low = field.lower()
    if any(h in low for h in SURFACE_HINTS):
        return "surface_like"
    if any(h in low for h in TOURNAMENT_HINTS):
        return "tournament_like"
    if any(h in low for h in STATUS_HINTS):
        return "status_like"
    if any(h in low for h in PLAYER_HINTS):
        return "player_like"
    return "other"


def looks_like_surface_value(v: Any) -> bool:
    s = str(v or "").strip().lower()
    return s in {"hard", "clay", "grass", "carpet", "indoor hard", "outdoor hard", "synthetic"}


def get_known_surface_fields(flat: dict[str, Any]) -> dict[str, str]:
    found = {}
    for k, v in flat.items():
        low = k.lower()
        if any(h in low for h in SURFACE_HINTS) or looks_like_surface_value(v):
            val = norm(v)
            if val not in {"<blank>", "<null>"}:
                found[k] = val
    return found


def row_value(f: dict[str, Any], *keys: str) -> str:
    for k in keys:
        if k in f and norm(f[k]) not in {"<blank>", "<null>"}:
            return norm(f[k])
    return ""


def inspect(raw_dir: Path, sample_limit: int) -> dict[str, Any]:
    ensure_dirs(REPORT_DIR)

    field_presence = Counter()
    field_values: dict[str, Counter] = defaultdict(Counter)
    field_groups = Counter()
    files = []
    samples: list[dict[str, Any]] = []
    unknown_surface_samples: list[dict[str, Any]] = []

    total = 0
    files_read = 0

    for path in sorted(raw_dir.glob("*.json")):
        payload = load_json(path)
        fixtures = unwrap_fixtures(payload)
        files_read += 1
        files.append({"path": str(path), "fixtures": len(fixtures)})

        for fixture in fixtures:
            total += 1
            flat = flatten(fixture)
            for k, v in flat.items():
                field_presence[k] += 1
                field_groups[field_group(k)] += 1
                if len(field_values[k]) < 60:
                    field_values[k][norm(v)] += 1
                else:
                    # still count already-known values
                    nv = norm(v)
                    if nv in field_values[k]:
                        field_values[k][nv] += 1

            surface_fields = get_known_surface_fields(flat)
            if len(samples) < sample_limit:
                samples.append({
                    "raw_file": str(path),
                    "surface_like_fields": surface_fields,
                    "fixture": fixture,
                })

            if not surface_fields and len(unknown_surface_samples) < 500:
                unknown_surface_samples.append({
                    "raw_file": str(path),
                    "event_key": row_value(flat, "event_key", "event_id", "id"),
                    "event_date": row_value(flat, "event_date", "date", "event_time"),
                    "event_type_type": row_value(flat, "event_type_type", "event_type", "type"),
                    "event_status": row_value(flat, "event_status", "status", "event_status_info"),
                    "event_winner": row_value(flat, "event_winner", "winner"),
                    "league_name": row_value(flat, "league_name", "tournament_name", "event_name"),
                    "event_first_player": row_value(flat, "event_first_player", "first_player", "home_team"),
                    "event_second_player": row_value(flat, "event_second_player", "second_player", "away_team"),
                    "all_keys": " | ".join(sorted(flat.keys())[:120]),
                })

    grouped_fields: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for field, count in field_presence.most_common():
        vals = field_values[field].most_common(25)
        grouped_fields[field_group(field)].append({
            "field": field,
            "presence": count,
            "coverage_pct": round((count / total * 100), 2) if total else 0,
            "top_values": vals,
        })

    summary = {
        "generated_at": now_utc_iso(),
        "raw_results_dir": str(raw_dir),
        "files_read": files_read,
        "fixtures_total": total,
        "files": files,
        "field_group_counts": dict(field_groups),
        "top_fields": [
            {"field": k, "presence": v, "coverage_pct": round((v / total * 100), 2) if total else 0}
            for k, v in field_presence.most_common(80)
        ],
        "surface_like_fields": grouped_fields.get("surface_like", []),
        "tournament_like_fields": grouped_fields.get("tournament_like", [])[:80],
        "status_like_fields": grouped_fields.get("status_like", [])[:80],
        "player_like_fields": grouped_fields.get("player_like", [])[:80],
        "outputs": {
            "summary_json": str(SUMMARY_JSON),
            "field_values_csv": str(FIELD_VALUES_CSV),
            "samples_json": str(SAMPLES_JSON),
            "unknown_surface_samples_csv": str(UNKNOWN_SURFACE_SAMPLES_CSV),
        },
    }

    write_json(SUMMARY_JSON, summary)
    write_json(SAMPLES_JSON, {"generated_at": now_utc_iso(), "samples": samples})

    with FIELD_VALUES_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["group", "field", "presence", "coverage_pct", "value", "count"])
        for field, count in field_presence.most_common():
            cov = round((count / total * 100), 2) if total else 0
            for value, vc in field_values[field].most_common(50):
                writer.writerow([field_group(field), field, count, cov, value, vc])

    with UNKNOWN_SURFACE_SAMPLES_CSV.open("w", encoding="utf-8", newline="") as fh:
        fieldnames = [
            "raw_file", "event_key", "event_date", "event_type_type", "event_status",
            "event_winner", "league_name", "event_first_player", "event_second_player", "all_keys",
        ]
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in unknown_surface_samples:
            writer.writerow(row)

    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args(argv)
    summary = inspect(Path(args.raw_dir), args.sample_limit)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

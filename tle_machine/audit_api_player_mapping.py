from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .utils import now_utc_iso, write_json
except Exception:
    def now_utc_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

API_SOURCE_DIR = Path("data/source/api_tennis")
MAPPING_JSON = Path("data/metadata/api_tennis/player_mapping.json")
REPORT_DIR = Path("data/reports/api_tennis")
AUDIT_JSON = REPORT_DIR / "player_mapping_audit.json"
ISSUES_CSV = REPORT_DIR / "player_mapping_audit_issues.csv"
MATCH_COVERAGE_CSV = REPORT_DIR / "player_mapping_match_coverage.csv"
UNMAPPED_IMPACT_CSV = REPORT_DIR / "player_mapping_unmapped_impact.csv"

VALID_STATUSES = {"auto_mapped", "manual_mapped", "manual_unmapped", "manual_invalid_target", "ambiguous", "unmapped"}
ACCEPTED_STATUSES = {"auto_mapped", "manual_mapped"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def player_obj(match: dict[str, Any], side: str) -> dict[str, Any]:
    obj = match.get(side) or {}
    return obj if isinstance(obj, dict) else {}


def player_key_name(match: dict[str, Any], side: str) -> tuple[str, str]:
    obj = player_obj(match, side)
    key = str(obj.get("player_key") or obj.get("api_player_key") or obj.get("key") or "")
    name = str(obj.get("name") or obj.get("player_name") or "")
    return key, name


def mapping_entry(mapping: dict[str, Any], api_key: str) -> dict[str, Any] | None:
    entry = mapping.get(api_key)
    if isinstance(entry, dict):
        return entry
    return None


def is_mapped(mapping: dict[str, Any], api_key: str) -> bool:
    e = mapping_entry(mapping, api_key)
    if not e:
        return False
    return e.get("status") in ACCEPTED_STATUSES and bool(e.get("sackmann_player_key"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args(argv)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    issues: list[dict[str, Any]] = []
    match_rows: list[dict[str, Any]] = []
    counters = Counter()
    unmapped_impact: dict[str, dict[str, Any]] = {}
    reverse: dict[str, list[str]] = defaultdict(list)

    if not MAPPING_JSON.exists():
        report = {
            "generated_at": now_utc_iso(),
            "status": "error",
            "issues_total": 1,
            "error": f"Missing mapping file: {MAPPING_JSON}",
        }
        write_json(AUDIT_JSON, report)
        write_csv(ISSUES_CSV, [{"severity": "error", "issue": "missing_mapping_json", "api_player_key": "", "detail": str(MAPPING_JSON)}], ["severity", "issue", "api_player_key", "detail"])
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        raise SystemExit(1)

    data = read_json(MAPPING_JSON)
    mapping = data.get("mapping") if isinstance(data, dict) else None
    if not isinstance(mapping, dict):
        report = {
            "generated_at": now_utc_iso(),
            "status": "error",
            "issues_total": 1,
            "error": "player_mapping.json does not contain mapping object",
        }
        write_json(AUDIT_JSON, report)
        write_csv(ISSUES_CSV, [{"severity": "error", "issue": "invalid_mapping_json", "api_player_key": "", "detail": "missing mapping object"}], ["severity", "issue", "api_player_key", "detail"])
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        raise SystemExit(1)

    counters["mapping_entries"] = len(mapping)

    for api_key, e in sorted(mapping.items()):
        if not isinstance(e, dict):
            issues.append({"severity": "error", "issue": "mapping_entry_not_object", "api_player_key": api_key, "detail": type(e).__name__})
            continue
        status = str(e.get("status") or "")
        gender = str(e.get("gender") or "")
        target = e.get("sackmann_player_key")
        counters[f"status_{status or 'blank'}"] += 1
        counters[f"gender_{gender or 'blank'}"] += 1

        if status not in VALID_STATUSES:
            issues.append({"severity": "error", "issue": "invalid_status", "api_player_key": api_key, "detail": status})
        if gender not in {"men", "women"}:
            issues.append({"severity": "error", "issue": "invalid_gender", "api_player_key": api_key, "detail": gender})
        if status in ACCEPTED_STATUSES and not target:
            issues.append({"severity": "error", "issue": "accepted_mapping_without_target", "api_player_key": api_key, "detail": status})
        if status not in ACCEPTED_STATUSES and target:
            issues.append({"severity": "warning", "issue": "nonaccepted_mapping_has_target", "api_player_key": api_key, "detail": f"{status}->{target}"})
        if target:
            t = str(target)
            reverse[t].append(api_key)
            target_gender = t.split(":", 1)[0] if ":" in t else ""
            if target_gender and gender and target_gender != gender:
                issues.append({"severity": "error", "issue": "target_gender_mismatch", "api_player_key": api_key, "detail": f"api_gender={gender}, target={t}"})

    # Many API aliases to same Sackmann target are not automatically wrong, but should be visible.
    for sack_key, api_keys in sorted(reverse.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(api_keys) > 3:
            issues.append({"severity": "warning", "issue": "many_api_aliases_for_one_sackmann_player", "api_player_key": ";".join(api_keys), "detail": sack_key})
            counters["warning_many_api_aliases"] += 1

    files = sorted(API_SOURCE_DIR.glob("tle_api_matches_*.jsonl.gz"))
    counters["api_source_files"] = len(files)

    for path in files:
        for m in read_jsonl_gz(path):
            counters["api_matches_checked"] += 1
            level = str(m.get("level") or "unknown")
            surface = str(m.get("surface") or "unknown")
            gender = str(m.get("gender") or "")
            date = str(m.get("date") or "")
            tournament = str(m.get("tourney_name") or m.get("tournament_name") or "")
            match_id = str(m.get("match_id") or m.get("api_event_key") or m.get("event_key") or "")
            wk, wn = player_key_name(m, "winner")
            lk, ln = player_key_name(m, "loser")
            wm = is_mapped(mapping, wk)
            lm = is_mapped(mapping, lk)

            if wm and lm:
                coverage = "both_mapped"
            elif wm or lm:
                coverage = "one_mapped"
            else:
                coverage = "none_mapped"
            counters[f"match_coverage_{coverage}"] += 1
            counters[f"match_coverage_{coverage}_{level}"] += 1
            counters[f"level_{level}"] += 1
            counters[f"surface_{surface}"] += 1
            counters[f"gender_{gender}"] += 1

            match_rows.append({
                "match_id": match_id,
                "date": date,
                "gender": gender,
                "level": level,
                "surface": surface,
                "tournament": tournament,
                "winner_api_key": wk,
                "winner_name": wn,
                "winner_mapping_status": (mapping_entry(mapping, wk) or {}).get("status", "missing_mapping_entry"),
                "winner_sackmann_key": (mapping_entry(mapping, wk) or {}).get("sackmann_player_key", ""),
                "loser_api_key": lk,
                "loser_name": ln,
                "loser_mapping_status": (mapping_entry(mapping, lk) or {}).get("status", "missing_mapping_entry"),
                "loser_sackmann_key": (mapping_entry(mapping, lk) or {}).get("sackmann_player_key", ""),
                "coverage": coverage,
            })

            for key, name, mapped in ((wk, wn, wm), (lk, ln, lm)):
                if mapped:
                    continue
                if not key:
                    continue
                ent = unmapped_impact.setdefault(key, {
                    "api_player_key": key,
                    "api_name": name,
                    "gender": gender,
                    "mapping_status": (mapping_entry(mapping, key) or {}).get("status", "missing_mapping_entry"),
                    "api_matches_in_source": 0,
                    "levels": Counter(),
                    "surfaces": Counter(),
                    "sample_tournaments": Counter(),
                })
                ent["api_matches_in_source"] += 1
                ent["levels"][level] += 1
                ent["surfaces"][surface] += 1
                ent["sample_tournaments"][tournament] += 1

    # Missing mapping entries for source players are real errors for merge readiness.
    source_api_keys = set()
    for r in match_rows:
        source_api_keys.add(r["winner_api_key"])
        source_api_keys.add(r["loser_api_key"])
    for api_key in sorted(k for k in source_api_keys if k and k not in mapping):
        issues.append({"severity": "error", "issue": "source_player_missing_mapping_entry", "api_player_key": api_key, "detail": "appears in API source but not player_mapping.json"})

    match_rows.sort(key=lambda r: (r["coverage"], r["level"], r["date"], r["match_id"]))

    impact_rows = []
    for key, ent in unmapped_impact.items():
        impact_rows.append({
            "api_player_key": ent["api_player_key"],
            "api_name": ent["api_name"],
            "gender": ent["gender"],
            "mapping_status": ent["mapping_status"],
            "api_matches_in_source": ent["api_matches_in_source"],
            "levels": json.dumps(dict(ent["levels"]), ensure_ascii=False, sort_keys=True),
            "surfaces": json.dumps(dict(ent["surfaces"]), ensure_ascii=False, sort_keys=True),
            "sample_tournaments": json.dumps(dict(ent["sample_tournaments"].most_common(5)), ensure_ascii=False),
        })
    impact_rows.sort(key=lambda r: (-int(r["api_matches_in_source"]), r["api_name"]))

    errors_total = sum(1 for i in issues if i.get("severity") == "error")
    warnings_total = sum(1 for i in issues if i.get("severity") == "warning")

    checked = counters["api_matches_checked"]
    both = counters["match_coverage_both_mapped"]
    one = counters["match_coverage_one_mapped"]
    none = counters["match_coverage_none_mapped"]

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok" if errors_total == 0 else "error",
        "mapping_path": str(MAPPING_JSON),
        "api_source_dir": str(API_SOURCE_DIR),
        "counters": dict(counters),
        "api_matches_checked": checked,
        "match_mapping_coverage": {
            "both_mapped": both,
            "one_mapped": one,
            "none_mapped": none,
            "both_mapped_pct": round(100.0 * both / checked, 3) if checked else 0.0,
            "one_mapped_pct": round(100.0 * one / checked, 3) if checked else 0.0,
            "none_mapped_pct": round(100.0 * none / checked, 3) if checked else 0.0,
        },
        "unmapped_players_with_match_impact": len(impact_rows),
        "issues_total": len(issues),
        "errors_total": errors_total,
        "warnings_total": warnings_total,
        "outputs": {
            "audit_json": str(AUDIT_JSON),
            "issues_csv": str(ISSUES_CSV),
            "match_coverage_csv": str(MATCH_COVERAGE_CSV),
            "unmapped_impact_csv": str(UNMAPPED_IMPACT_CSV),
        },
        "notes": [
            "Only auto_mapped/manual_mapped statuses count as mapped.",
            "one_mapped/none_mapped matches should not be merged into canonical until mapping is improved or accepted policy is defined.",
        ],
    }

    write_json(AUDIT_JSON, report)
    write_csv(ISSUES_CSV, issues, ["severity", "issue", "api_player_key", "detail"])
    write_csv(MATCH_COVERAGE_CSV, match_rows, [
        "match_id", "date", "gender", "level", "surface", "tournament",
        "winner_api_key", "winner_name", "winner_mapping_status", "winner_sackmann_key",
        "loser_api_key", "loser_name", "loser_mapping_status", "loser_sackmann_key",
        "coverage",
    ])
    write_csv(UNMAPPED_IMPACT_CSV, impact_rows, [
        "api_player_key", "api_name", "gender", "mapping_status", "api_matches_in_source",
        "levels", "surfaces", "sample_tournaments",
    ])

    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    if errors_total:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

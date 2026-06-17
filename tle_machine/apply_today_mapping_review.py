from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OVERRIDES_JSON = Path("data/metadata/api_tennis/player_mapping_overrides.json")
REVIEW_CSV = Path("data/reports/api_tennis/today_mapping_review.csv")
REPORT_JSON = Path("data/reports/api_tennis/apply_today_mapping_review_report.json")
APPLIED_CSV = Path("data/reports/api_tennis/apply_today_mapping_review_applied.csv")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def truthy(value: Any) -> bool:
    s = str(value or "").strip().lower()
    return s in {"1", "true", "yes", "y", "x", "reject", "manual_unmapped", "unmapped"}


def clean(value: Any) -> str:
    return str(value or "").strip()


def valid_api_key(value: str) -> bool:
    return value.startswith("men:api:") or value.startswith("women:api:")


def valid_sackmann_key(value: str) -> bool:
    return value.startswith("men:sackmann:") or value.startswith("women:sackmann:")


def read_review(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing review CSV: {path}")

    with path.open("r", encoding="utf-8", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def choose_target(row: dict[str, str]) -> tuple[str, str]:
    manual = clean(row.get("manual_sackmann_key"))
    if manual:
        return manual, "manual_sackmann_key"

    rank_s = clean(row.get("accept_candidate_rank"))
    if not rank_s:
        return "", ""

    try:
        rank = int(rank_s)
    except Exception:
        return "", f"invalid_rank:{rank_s}"

    if rank not in {1, 2, 3}:
        return "", f"invalid_rank:{rank_s}"

    target = clean(row.get(f"candidate_{rank}_key"))
    return target, f"candidate_{rank}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-csv", type=Path, default=REVIEW_CSV)
    parser.add_argument("--overrides-json", type=Path, default=OVERRIDES_JSON)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    rows = read_review(args.review_csv)
    overrides = read_json(args.overrides_json, {})
    if not isinstance(overrides, dict):
        overrides = {}

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    changed = 0

    for row in rows:
        api_key = clean(row.get("api_player_key"))
        if not api_key:
            skipped.append({"api_player_key": "", "action": "skip", "reason": "missing_api_player_key"})
            continue
        if not valid_api_key(api_key):
            skipped.append({"api_player_key": api_key, "action": "skip", "reason": "invalid_api_player_key"})
            continue

        # Optional reject/manual-unmapped. Use this only when you are sure the API player
        # should not be mapped to Sackmann from the current candidate set.
        if truthy(row.get("reject")):
            old = overrides.get(api_key, "__missing__")
            overrides[api_key] = None
            applied.append({
                "api_player_key": api_key,
                "api_name": row.get("api_name", ""),
                "action": "manual_unmapped",
                "target": "",
                "old_value": old,
                "source": "reject",
                "review_note": row.get("review_note", ""),
            })
            changed += 1
            continue

        target, source = choose_target(row)
        if not target:
            skipped.append({
                "api_player_key": api_key,
                "api_name": row.get("api_name", ""),
                "action": "skip",
                "reason": "no_accept_candidate_rank_or_manual_sackmann_key",
            })
            continue

        if source.startswith("invalid_rank"):
            skipped.append({
                "api_player_key": api_key,
                "api_name": row.get("api_name", ""),
                "action": "skip",
                "reason": source,
            })
            continue

        if not valid_sackmann_key(target):
            skipped.append({
                "api_player_key": api_key,
                "api_name": row.get("api_name", ""),
                "action": "skip",
                "reason": f"invalid_sackmann_key:{target}",
            })
            continue

        old = overrides.get(api_key, "__missing__")
        overrides[api_key] = target
        applied.append({
            "api_player_key": api_key,
            "api_name": row.get("api_name", ""),
            "action": "manual_mapped",
            "target": target,
            "old_value": old,
            "source": source,
            "candidate_name": row.get(source.replace("candidate_", "candidate_") + "_name", "") if source.startswith("candidate_") else "",
            "opponent_name": row.get("opponent_name", ""),
            "tournament": row.get("tournament", ""),
            "review_note": row.get("review_note", ""),
        })
        changed += 1

    if not args.dry_run:
        write_json(args.overrides_json, overrides)

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "dry_run": args.dry_run,
        "inputs": {
            "review_csv": str(args.review_csv),
            "overrides_json": str(args.overrides_json),
        },
        "outputs": {
            "report_json": str(REPORT_JSON),
            "applied_csv": str(APPLIED_CSV),
        },
        "counts": {
            "review_rows": len(rows),
            "applied": len(applied),
            "skipped": len(skipped),
            "overrides_total_after": len(overrides),
            "changed": changed,
        },
        "notes": [
            "Fill accept_candidate_rank with 1, 2, or 3 to apply one of the candidate columns.",
            "Fill manual_sackmann_key to override candidates.",
            "Set reject=true only for deliberate manual_unmapped entries.",
        ],
    }

    write_json(REPORT_JSON, report)
    write_csv(
        APPLIED_CSV,
        applied + skipped,
        ["api_player_key", "api_name", "action", "target", "old_value", "source", "candidate_name", "opponent_name", "tournament", "review_note", "reason"],
    )

    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

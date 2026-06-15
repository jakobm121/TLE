from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_RATINGS_PATHS = [
    Path("data/ratings/tle_player_ratings.json.gz"),
    Path("data/ratings/tle_player_ratings.json"),
]
REPORT_DIR = Path("data/reports/ratings")
AUDIT_JSON = REPORT_DIR / "rating_integrity_audit.json"
ISSUES_CSV = REPORT_DIR / "rating_integrity_issues.csv"

VALID_GENDERS = {"men", "women"}
VALID_SURFACES = {"hard", "clay", "grass", "carpet", "unknown"}
VALID_LEVELS = {"atp_wta", "grand_slam", "challenger", "itf", "qualifying"}
MIN_REASONABLE_RATING = 700.0
MAX_REASONABLE_RATING = 2800.0


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json_maybe_gz(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def add_issue(issues: list[dict[str, Any]], player_key: str, player: dict[str, Any], issue: str, detail: str) -> None:
    issues.append(
        {
            "player_key": player_key,
            "name": player.get("name", "") if isinstance(player, dict) else "",
            "gender": player.get("gender", "") if isinstance(player, dict) else "",
            "issue": issue,
            "detail": detail,
        }
    )


def audit_rating_value(
    *,
    issues: list[dict[str, Any]],
    player_key: str,
    player: dict[str, Any],
    layer: str,
    rating_key: str,
    value: Any,
) -> None:
    if not is_number(value):
        add_issue(issues, player_key, player, "invalid_rating_value", f"{layer}.{rating_key}={value!r}")
        return
    value_f = float(value)
    if value_f < MIN_REASONABLE_RATING or value_f > MAX_REASONABLE_RATING:
        add_issue(
            issues,
            player_key,
            player,
            "rating_out_of_reasonable_range",
            f"{layer}.{rating_key}={value_f}",
        )


def audit_match_count(
    *,
    issues: list[dict[str, Any]],
    player_key: str,
    player: dict[str, Any],
    layer: str,
    count_key: str,
    value: Any,
) -> None:
    if not is_non_negative_int(value):
        add_issue(issues, player_key, player, "invalid_match_count", f"{layer}.{count_key}={value!r}")


def get_players(data: Any) -> dict[str, Any]:
    if isinstance(data, dict) and isinstance(data.get("players"), dict):
        return data["players"]
    if isinstance(data, dict):
        # Backward compatible: top-level dict may already be players.
        maybe_players = {k: v for k, v in data.items() if isinstance(v, dict) and "overall" in v}
        if maybe_players:
            return maybe_players
    raise ValueError("Ratings JSON must contain a top-level 'players' object")


def write_issues_csv(path: Path, issues: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["player_key", "name", "gender", "issue", "detail"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in issues:
            writer.writerow(row)


def audit_player(player_key: str, player: Any, issues: list[dict[str, Any]], counters: Counter) -> None:
    if not isinstance(player, dict):
        add_issue(issues, player_key, {}, "invalid_player_object", f"type={type(player).__name__}")
        return

    gender = player.get("gender")
    if gender not in VALID_GENDERS:
        add_issue(issues, player_key, player, "invalid_gender", f"gender={gender!r}")
    else:
        counters[f"players_{gender}"] += 1
        if not player_key.startswith(f"{gender}:"):
            add_issue(issues, player_key, player, "player_key_gender_mismatch", f"gender={gender!r}")

    name = player.get("name")
    if not isinstance(name, str) or not name.strip():
        add_issue(issues, player_key, player, "missing_name", f"name={name!r}")

    matches = player.get("matches")
    audit_match_count(issues=issues, player_key=player_key, player=player, layer="root", count_key="matches", value=matches)
    if matches == 0:
        add_issue(issues, player_key, player, "zero_matches", "matches=0")

    overall = player.get("overall")
    audit_rating_value(issues=issues, player_key=player_key, player=player, layer="root", rating_key="overall", value=overall)

    layer_pairs = [
        ("surface", "surface_matches", VALID_SURFACES),
        ("level", "level_matches", VALID_LEVELS),
        ("level_surface", "level_surface_matches", None),
    ]

    for rating_layer, count_layer, valid_keys in layer_pairs:
        ratings = player.get(rating_layer)
        counts = player.get(count_layer)

        if not isinstance(ratings, dict):
            add_issue(issues, player_key, player, "missing_or_invalid_layer", f"{rating_layer} is {type(ratings).__name__}")
            continue
        if not isinstance(counts, dict):
            add_issue(issues, player_key, player, "missing_or_invalid_layer", f"{count_layer} is {type(counts).__name__}")
            continue

        if ratings:
            counters[f"players_with_{rating_layer}"] += 1

        for key, value in ratings.items():
            if valid_keys is not None and key not in valid_keys:
                add_issue(issues, player_key, player, "invalid_layer_key", f"{rating_layer}.{key}")
            if rating_layer == "level_surface":
                if "|" not in key:
                    add_issue(issues, player_key, player, "invalid_level_surface_key", key)
                else:
                    level, surface = key.split("|", 1)
                    if level not in VALID_LEVELS:
                        add_issue(issues, player_key, player, "invalid_level_surface_level", key)
                    if surface not in VALID_SURFACES:
                        add_issue(issues, player_key, player, "invalid_level_surface_surface", key)

            audit_rating_value(
                issues=issues,
                player_key=player_key,
                player=player,
                layer=rating_layer,
                rating_key=key,
                value=value,
            )
            if key not in counts:
                add_issue(issues, player_key, player, "missing_matching_count", f"{rating_layer}.{key} has no {count_layer}.{key}")

        for key, value in counts.items():
            audit_match_count(
                issues=issues,
                player_key=player_key,
                player=player,
                layer=count_layer,
                count_key=key,
                value=value,
            )
            if key not in ratings:
                add_issue(issues, player_key, player, "count_without_rating", f"{count_layer}.{key} has no {rating_layer}.{key}")

    if is_non_negative_int(matches):
        for count_layer in ["surface_matches", "level_matches", "level_surface_matches"]:
            counts = player.get(count_layer)
            if isinstance(counts, dict):
                total = sum(v for v in counts.values() if isinstance(v, int) and not isinstance(v, bool))
                if total != matches:
                    add_issue(issues, player_key, player, "match_count_sum_mismatch", f"sum({count_layer})={total}, matches={matches}")


def find_default_ratings_path() -> Path:
    for path in DEFAULT_RATINGS_PATHS:
        if path.exists():
            return path
    raise FileNotFoundError("Could not find data/ratings/tle_player_ratings.json.gz or data/ratings/tle_player_ratings.json")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ratings-path", type=Path, default=None)
    parser.add_argument("--audit-json", type=Path, default=AUDIT_JSON)
    parser.add_argument("--issues-csv", type=Path, default=ISSUES_CSV)
    args = parser.parse_args(argv)

    ratings_path = args.ratings_path or find_default_ratings_path()
    data = read_json_maybe_gz(ratings_path)
    players = get_players(data)

    issues: list[dict[str, Any]] = []
    counters = Counter()
    counters["players_checked"] = len(players)

    for player_key, player in players.items():
        audit_player(player_key, player, issues, counters)

    issue_counts = Counter(row["issue"] for row in issues)

    audit = {
        "generated_at": now_utc_iso(),
        "status": "ok" if not issues else "failed",
        "ratings_path": str(ratings_path),
        "players_checked": len(players),
        "issues_total": len(issues),
        "issue_counts": dict(sorted(issue_counts.items())),
        "counters": dict(sorted(counters.items())),
        "rating_range_policy": {
            "min_reasonable_rating": MIN_REASONABLE_RATING,
            "max_reasonable_rating": MAX_REASONABLE_RATING,
        },
        "outputs": {
            "audit_json": str(args.audit_json),
            "issues_csv": str(args.issues_csv),
        },
    }

    write_json(args.audit_json, audit)
    write_issues_csv(args.issues_csv, issues)

    print(json.dumps(audit, indent=2, ensure_ascii=False, sort_keys=True))

    if issues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

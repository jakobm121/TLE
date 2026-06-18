from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import CANONICAL_DIR, SOURCE_SACKMANN_DIR
from .utils import ensure_dirs, iter_jsonl_gz, now_utc_iso, read_json, write_json, write_jsonl_gz


PLAYER_ALIASES_PATH = Path("data/metadata/sackmann/player_aliases.json")


def load_player_aliases() -> dict[str, str]:
    data = read_json(PLAYER_ALIASES_PATH, {})
    if not isinstance(data, dict):
        return {}

    aliases: dict[str, str] = {}
    for k, v in data.items():
        kk = str(k).strip()
        vv = str(v).strip()
        if not kk or not vv or kk == vv:
            continue
        if not kk.startswith(("men:sackmann:", "women:sackmann:")):
            continue
        if not vv.startswith(("men:sackmann:", "women:sackmann:")):
            continue
        if kk.split(":", 1)[0] != vv.split(":", 1)[0]:
            continue
        aliases[kk] = vv

    return aliases


def first_nonempty(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def nested_key(row: dict[str, Any], side: str) -> str:
    obj = row.get(side)
    if isinstance(obj, dict):
        return str(
            obj.get("player_key")
            or obj.get("key")
            or obj.get("canonical_key")
            or ""
        ).strip()
    return ""


def get_player_key(row: dict[str, Any], side: str) -> str:
    if side == "winner":
        return (
            first_nonempty(row, ["winner_player_key", "winner_key", "winner_canonical_key"])
            or nested_key(row, "winner")
        )
    return (
        first_nonempty(row, ["loser_player_key", "loser_key", "loser_canonical_key"])
        or nested_key(row, "loser")
    )


def set_player_key(row: dict[str, Any], side: str, value: str) -> None:
    if side == "winner":
        row["winner_player_key"] = value
        if "winner_key" in row:
            row["winner_key"] = value
        if "winner_canonical_key" in row:
            row["winner_canonical_key"] = value
    else:
        row["loser_player_key"] = value
        if "loser_key" in row:
            row["loser_key"] = value
        if "loser_canonical_key" in row:
            row["loser_canonical_key"] = value

    obj = row.get(side)
    if isinstance(obj, dict):
        obj["player_key"] = value


def resolve_alias(player_key: str, aliases: dict[str, str], counters: Counter) -> str:
    original = str(player_key or "").strip()
    if not original:
        return original

    seen: set[str] = set()
    current = original

    while current in aliases:
        if current in seen:
            counters["alias_cycle_detected"] += 1
            return current
        seen.add(current)
        current = aliases[current]

    if current != original:
        counters["alias_resolved_player_keys"] += 1

    return current


def apply_aliases_to_row(row: dict[str, Any], aliases: dict[str, str], counters: Counter) -> dict[str, Any]:
    out = dict(row)

    # Copy nested dicts so we do not mutate source row accidentally.
    if isinstance(row.get("winner"), dict):
        out["winner"] = dict(row["winner"])
    if isinstance(row.get("loser"), dict):
        out["loser"] = dict(row["loser"])

    winner_old = get_player_key(out, "winner")
    loser_old = get_player_key(out, "loser")

    if not winner_old:
        counters["missing_winner_player_key"] += 1
    if not loser_old:
        counters["missing_loser_player_key"] += 1

    winner_new = resolve_alias(winner_old, aliases, counters)
    loser_new = resolve_alias(loser_old, aliases, counters)

    changed = False

    if winner_old:
        if winner_new != winner_old:
            out["winner_player_key_original"] = winner_old
            changed = True
        set_player_key(out, "winner", winner_new)

    if loser_old:
        if loser_new != loser_old:
            out["loser_player_key_original"] = loser_old
            changed = True
        set_player_key(out, "loser", loser_new)

    if changed:
        counters["alias_resolved_matches"] += 1

    return out


def match_dedupe_key(row: dict[str, Any]) -> tuple[Any, ...]:
    winner_key = get_player_key(row, "winner")
    loser_key = get_player_key(row, "loser")

    return (
        row.get("date") or row.get("match_date"),
        row.get("gender"),
        row.get("level"),
        row.get("surface"),
        row.get("tourney_id"),
        row.get("tourney_name") or row.get("tournament") or row.get("event_name"),
        row.get("round"),
        winner_key,
        loser_key,
        row.get("score") or "",
    )


def row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("date") or row.get("match_date") or "",
        row.get("match_id") or row.get("source_match_id") or "",
    )


def main() -> None:
    ensure_dirs(CANONICAL_DIR)

    aliases = load_player_aliases()
    manifest = read_json(SOURCE_SACKMANN_DIR / "manifest.json", {})

    by_year: dict[int, list[dict[str, Any]]] = defaultdict(list)
    seen_match_keys: set[tuple[Any, ...]] = set()
    counters: Counter = Counter()
    counters["player_aliases_loaded"] = len(aliases)

    for yf in manifest.get("year_files", []):
        path = SOURCE_SACKMANN_DIR / str(yf["path"]).split("data/source/sackmann/")[-1]
        if not path.exists():
            path = Path(yf["path"])

        for row in iter_jsonl_gz(path):
            counters["source_rows_seen"] += 1

            row2 = apply_aliases_to_row(row, aliases, counters)

            winner_key = get_player_key(row2, "winner")
            loser_key = get_player_key(row2, "loser")

            if winner_key and loser_key and winner_key == loser_key:
                counters["winner_loser_same_after_alias_skipped"] += 1
                continue

            if not winner_key or not loser_key:
                counters["missing_player_key_rows_skipped"] += 1
                continue

            dedupe_key = match_dedupe_key(row2)
            if dedupe_key in seen_match_keys:
                counters["duplicate_after_alias_skipped"] += 1
                continue

            seen_match_keys.add(dedupe_key)

            year = int(row2.get("year") or str(row2.get("date") or row2.get("match_date") or "")[:4])
            by_year[year].append(row2)

            counters["canonical_matches"] += 1
            counters[f"source_{row2.get('source', 'unknown')}"] += 1
            counters[f"level_{row2.get('level', 'unknown')}"] += 1
            counters[f"surface_{row2.get('surface', 'unknown')}"] += 1
            counters[f"gender_{row2.get('gender', 'unknown')}"] += 1

    year_files = []
    for year, rows in sorted(by_year.items()):
        rows.sort(key=row_sort_key)

        path = CANONICAL_DIR / f"tle_matches_{year}.jsonl.gz"
        count = write_jsonl_gz(path, rows)

        year_files.append(
            {
                "year": year,
                "path": str(path),
                "matches": count,
                "created_at": now_utc_iso(),
            }
        )

    out = {
        "generated_at": now_utc_iso(),
        "canonical_matches": counters["canonical_matches"],
        "year_files": year_files,
        "counters": dict(counters),
        "sources": {"sackmann": counters.get("source_sackmann", 0)},
        "player_aliases": {
            "path": str(PLAYER_ALIASES_PATH),
            "loaded": len(aliases),
        },
    }

    write_json(CANONICAL_DIR / "manifest.json", out)
    print(out)


if __name__ == "__main__":
    main()

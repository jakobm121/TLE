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


def get_winner_key(row: dict[str, Any]) -> str:
    return first_nonempty(row, ["winner_player_key", "winner_key", "winner_canonical_key"])


def get_loser_key(row: dict[str, Any]) -> str:
    return first_nonempty(row, ["loser_player_key", "loser_key", "loser_canonical_key"])


def set_winner_key(row: dict[str, Any], value: str) -> None:
    # Keep the pipeline's standard field, but also update fallback fields if they exist.
    row["winner_player_key"] = value
    if "winner_key" in row:
        row["winner_key"] = value
    if "winner_canonical_key" in row:
        row["winner_canonical_key"] = value


def set_loser_key(row: dict[str, Any], value: str) -> None:
    row["loser_player_key"] = value
    if "loser_key" in row:
        row["loser_key"] = value
    if "loser_canonical_key" in row:
        row["loser_canonical_key"] = value


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

    winner_old = get_winner_key(out)
    loser_old = get_loser_key(out)

    if not winner_old:
        counters["missing_winner_player_key"] += 1
    if not loser_old:
        counters["missing_loser_player_key"] += 1

    winner_new = resolve_alias(winner_old, aliases, counters)
    loser_new = resolve_alias(loser_old, aliases, counters)

    changed = False

    if winner_old and winner_new != winner_old:
        out["winner_player_key_original"] = winner_old
        set_winner_key(out, winner_new)
        changed = True
    elif winner_old:
        set_winner_key(out, winner_old)

    if loser_old and loser_new != loser_old:
        out["loser_player_key_original"] = loser_old
        set_loser_key(out, loser_new)
        changed = True
    elif loser_old:
        set_loser_key(out, loser_old)

    if changed:
        counters["alias_resolved_matches"] += 1

    return out


def match_dedupe_key(row: dict[str, Any]) -> tuple[Any, ...]:
    winner_key = get_winner_key(row)
    loser_key = get_loser_key(row)

    return (
        row.get("date") or row.get("match_date"),
        row.get("gender"),
        row.get("level"),
        row.get("surface"),
        row.get("tournament") or row.get("event_name") or row.get("tourney_name"),
        row.get("round"),
        winner_key,
        loser_key,
        row.get("score"),
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

            winner_key = get_winner_key(row2)
            loser_key = get_loser_key(row2)

            # Only skip winner==loser when both keys are present. The previous patch
            # incorrectly skipped rows where both were missing/empty.
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

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

    winner_old = str(out.get("winner_player_key") or "").strip()
    loser_old = str(out.get("loser_player_key") or "").strip()

    winner_new = resolve_alias(winner_old, aliases, counters)
    loser_new = resolve_alias(loser_old, aliases, counters)

    changed = False

    if winner_new != winner_old:
        out["winner_player_key_original"] = winner_old
        out["winner_player_key"] = winner_new
        changed = True

    if loser_new != loser_old:
        out["loser_player_key_original"] = loser_old
        out["loser_player_key"] = loser_new
        changed = True

    if changed:
        counters["alias_resolved_matches"] += 1

    return out


def match_dedupe_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("date"),
        row.get("gender"),
        row.get("level"),
        row.get("surface"),
        row.get("tournament") or row.get("event_name") or row.get("tourney_name"),
        row.get("round"),
        row.get("winner_player_key"),
        row.get("loser_player_key"),
        row.get("score"),
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

            if row2.get("winner_player_key") == row2.get("loser_player_key"):
                counters["winner_loser_same_after_alias_skipped"] += 1
                continue

            dedupe_key = match_dedupe_key(row2)
            if dedupe_key in seen_match_keys:
                counters["duplicate_after_alias_skipped"] += 1
                continue

            seen_match_keys.add(dedupe_key)

            by_year[int(row2["year"])].append(row2)

            counters["canonical_matches"] += 1
            counters[f"source_{row2.get('source', 'unknown')}"] += 1
            counters[f"level_{row2.get('level', 'unknown')}"] += 1
            counters[f"surface_{row2.get('surface', 'unknown')}"] += 1
            counters[f"gender_{row2.get('gender', 'unknown')}"] += 1

    year_files = []
    for year, rows in sorted(by_year.items()):
        rows.sort(key=lambda r: (r["date"], r["match_id"]))

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

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from .config import CANONICAL_DIR, RATING_INITIAL, RATING_K, RATINGS_DIR
from .utils import ensure_dirs, iter_jsonl_gz, now_utc_iso, read_json, write_json


@dataclass
class PlayerRating:
    name: str = ""
    gender: str = ""
    overall: float = RATING_INITIAL
    matches: int = 0
    level: dict[str, float] = field(default_factory=dict)
    level_matches: dict[str, int] = field(default_factory=dict)
    surface: dict[str, float] = field(default_factory=dict)
    surface_matches: dict[str, int] = field(default_factory=dict)
    level_surface: dict[str, float] = field(default_factory=dict)
    level_surface_matches: dict[str, int] = field(default_factory=dict)


def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


def update_pair(ra: float, rb: float, score_a: float, k: float = RATING_K) -> tuple[float, float]:
    ea = expected(ra, rb)
    delta = k * (score_a - ea)
    return ra + delta, rb - delta


def get_level_key(level: str, surface: str) -> str:
    return f"{level}|{surface}"


def ensure_player(players: dict[str, PlayerRating], key: str, side: dict[str, Any], gender: str) -> PlayerRating:
    if key not in players:
        players[key] = PlayerRating(name=side.get("name", ""), gender=gender)
    return players[key]


def main() -> None:
    ensure_dirs(RATINGS_DIR)
    manifest = read_json(CANONICAL_DIR / "manifest.json", {})
    players: dict[str, PlayerRating] = {}
    counters = Counter()

    files = manifest.get("year_files", [])
    for yf in sorted(files, key=lambda x: x["year"]):
        path = CANONICAL_DIR / str(yf["path"]).split("data/canonical/")[-1]
        for match in iter_jsonl_gz(path):
            gender = match["gender"]
            level = match["level"]
            surface = match["surface"]
            w_side = match["winner"]
            l_side = match["loser"]
            w_key = w_side["player_key"]
            l_key = l_side["player_key"]
            w = ensure_player(players, w_key, w_side, gender)
            l = ensure_player(players, l_key, l_side, gender)

            w.overall, l.overall = update_pair(w.overall, l.overall, 1.0)
            w.matches += 1
            l.matches += 1

            w_lvl = w.level.get(level, RATING_INITIAL)
            l_lvl = l.level.get(level, RATING_INITIAL)
            w.level[level], l.level[level] = update_pair(w_lvl, l_lvl, 1.0)
            w.level_matches[level] = w.level_matches.get(level, 0) + 1
            l.level_matches[level] = l.level_matches.get(level, 0) + 1

            if surface != "unknown":
                w_s = w.surface.get(surface, RATING_INITIAL)
                l_s = l.surface.get(surface, RATING_INITIAL)
                w.surface[surface], l.surface[surface] = update_pair(w_s, l_s, 1.0)
                w.surface_matches[surface] = w.surface_matches.get(surface, 0) + 1
                l.surface_matches[surface] = l.surface_matches.get(surface, 0) + 1

                ls_key = get_level_key(level, surface)
                w_ls = w.level_surface.get(ls_key, RATING_INITIAL)
                l_ls = l.level_surface.get(ls_key, RATING_INITIAL)
                w.level_surface[ls_key], l.level_surface[ls_key] = update_pair(w_ls, l_ls, 1.0)
                w.level_surface_matches[ls_key] = w.level_surface_matches.get(ls_key, 0) + 1
                l.level_surface_matches[ls_key] = l.level_surface_matches.get(ls_key, 0) + 1
            else:
                counters["unknown_surface"] += 1

            counters["processed_matches"] += 1
            counters[f"processed_{gender}"] += 1
            counters[f"processed_level_{level}"] += 1

    ratings = {
        "generated_at": now_utc_iso(),
        "rating_initial": RATING_INITIAL,
        "rating_k": RATING_K,
        "players": {
            key: {
                "name": p.name,
                "gender": p.gender,
                "overall": round(p.overall, 3),
                "matches": p.matches,
                "level": {k: round(v, 3) for k, v in sorted(p.level.items())},
                "level_matches": dict(sorted(p.level_matches.items())),
                "surface": {k: round(v, 3) for k, v in sorted(p.surface.items())},
                "surface_matches": dict(sorted(p.surface_matches.items())),
                "level_surface": {k: round(v, 3) for k, v in sorted(p.level_surface.items())},
                "level_surface_matches": dict(sorted(p.level_surface_matches.items())),
            }
            for key, p in sorted(players.items())
        },
    }
    write_json(RATINGS_DIR / "tle_player_ratings.json", ratings)

    summary = {
        "generated_at": now_utc_iso(),
        "players_total": len(players),
        "players_men": sum(1 for p in players.values() if p.gender == "men"),
        "players_women": sum(1 for p in players.values() if p.gender == "women"),
        **dict(counters),
    }
    write_json(RATINGS_DIR / "manifest.json", summary)
    write_json(__import__("pathlib").Path("data/reports/ratings/rating_build_report.json"), summary)
    print(summary)


if __name__ == "__main__":
    main()

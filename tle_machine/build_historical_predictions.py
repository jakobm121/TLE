from __future__ import annotations

import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CANONICAL_DIR, RATING_INITIAL, RATING_K, REPORTS_DIR
from .utils import ensure_dirs, iter_jsonl_gz, now_utc_iso, read_json, write_json


PREDICTIONS_DIR = Path("data/backtest")
REPORT_DIR = REPORTS_DIR / "backtest"
OUT_CSV_GZ = PREDICTIONS_DIR / "historical_predictions.csv.gz"
REPORT_JSON = REPORT_DIR / "historical_predictions_report.json"

VALID_SURFACES = {"hard", "clay", "grass", "carpet"}
VALID_LEVELS = {"grand_slam", "atp_wta", "challenger", "itf", "qualifying"}

BLEND_WEIGHTS = {
    "blend_80_20": (0.80, 0.20),
    "blend_70_30": (0.70, 0.30),
    "blend_60_40": (0.60, 0.40),
}


@dataclass
class PlayerState:
    name: str = ""
    gender: str = ""
    overall: float = RATING_INITIAL
    matches: int = 0
    level: dict[str, float] = field(default_factory=dict)
    level_matches: dict[str, int] = field(default_factory=dict)
    surface: dict[str, float] = field(default_factory=dict)
    surface_matches: dict[str, int] = field(default_factory=dict)


def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


def update_pair(ra: float, rb: float, score_a: float, k: float = RATING_K) -> tuple[float, float]:
    ea = expected(ra, rb)
    delta = k * (score_a - ea)
    return ra + delta, rb - delta


def ensure_player(players: dict[str, PlayerState], key: str, side: dict[str, Any], gender: str) -> PlayerState:
    if key not in players:
        players[key] = PlayerState(name=str(side.get("name", "") or ""), gender=gender)
    else:
        if not players[key].name and side.get("name"):
            players[key].name = str(side.get("name"))
        if not players[key].gender:
            players[key].gender = gender
    return players[key]


def rating_value(d: dict[str, float], key: str) -> float:
    return float(d.get(key, RATING_INITIAL))


def count_value(d: dict[str, int], key: str) -> int:
    return int(d.get(key, 0))


def grand_slam_level_component(p: PlayerState) -> tuple[float, int, float, int, float]:
    """Return GS+ATP/WTA blended level component.

    The rating is weighted by available match count, with a small prior so that
    pure GS samples are not overpowered too early. This is only used for
    model_level_gs_atp and all blend models on Grand Slam matches.
    """
    gs_r = rating_value(p.level, "grand_slam")
    atp_r = rating_value(p.level, "atp_wta")
    gs_n = count_value(p.level_matches, "grand_slam")
    atp_n = count_value(p.level_matches, "atp_wta")

    if gs_n <= 0 and atp_n <= 0:
        return RATING_INITIAL, 0, gs_r, gs_n, atp_r

    # Counts + priors: GS matters if present, ATP/WTA stabilizes small GS samples.
    gs_w = gs_n + (8 if gs_n > 0 else 0)
    atp_w = atp_n + (12 if atp_n > 0 else 0)
    total = gs_w + atp_w
    blended = (gs_r * gs_w + atp_r * atp_w) / total if total else RATING_INITIAL
    return blended, gs_n + atp_n, gs_r, gs_n, atp_r


def level_component(p: PlayerState, level: str) -> tuple[float, int]:
    if level == "grand_slam":
        blended, n, _, _, _ = grand_slam_level_component(p)
        return blended, n
    return rating_value(p.level, level), count_value(p.level_matches, level)


def sample_aware_surface_weight(a_surface_n: int, b_surface_n: int) -> float:
    """Surface weight grows only when both players have surface history."""
    n = min(a_surface_n, b_surface_n)
    if n < 5:
        return 0.0
    if n < 10:
        return 0.15
    if n < 25:
        return 0.25
    return 0.35


def blend_rating(level_r: float, surface_r: float, level_w: float, surface_w: float) -> float:
    total = level_w + surface_w
    if total <= 0:
        return RATING_INITIAL
    return (level_r * level_w + surface_r * surface_w) / total


def prediction_row(
    *,
    match: dict[str, Any],
    match_index: int,
    w_key: str,
    l_key: str,
    w: PlayerState,
    l: PlayerState,
) -> dict[str, Any]:
    level = str(match["level"])
    surface = str(match["surface"])
    gender = str(match["gender"])

    w_level_r, w_level_n = level_component(w, level)
    l_level_r, l_level_n = level_component(l, level)

    w_surface_r = rating_value(w.surface, surface) if surface in VALID_SURFACES else RATING_INITIAL
    l_surface_r = rating_value(l.surface, surface) if surface in VALID_SURFACES else RATING_INITIAL
    w_surface_n = count_value(w.surface_matches, surface) if surface in VALID_SURFACES else 0
    l_surface_n = count_value(l.surface_matches, surface) if surface in VALID_SURFACES else 0

    prob_level = expected(w_level_r, l_level_r)
    prob_surface = expected(w_surface_r, l_surface_r) if surface in VALID_SURFACES else ""

    gs_w_blend = gs_l_blend = ""
    w_gs_n = l_gs_n = w_atp_n = l_atp_n = ""
    if level == "grand_slam":
        gs_w_blend, _, w_gs_r, w_gs_n, w_atp_r = grand_slam_level_component(w)
        gs_l_blend, _, l_gs_r, l_gs_n, l_atp_r = grand_slam_level_component(l)
        w_atp_n = count_value(w.level_matches, "atp_wta")
        l_atp_n = count_value(l.level_matches, "atp_wta")

    row = {
        "match_index": match_index,
        "date": match.get("date", ""),
        "source": match.get("source", ""),
        "match_id": match.get("match_id", "") or match.get("source_match_id", ""),
        "gender": gender,
        "level": level,
        "surface": surface,
        "tournament": match.get("tournament", "") or match.get("tournament_name", ""),
        "round": match.get("round", ""),
        "winner_key": w_key,
        "winner_name": w.name,
        "loser_key": l_key,
        "loser_name": l.name,
        "winner_level_rating": round(w_level_r, 6),
        "loser_level_rating": round(l_level_r, 6),
        "winner_level_matches": w_level_n,
        "loser_level_matches": l_level_n,
        "winner_surface_rating": "" if surface not in VALID_SURFACES else round(w_surface_r, 6),
        "loser_surface_rating": "" if surface not in VALID_SURFACES else round(l_surface_r, 6),
        "winner_surface_matches": w_surface_n,
        "loser_surface_matches": l_surface_n,
        "prob_level_only_winner": round(prob_level, 8),
        "prob_surface_only_winner": "" if prob_surface == "" else round(float(prob_surface), 8),
        "winner_total_matches_before": w.matches,
        "loser_total_matches_before": l.matches,
        "winner_gs_matches": w_gs_n,
        "loser_gs_matches": l_gs_n,
        "winner_atp_wta_matches": w_atp_n,
        "loser_atp_wta_matches": l_atp_n,
        "winner_gs_atp_level_rating": "" if level != "grand_slam" else round(float(gs_w_blend), 6),
        "loser_gs_atp_level_rating": "" if level != "grand_slam" else round(float(gs_l_blend), 6),
    }

    if surface in VALID_SURFACES:
        for model, (lw, sw) in BLEND_WEIGHTS.items():
            wr = blend_rating(w_level_r, w_surface_r, lw, sw)
            lr = blend_rating(l_level_r, l_surface_r, lw, sw)
            row[f"prob_{model}_winner"] = round(expected(wr, lr), 8)

        sw = sample_aware_surface_weight(w_surface_n, l_surface_n)
        lw = 1.0 - sw
        wr = blend_rating(w_level_r, w_surface_r, lw, sw)
        lr = blend_rating(l_level_r, l_surface_r, lw, sw)
        row["sample_aware_surface_weight"] = round(sw, 4)
        row["prob_sample_aware_winner"] = round(expected(wr, lr), 8)
    else:
        for model in BLEND_WEIGHTS:
            row[f"prob_{model}_winner"] = ""
        row["sample_aware_surface_weight"] = ""
        row["prob_sample_aware_winner"] = ""

    return row


def update_states(w: PlayerState, l: PlayerState, level: str, surface: str) -> None:
    # Overall is kept internally for consistency with build_ratings, but not output/tested.
    w.overall, l.overall = update_pair(w.overall, l.overall, 1.0)
    w.matches += 1
    l.matches += 1

    w_lvl = rating_value(w.level, level)
    l_lvl = rating_value(l.level, level)
    w.level[level], l.level[level] = update_pair(w_lvl, l_lvl, 1.0)
    w.level_matches[level] = count_value(w.level_matches, level) + 1
    l.level_matches[level] = count_value(l.level_matches, level) + 1

    if surface in VALID_SURFACES:
        w_s = rating_value(w.surface, surface)
        l_s = rating_value(l.surface, surface)
        w.surface[surface], l.surface[surface] = update_pair(w_s, l_s, 1.0)
        w.surface_matches[surface] = count_value(w.surface_matches, surface) + 1
        l.surface_matches[surface] = count_value(l.surface_matches, surface) + 1


def fieldnames() -> list[str]:
    return [
        "match_index",
        "date",
        "source",
        "match_id",
        "gender",
        "level",
        "surface",
        "tournament",
        "round",
        "winner_key",
        "winner_name",
        "loser_key",
        "loser_name",
        "winner_level_rating",
        "loser_level_rating",
        "winner_level_matches",
        "loser_level_matches",
        "winner_surface_rating",
        "loser_surface_rating",
        "winner_surface_matches",
        "loser_surface_matches",
        "prob_level_only_winner",
        "prob_surface_only_winner",
        "prob_blend_80_20_winner",
        "prob_blend_70_30_winner",
        "prob_blend_60_40_winner",
        "sample_aware_surface_weight",
        "prob_sample_aware_winner",
        "winner_total_matches_before",
        "loser_total_matches_before",
        "winner_gs_matches",
        "loser_gs_matches",
        "winner_atp_wta_matches",
        "loser_atp_wta_matches",
        "winner_gs_atp_level_rating",
        "loser_gs_atp_level_rating",
    ]


def main() -> None:
    ensure_dirs(PREDICTIONS_DIR, REPORT_DIR)

    manifest = read_json(CANONICAL_DIR / "manifest.json", {})
    files = manifest.get("year_files", [])

    players: dict[str, PlayerState] = {}
    counters = Counter()

    with gzip.open(OUT_CSV_GZ, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames())
        writer.writeheader()

        match_index = 0
        for yf in sorted(files, key=lambda x: x["year"]):
            path = CANONICAL_DIR / str(yf["path"]).split("data/canonical/")[-1]
            for match in iter_jsonl_gz(path):
                match_index += 1

                gender = str(match["gender"])
                level = str(match["level"])
                surface = str(match["surface"])
                w_side = match["winner"]
                l_side = match["loser"]
                w_key = w_side["player_key"]
                l_key = l_side["player_key"]

                w = ensure_player(players, w_key, w_side, gender)
                l = ensure_player(players, l_key, l_side, gender)

                writer.writerow(
                    prediction_row(
                        match=match,
                        match_index=match_index,
                        w_key=w_key,
                        l_key=l_key,
                        w=w,
                        l=l,
                    )
                )

                update_states(w, l, level, surface)

                counters["matches"] += 1
                counters[f"gender_{gender}"] += 1
                counters[f"level_{level}"] += 1
                counters[f"surface_{surface}"] += 1
                if surface not in VALID_SURFACES:
                    counters["unknown_surface"] += 1

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "output_csv_gz": str(OUT_CSV_GZ),
        "rating_initial": RATING_INITIAL,
        "rating_k": RATING_K,
        "models": [
            "level_only",
            "surface_only",
            "blend_80_20",
            "blend_70_30",
            "blend_60_40",
            "sample_aware",
        ],
        "notes": [
            "Predictions are pre-match: each row is written before Elo states are updated with that match.",
            "Overall Elo is updated internally for consistency but is not output as a tested model.",
            "No level_surface/challenger_clay model is tested here.",
            "Grand Slam level component blends grand_slam and atp_wta level ratings.",
            "Surface component is pure surface Elo across levels.",
        ],
        "counters": dict(sorted(counters.items())),
    }
    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

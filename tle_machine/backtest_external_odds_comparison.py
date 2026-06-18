from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import re
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SOURCE_URL = "https://raw.githubusercontent.com/jakobm121/Ai/refs/heads/main/data/tennis_results.json"

PREDICTIONS_CSV_GZ = Path("data/backtest/historical_predictions.csv.gz")
API_MAPPING_JSON = Path("data/metadata/api_tennis/player_mapping.json")

BACKTEST_DIR = Path("data/backtest")
REPORT_DIR = Path("data/reports/backtest")

OUT_DETAIL_CSV = BACKTEST_DIR / "external_odds_comparison.csv"
OUT_SCENARIOS_CSV = BACKTEST_DIR / "external_odds_comparison_scenarios.csv"
OUT_BY_LEVEL_CSV = BACKTEST_DIR / "external_odds_comparison_by_level.csv"
REPORT_JSON = REPORT_DIR / "external_odds_comparison_report.json"

MODEL_NAME = "blend_80_20"
MODEL_COL = "prob_blend_80_20_winner"

DEFAULT_MIN_LEVEL = 10
DEFAULT_MIN_SURFACE = 5

EDGE_THRESHOLDS = [-0.02, 0.0, 0.02, 0.04, 0.06, 0.08, 0.10]
PROB_THRESHOLDS = [0.50, 0.52, 0.55, 0.57, 0.60, 0.62, 0.65, 0.67, 0.70, 0.72, 0.75, 0.80]


DETAIL_FIELDS = [
    "pick_id",
    "date",
    "time",
    "gender",
    "tour_level",
    "tournament",
    "round",
    "match",
    "bet",
    "player_name",
    "opponent_name",
    "player_key",
    "opponent_key",
    "player_api_key",
    "opponent_api_key",
    "player_canonical_key",
    "opponent_canonical_key",
    "mapping_status",
    "match_status",
    "prediction_match_id",
    "winner_key",
    "loser_key",
    "winner_name",
    "loser_name",
    "surface",
    "level",
    "picked_side_in_prediction",
    "old_model_prob",
    "old_implied_prob",
    "old_edge",
    "odds",
    "stake",
    "result",
    "old_profit",
    "old_roi",
    "elo_prob_pick",
    "elo_pick_prob",
    "elo_selected_side",
    "elo_agrees_with_old_pick",
    "elo_implied_prob",
    "elo_edge",
    "elo_min_level_matches",
    "elo_min_surface_matches",
    "elo_eligible",
    "elo_no_bet_reason",
    "winner_level_matches",
    "loser_level_matches",
    "winner_surface_matches",
    "loser_surface_matches",
    "player_last10_win_rate",
    "opponent_last10_win_rate",
    "last10_win_rate_diff",
    "h2h_matches",
    "h2h_player_wins",
    "h2h_opponent_wins",
    "favorite_type",
    "confidence",
    "quality_score",
    "best_bookmaker",
    "market_median_odds",
    "bookmakers_used",
]


SCENARIO_FIELDS = [
    "scenario",
    "description",
    "picks",
    "wins",
    "losses",
    "push_or_other",
    "hit_rate",
    "total_stake",
    "total_profit",
    "roi",
    "avg_odds",
    "avg_old_model_prob",
    "avg_elo_prob",
    "avg_old_edge",
    "avg_elo_edge",
]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_float(x: Any) -> float | None:
    if x is None:
        return None
    if isinstance(x, bool):
        return float(int(x))
    s = str(x).strip()
    if not s:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    return v if math.isfinite(v) else None


def safe_int(x: Any) -> int | None:
    v = safe_float(x)
    if v is None:
        return None
    return int(v)


def safe_str(x: Any) -> str:
    return str(x or "").strip()


def normalize_gender(g: Any) -> str:
    s = safe_str(g).lower()
    if s in {"m", "man", "men", "male", "atp"}:
        return "men"
    if s in {"w", "woman", "women", "female", "wta"}:
        return "women"
    return s


def normalize_level(level: Any, event_type: Any = None, qualification: Any = None) -> str:
    s = safe_str(level).lower().replace("-", "_").replace(" ", "_")
    ev = safe_str(event_type).lower()
    qual = str(qualification).strip().lower() in {"1", "true", "yes", "y"}
    if qual or "qualification" in ev or "qualifying" in ev:
        return "qualifying"
    if s in {"atp_wta", "main_tour", "tour"}:
        return "atp_wta"
    if s in {"grand_slam", "slam"}:
        return "grand_slam"
    if s in {"challenger"}:
        return "challenger"
    if s in {"itf"}:
        return "itf"
    return s or "unknown"


def api_key(gender: str, player_id: Any) -> str | None:
    pid = safe_int(player_id)
    if pid is None:
        return None
    g = normalize_gender(gender)
    if g not in {"men", "women"}:
        return None
    return f"{g}:api:{pid}"


def read_json_url_or_path(source: str) -> Any:
    if source.startswith("http://") or source.startswith("https://"):
        req = urllib.request.Request(
            source,
            headers={
                "User-Agent": "TLE-external-odds-comparison/1.0",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    with Path(source).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def read_api_mapping(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing API mapping: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    mapping = data.get("mapping", data)
    if not isinstance(mapping, dict):
        raise ValueError(f"Invalid mapping format in {path}")

    out: dict[str, dict[str, Any]] = {}
    for k, v in mapping.items():
        if isinstance(v, dict):
            out[str(k)] = v
        elif isinstance(v, str):
            out[str(k)] = {"status": "mapped", "sackmann_player_key": v}
        elif v is None:
            out[str(k)] = {"status": "unmapped", "sackmann_player_key": None}
    return out


def canonical_for_api(api_mapping: dict[str, dict[str, Any]], key: str | None) -> tuple[str | None, str]:
    if not key:
        return None, "missing_api_key"
    item = api_mapping.get(key)
    if not item:
        return None, "not_in_mapping"
    target = item.get("sackmann_player_key") or item.get("canonical_player_key") or item.get("target")
    status = safe_str(item.get("status")) or ("mapped" if target else "unmapped")
    if not target:
        return None, status or "unmapped"
    return safe_str(target), status


def iter_prediction_rows(path: Path) -> Iterable[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def digits_only(x: Any) -> str:
    return re.sub(r"\D+", "", safe_str(x))


def build_prediction_indexes(predictions_path: Path, wanted_dates: set[str] | None = None) -> tuple[dict[tuple[str, str, str], list[dict[str, str]]], dict[tuple[str, str, frozenset[str]], list[dict[str, str]]], Counter]:
    if not predictions_path.exists():
        raise FileNotFoundError(f"Missing historical predictions: {predictions_path}")

    by_match_id: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    by_pair: dict[tuple[str, str, frozenset[str]], list[dict[str, str]]] = defaultdict(list)
    counters = Counter()

    for row in iter_prediction_rows(predictions_path):
        date = safe_str(row.get("date"))
        if wanted_dates and date not in wanted_dates:
            continue
        gender = normalize_gender(row.get("gender"))
        w = safe_str(row.get("winner_key"))
        l = safe_str(row.get("loser_key"))
        if not date or not gender or not w or not l:
            counters["skipped_prediction_incomplete"] += 1
            continue

        mid = digits_only(row.get("match_id"))
        if mid:
            by_match_id[(date, gender, mid)].append(row)

        by_pair[(date, gender, frozenset((w, l)))].append(row)
        counters["indexed_predictions"] += 1

    counters["index_match_id_keys"] = len(by_match_id)
    counters["index_pair_keys"] = len(by_pair)
    return by_match_id, by_pair, counters


def pick_result_profit(item: dict[str, Any]) -> tuple[str, float | None, float | None, float | None]:
    result = safe_str(item.get("result")).lower()
    stake = safe_float(item.get("stake"))
    odds = safe_float(item.get("odds"))
    profit = safe_float(item.get("profit"))

    if profit is None and stake is not None and odds is not None:
        if result == "win":
            profit = stake * (odds - 1.0)
        elif result == "loss":
            profit = -stake

    roi = profit / stake if profit is not None and stake not in {None, 0} else None
    return result, stake, profit, roi


def nested(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def prediction_match_for_pick(
    item: dict[str, Any],
    *,
    gender: str,
    player_ckey: str | None,
    opponent_ckey: str | None,
    by_match_id: dict[tuple[str, str, str], list[dict[str, str]]],
    by_pair: dict[tuple[str, str, frozenset[str]], list[dict[str, str]]],
) -> tuple[dict[str, str] | None, str]:
    date = safe_str(item.get("date"))
    if not date:
        return None, "missing_date"

    fixture_id = digits_only(item.get("fixture_id") or item.get("event_key"))
    if fixture_id:
        rows = by_match_id.get((date, gender, fixture_id), [])
        if len(rows) == 1:
            return rows[0], "matched_by_match_id"
        if len(rows) > 1 and player_ckey and opponent_ckey:
            pair = frozenset((player_ckey, opponent_ckey))
            exact = [r for r in rows if frozenset((safe_str(r.get("winner_key")), safe_str(r.get("loser_key")))) == pair]
            if len(exact) == 1:
                return exact[0], "matched_by_match_id_pair"
            return rows[0], "matched_by_match_id_ambiguous"

    if player_ckey and opponent_ckey:
        rows = by_pair.get((date, gender, frozenset((player_ckey, opponent_ckey))), [])
        if len(rows) == 1:
            return rows[0], "matched_by_date_gender_pair"
        if len(rows) > 1:
            return rows[0], "matched_by_date_gender_pair_ambiguous"

    return None, "no_prediction_match"


def bool_str(x: bool | None) -> str:
    if x is None:
        return ""
    return "true" if x else "false"


def min_counts_from_prediction(row: dict[str, str]) -> tuple[int, int]:
    wl = safe_int(row.get("winner_level_matches")) or 0
    ll = safe_int(row.get("loser_level_matches")) or 0
    ws = safe_int(row.get("winner_surface_matches")) or 0
    ls = safe_int(row.get("loser_surface_matches")) or 0
    return min(wl, ll), min(ws, ls)


def enrich_pick_row(
    item: dict[str, Any],
    *,
    api_mapping: dict[str, dict[str, Any]],
    by_match_id: dict[tuple[str, str, str], list[dict[str, str]]],
    by_pair: dict[tuple[str, str, frozenset[str]], list[dict[str, str]]],
    min_level_matches: int,
    min_surface_matches: int,
) -> dict[str, Any]:
    gender = normalize_gender(item.get("gender"))
    level = normalize_level(item.get("tour_level"), item.get("event_type"), item.get("qualification"))

    p_api = api_key(gender, item.get("player_key"))
    o_api = api_key(gender, item.get("opponent_key"))
    p_ckey, p_map_status = canonical_for_api(api_mapping, p_api)
    o_ckey, o_map_status = canonical_for_api(api_mapping, o_api)

    mapping_status = "mapped"
    if not p_ckey and not o_ckey:
        mapping_status = f"both_unmapped:{p_map_status}|{o_map_status}"
    elif not p_ckey:
        mapping_status = f"player_unmapped:{p_map_status}"
    elif not o_ckey:
        mapping_status = f"opponent_unmapped:{o_map_status}"

    pred, match_status = prediction_match_for_pick(
        item,
        gender=gender,
        player_ckey=p_ckey,
        opponent_ckey=o_ckey,
        by_match_id=by_match_id,
        by_pair=by_pair,
    )

    result, stake, profit, roi = pick_result_profit(item)
    odds = safe_float(item.get("odds"))
    old_model_prob = safe_float(item.get("model_prob"))
    implied = safe_float(item.get("implied_prob"))
    if implied is None and odds:
        implied = 1.0 / odds
    old_edge = safe_float(item.get("edge"))
    if old_edge is None and old_model_prob is not None and implied is not None:
        old_edge = old_model_prob - implied

    detail: dict[str, Any] = {
        "pick_id": item.get("pick_id"),
        "date": item.get("date"),
        "time": item.get("time"),
        "gender": gender,
        "tour_level": level,
        "tournament": item.get("tournament"),
        "round": item.get("round"),
        "match": item.get("match"),
        "bet": item.get("bet"),
        "player_name": item.get("player_name"),
        "opponent_name": item.get("opponent_name"),
        "player_key": item.get("player_key"),
        "opponent_key": item.get("opponent_key"),
        "player_api_key": p_api,
        "opponent_api_key": o_api,
        "player_canonical_key": p_ckey,
        "opponent_canonical_key": o_ckey,
        "mapping_status": mapping_status,
        "match_status": match_status,
        "old_model_prob": old_model_prob,
        "old_implied_prob": implied,
        "old_edge": old_edge,
        "odds": odds,
        "stake": stake,
        "result": result,
        "old_profit": profit,
        "old_roi": roi,
        "favorite_type": item.get("favorite_type"),
        "confidence": item.get("confidence"),
        "quality_score": item.get("quality_score"),
        "best_bookmaker": item.get("best_bookmaker"),
        "market_median_odds": item.get("market_median_odds"),
        "bookmakers_used": item.get("bookmakers_used"),
        "player_last10_win_rate": nested(item, "player_form", "last_10", "win_rate"),
        "opponent_last10_win_rate": nested(item, "opponent_form", "last_10", "win_rate"),
        "last10_win_rate_diff": None,
        "h2h_matches": nested(item, "h2h", "matches"),
        "h2h_player_wins": nested(item, "h2h", "first_wins"),
        "h2h_opponent_wins": nested(item, "h2h", "second_wins"),
    }

    pw = safe_float(detail["player_last10_win_rate"])
    ow = safe_float(detail["opponent_last10_win_rate"])
    if pw is not None and ow is not None:
        detail["last10_win_rate_diff"] = round(pw - ow, 6)

    if pred is None:
        detail.update(
            {
                "prediction_match_id": "",
                "winner_key": "",
                "loser_key": "",
                "winner_name": "",
                "loser_name": "",
                "surface": "",
                "level": "",
                "picked_side_in_prediction": "",
                "elo_prob_pick": None,
                "elo_pick_prob": None,
                "elo_selected_side": "",
                "elo_agrees_with_old_pick": "",
                "elo_implied_prob": implied,
                "elo_edge": None,
                "elo_min_level_matches": "",
                "elo_min_surface_matches": "",
                "elo_eligible": False,
                "elo_no_bet_reason": "no_prediction_match",
                "winner_level_matches": "",
                "loser_level_matches": "",
                "winner_surface_matches": "",
                "loser_surface_matches": "",
            }
        )
        return detail

    w_key = safe_str(pred.get("winner_key"))
    l_key = safe_str(pred.get("loser_key"))
    prob_winner = safe_float(pred.get(MODEL_COL))
    surface = safe_str(pred.get("surface")).lower()
    pred_level = safe_str(pred.get("level")).lower()
    min_level, min_surface = min_counts_from_prediction(pred)

    picked_side = ""
    elo_prob_pick: float | None = None
    if p_ckey == w_key:
        picked_side = "winner"
        elo_prob_pick = prob_winner
    elif p_ckey == l_key and prob_winner is not None:
        picked_side = "loser"
        elo_prob_pick = 1.0 - prob_winner

    elo_pick_prob = max(elo_prob_pick, 1.0 - elo_prob_pick) if elo_prob_pick is not None else None
    elo_selected_side = ""
    agrees: bool | None = None
    if elo_prob_pick is not None:
        agrees = elo_prob_pick >= 0.5
        elo_selected_side = "old_pick" if agrees else "opponent"

    elo_edge = elo_prob_pick - implied if elo_prob_pick is not None and implied is not None else None

    no_bet_reason = ""
    elo_eligible = True
    if mapping_status != "mapped":
        elo_eligible = False
        no_bet_reason = "unmapped_player"
    elif picked_side == "":
        elo_eligible = False
        no_bet_reason = "picked_player_not_in_prediction"
    elif prob_winner is None:
        elo_eligible = False
        no_bet_reason = "missing_elo_probability"
    elif surface in {"", "unknown"}:
        elo_eligible = False
        no_bet_reason = "unknown_surface"
    elif min_level < min_level_matches:
        elo_eligible = False
        no_bet_reason = "min_level_not_met"
    elif min_surface < min_surface_matches:
        elo_eligible = False
        no_bet_reason = "min_surface_not_met"

    detail.update(
        {
            "prediction_match_id": pred.get("match_id"),
            "winner_key": w_key,
            "loser_key": l_key,
            "winner_name": pred.get("winner_name"),
            "loser_name": pred.get("loser_name"),
            "surface": surface,
            "level": pred_level,
            "picked_side_in_prediction": picked_side,
            "elo_prob_pick": None if elo_prob_pick is None else round(elo_prob_pick, 8),
            "elo_pick_prob": None if elo_pick_prob is None else round(elo_pick_prob, 8),
            "elo_selected_side": elo_selected_side,
            "elo_agrees_with_old_pick": bool_str(agrees),
            "elo_implied_prob": implied,
            "elo_edge": None if elo_edge is None else round(elo_edge, 8),
            "elo_min_level_matches": min_level,
            "elo_min_surface_matches": min_surface,
            "elo_eligible": elo_eligible,
            "elo_no_bet_reason": no_bet_reason,
            "winner_level_matches": pred.get("winner_level_matches"),
            "loser_level_matches": pred.get("loser_level_matches"),
            "winner_surface_matches": pred.get("winner_surface_matches"),
            "loser_surface_matches": pred.get("loser_surface_matches"),
        }
    )
    return detail


class RoiMetrics:
    def __init__(self) -> None:
        self.picks = 0
        self.wins = 0
        self.losses = 0
        self.other = 0
        self.stake = 0.0
        self.profit = 0.0
        self.odds_sum = 0.0
        self.odds_n = 0
        self.old_prob_sum = 0.0
        self.old_prob_n = 0
        self.elo_prob_sum = 0.0
        self.elo_prob_n = 0
        self.old_edge_sum = 0.0
        self.old_edge_n = 0
        self.elo_edge_sum = 0.0
        self.elo_edge_n = 0

    def add(self, row: dict[str, Any]) -> None:
        self.picks += 1
        result = safe_str(row.get("result")).lower()
        if result == "win":
            self.wins += 1
        elif result == "loss":
            self.losses += 1
        else:
            self.other += 1

        stake = safe_float(row.get("stake"))
        profit = safe_float(row.get("old_profit"))
        odds = safe_float(row.get("odds"))
        old_prob = safe_float(row.get("old_model_prob"))
        elo_prob = safe_float(row.get("elo_prob_pick"))
        old_edge = safe_float(row.get("old_edge"))
        elo_edge = safe_float(row.get("elo_edge"))

        if stake is not None:
            self.stake += stake
        if profit is not None:
            self.profit += profit
        if odds is not None:
            self.odds_sum += odds
            self.odds_n += 1
        if old_prob is not None:
            self.old_prob_sum += old_prob
            self.old_prob_n += 1
        if elo_prob is not None:
            self.elo_prob_sum += elo_prob
            self.elo_prob_n += 1
        if old_edge is not None:
            self.old_edge_sum += old_edge
            self.old_edge_n += 1
        if elo_edge is not None:
            self.elo_edge_sum += elo_edge
            self.elo_edge_n += 1

    def row(self, scenario: str, description: str) -> dict[str, Any]:
        settled = self.wins + self.losses
        return {
            "scenario": scenario,
            "description": description,
            "picks": self.picks,
            "wins": self.wins,
            "losses": self.losses,
            "push_or_other": self.other,
            "hit_rate": round(self.wins / settled, 6) if settled else None,
            "total_stake": round(self.stake, 6),
            "total_profit": round(self.profit, 6),
            "roi": round(self.profit / self.stake, 6) if self.stake else None,
            "avg_odds": round(self.odds_sum / self.odds_n, 6) if self.odds_n else None,
            "avg_old_model_prob": round(self.old_prob_sum / self.old_prob_n, 6) if self.old_prob_n else None,
            "avg_elo_prob": round(self.elo_prob_sum / self.elo_prob_n, 6) if self.elo_prob_n else None,
            "avg_old_edge": round(self.old_edge_sum / self.old_edge_n, 6) if self.old_edge_n else None,
            "avg_elo_edge": round(self.elo_edge_sum / self.elo_edge_n, 6) if self.elo_edge_n else None,
        }


def scenario_rows(detail_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scenarios: list[tuple[str, str, Any]] = [
        ("old_all_settled", "All settled picks from the old Ai model.", lambda r: True),
        ("matched_all_old_picks", "Old picks that matched a TLE historical prediction row.", lambda r: safe_str(r.get("match_status")).startswith("matched")),
        ("elo_eligible_all_old_picks", "Matched old picks passing Elo NO_BET filters.", lambda r: bool(r.get("elo_eligible"))),
        ("old_and_elo_agree", "Old pick only when Elo also selects the same player.", lambda r: bool(r.get("elo_eligible")) and safe_str(r.get("elo_agrees_with_old_pick")) == "true"),
        ("old_and_elo_disagree", "Old pick when Elo selects the opponent; useful as an avoid bucket.", lambda r: bool(r.get("elo_eligible")) and safe_str(r.get("elo_agrees_with_old_pick")) == "false"),
    ]

    for th in EDGE_THRESHOLDS:
        scenarios.append(
            (
                f"elo_edge_ge_{th:+.2f}",
                f"Old pick only if Elo probability minus implied probability is >= {th:+.2%}.",
                lambda r, th=th: bool(r.get("elo_eligible")) and (safe_float(r.get("elo_edge")) is not None and safe_float(r.get("elo_edge")) >= th),
            )
        )

    for th in PROB_THRESHOLDS:
        scenarios.append(
            (
                f"elo_prob_ge_{th:.2f}",
                f"Old pick only if Elo probability for the old pick is >= {th:.0%}.",
                lambda r, th=th: bool(r.get("elo_eligible")) and (safe_float(r.get("elo_prob_pick")) is not None and safe_float(r.get("elo_prob_pick")) >= th),
            )
        )

    rows = []
    for name, desc, pred in scenarios:
        m = RoiMetrics()
        for r in detail_rows:
            try:
                keep = pred(r)
            except Exception:
                keep = False
            if keep:
                m.add(r)
        rows.append(m.row(name, desc))
    return rows


def by_level_rows(detail_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], RoiMetrics] = defaultdict(RoiMetrics)

    for r in detail_rows:
        level = safe_str(r.get("level") or r.get("tour_level") or "unknown").lower()
        groups[(level, "old_all_settled")].add(r)
        if bool(r.get("elo_eligible")):
            groups[(level, "elo_eligible_all_old_picks")].add(r)
        if bool(r.get("elo_eligible")) and safe_str(r.get("elo_agrees_with_old_pick")) == "true":
            groups[(level, "old_and_elo_agree")].add(r)
        edge = safe_float(r.get("elo_edge"))
        if bool(r.get("elo_eligible")) and edge is not None and edge >= 0.04:
            groups[(level, "elo_edge_ge_+0.04")].add(r)

    out = []
    for (level, scenario), metrics in sorted(groups.items()):
        row = metrics.row(scenario, f"{scenario} within level={level}")
        row["level"] = level
        out.append(row)
    return out


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=DEFAULT_SOURCE_URL, help="External Ai tennis_results.json URL or local path")
    parser.add_argument("--predictions", type=Path, default=PREDICTIONS_CSV_GZ)
    parser.add_argument("--api-mapping", type=Path, default=API_MAPPING_JSON)
    parser.add_argument("--min-level-matches", type=int, default=DEFAULT_MIN_LEVEL)
    parser.add_argument("--min-surface-matches", type=int, default=DEFAULT_MIN_SURFACE)
    args = parser.parse_args()

    raw = read_json_url_or_path(args.source)
    if not isinstance(raw, list):
        raise ValueError(f"Expected external source to be a list, got {type(raw).__name__}")

    settled_items = [x for x in raw if isinstance(x, dict) and safe_str(x.get("result")).lower() in {"win", "loss"}]
    wanted_dates = {safe_str(x.get("date")) for x in settled_items if safe_str(x.get("date"))}

    api_mapping = read_api_mapping(args.api_mapping)
    by_match_id, by_pair, pred_counters = build_prediction_indexes(args.predictions, wanted_dates=wanted_dates)

    detail_rows: list[dict[str, Any]] = []
    counters = Counter()

    for item in settled_items:
        row = enrich_pick_row(
            item,
            api_mapping=api_mapping,
            by_match_id=by_match_id,
            by_pair=by_pair,
            min_level_matches=args.min_level_matches,
            min_surface_matches=args.min_surface_matches,
        )
        detail_rows.append(row)
        counters["settled_picks"] += 1
        counters[f"mapping_{safe_str(row.get('mapping_status')).split(':')[0]}"] += 1
        counters[f"match_{safe_str(row.get('match_status'))}"] += 1
        if row.get("elo_eligible"):
            counters["elo_eligible"] += 1
        else:
            counters[f"elo_no_bet_{safe_str(row.get('elo_no_bet_reason')) or 'unknown'}"] += 1

    scenarios = scenario_rows(detail_rows)
    by_level = by_level_rows(detail_rows)

    write_csv(OUT_DETAIL_CSV, detail_rows, DETAIL_FIELDS)
    write_csv(OUT_SCENARIOS_CSV, scenarios, SCENARIO_FIELDS)
    write_csv(OUT_BY_LEVEL_CSV, by_level, ["level", *SCENARIO_FIELDS])

    scenario_by_name = {r["scenario"]: r for r in scenarios}
    report = {
        "status": "ok",
        "generated_at": now_utc_iso(),
        "source": args.source,
        "predictions": str(args.predictions),
        "api_mapping": str(args.api_mapping),
        "model": MODEL_NAME,
        "model_col": MODEL_COL,
        "filters": {
            "min_level_matches": args.min_level_matches,
            "min_surface_matches": args.min_surface_matches,
            "unknown_surface": "NO_BET",
            "unmapped_player": "NO_BET",
        },
        "counters": dict(sorted(counters.items())),
        "prediction_index_counters": dict(sorted(pred_counters.items())),
        "headline": {
            "old_all_settled": scenario_by_name.get("old_all_settled"),
            "matched_all_old_picks": scenario_by_name.get("matched_all_old_picks"),
            "elo_eligible_all_old_picks": scenario_by_name.get("elo_eligible_all_old_picks"),
            "old_and_elo_agree": scenario_by_name.get("old_and_elo_agree"),
            "old_and_elo_disagree": scenario_by_name.get("old_and_elo_disagree"),
            "elo_edge_ge_+0.02": scenario_by_name.get("elo_edge_ge_+0.02"),
            "elo_edge_ge_+0.04": scenario_by_name.get("elo_edge_ge_+0.04"),
            "elo_edge_ge_+0.06": scenario_by_name.get("elo_edge_ge_+0.06"),
            "elo_edge_ge_+0.08": scenario_by_name.get("elo_edge_ge_+0.08"),
        },
        "outputs": {
            "detail_csv": str(OUT_DETAIL_CSV),
            "scenarios_csv": str(OUT_SCENARIOS_CSV),
            "by_level_csv": str(OUT_BY_LEVEL_CSV),
            "report_json": str(REPORT_JSON),
        },
        "notes": [
            "ROI is calculated using the old pick side and its recorded odds/stake/profit.",
            "Because the external file normally has odds only for the old selected player, this does not simulate betting the opposite side when Elo disagrees.",
            "old_and_elo_disagree should be interpreted as an avoid bucket, not as an opposite-side betting ROI.",
            "Elo edge is Elo probability for the old picked player minus implied probability from the old picked odds.",
        ],
    }

    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

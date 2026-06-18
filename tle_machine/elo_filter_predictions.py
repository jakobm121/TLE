from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PICKS_URL = "https://raw.githubusercontent.com/jakobm121/Ai/refs/heads/main/data/tennis_predictions.json"
DEFAULT_ODDS_URL = "https://raw.githubusercontent.com/jakobm121/Ai/refs/heads/main/data/tennis_odds_today.json"

API_MAPPING_JSON = Path("data/metadata/api_tennis/player_mapping.json")

RATING_CANDIDATES = [
    Path("data/ratings/tle_player_ratings.json.gz"),
    Path("data/ratings/tle_player_ratings.json"),
    Path("data/ratings/player_ratings.json.gz"),
    Path("data/ratings/player_ratings.json"),
    Path("data/ratings/ratings.json.gz"),
    Path("data/ratings/ratings.json"),
]

BASE_DIR = Path("data/elo_filter")
REPORT_DIR = Path("data/reports/elo_filter")

PREDICTIONS_JSON = BASE_DIR / "predictions.json"
RESULTS_JSON = BASE_DIR / "results.json"
ACTIVE_CSV = BASE_DIR / "active_predictions.csv"
REPORT_JSON = REPORT_DIR / "predictions_report.json"

RATING_INITIAL = 1500.0
VALID_SURFACES = {"hard", "clay", "grass", "carpet"}

LEVEL_RULES = {
    "atp_wta": {
        "model": "blend_70_30",
        "level_weight": 0.70,
        "surface_weight": 0.30,
        "needs_surface": True,
        "min_level_matches": 10,
        "min_surface_matches": 5,
        "min_prob": 0.50,
        "edge_bet": 0.04,
        "edge_strong": 0.08,
    },
    "grand_slam": {
        "model": "blend_70_30",
        "level_weight": 0.70,
        "surface_weight": 0.30,
        "needs_surface": True,
        "min_level_matches": 10,
        "min_surface_matches": 5,
        "min_prob": 0.50,
        "edge_bet": 0.04,
        "edge_strong": 0.08,
    },
    "challenger": {
        "model": "blend_60_40",
        "level_weight": 0.60,
        "surface_weight": 0.40,
        "needs_surface": True,
        "min_level_matches": 10,
        "min_surface_matches": 5,
        "min_prob": 0.50,
        "edge_bet": 0.04,
        "edge_strong": 0.08,
    },
    "itf": {
        "model": "level_only",
        "level_weight": 1.00,
        "surface_weight": 0.00,
        "needs_surface": False,
        "min_level_matches": 10,
        "min_surface_matches": 0,
        "min_prob": 0.50,
        "edge_bet": 0.04,
        "edge_strong": 0.08,
    },
    "qualifying": {
        "model": "level_only_qualifying_strict",
        "level_weight": 1.00,
        "surface_weight": 0.00,
        "needs_surface": False,
        "min_level_matches": 20,
        "min_surface_matches": 0,
        "min_prob": 0.55,
        "edge_bet": 0.06,
        "edge_strong": 0.10,
    },
}

CSV_FIELDS = [
    "pick_id", "status", "decision", "confidence", "reason",
    "date", "time", "gender", "level", "surface", "tournament", "round",
    "match", "pick", "opponent", "side",
    "odds", "implied_prob", "old_model_prob", "old_edge",
    "tle_model", "tle_prob", "tle_edge",
    "tle_min_level_matches", "tle_min_surface_matches",
    "stake", "stake_label", "best_bookmaker", "market_median_odds", "bookmakers_used",
    "player_key", "opponent_key", "player_canonical_key", "opponent_canonical_key",
    "player_last10_win_rate", "opponent_last10_win_rate", "last10_win_rate_diff",
    "h2h_matches", "h2h_player_wins", "h2h_opponent_wins",
    "created_at", "tle_created_at",
]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_str(x: Any) -> str:
    return str(x or "").strip()


def safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(str(x).strip())
    except Exception:
        return None
    return v if math.isfinite(v) else None


def safe_int(x: Any) -> int | None:
    v = safe_float(x)
    return int(v) if v is not None else None


def read_json_url_or_path(source: str | Path) -> Any:
    s = str(source)
    if s.startswith("http://") or s.startswith("https://"):
        req = urllib.request.Request(
            s,
            headers={
                "User-Agent": "TLE-elo-filter-predictions/1.0",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    path = Path(s)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def read_json_maybe_gz(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def find_ratings_path(explicit: Path | None) -> Path:
    if explicit:
        if explicit.exists():
            return explicit
        raise FileNotFoundError(f"Ratings path does not exist: {explicit}")
    for p in RATING_CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError("Missing ratings file. Tried: " + ", ".join(str(p) for p in RATING_CANDIDATES))


def get_players_from_ratings(data: Any) -> dict[str, dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("players"), dict):
        return data["players"]
    if isinstance(data, dict):
        maybe = {k: v for k, v in data.items() if isinstance(v, dict) and ("level" in v or "surface" in v)}
        if maybe:
            return maybe
    raise ValueError("Could not locate players in ratings JSON")


def read_api_mapping(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing API mapping: {path}")
    data = read_json_url_or_path(path)
    mapping = data.get("mapping", data) if isinstance(data, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in mapping.items():
        if isinstance(v, dict):
            out[str(k)] = v
        elif isinstance(v, str):
            out[str(k)] = {"status": "mapped", "sackmann_player_key": v}
        else:
            out[str(k)] = {"status": "unmapped"}
    return out


def normalize_gender(g: Any) -> str:
    s = safe_str(g).lower()
    if s in {"m", "men", "male", "atp"}:
        return "men"
    if s in {"w", "women", "female", "wta"}:
        return "women"
    return s


def normalize_surface(s: Any) -> str:
    x = safe_str(s).lower().replace("court", "").strip()
    return x if x in VALID_SURFACES else "unknown"


def normalize_level(level: Any, event_type: Any = None, qualification: Any = None) -> str:
    s = safe_str(level).lower().replace("-", "_").replace(" ", "_")
    ev = safe_str(event_type).lower()
    q = safe_str(qualification).lower() in {"1", "true", "yes", "y"}
    if q or "qualification" in ev or "qualifying" in ev:
        return "qualifying"
    if s in {"main_tour", "tour", "atp", "wta", "atp_wta"}:
        return "atp_wta"
    if s in {"grand_slam", "slam"}:
        return "grand_slam"
    if s in {"challenger", "itf"}:
        return s
    if "grand slam" in ev:
        return "grand_slam"
    if "challenger" in ev:
        return "challenger"
    if "itf" in ev:
        return "itf"
    if "atp" in ev or "wta" in ev:
        return "atp_wta"
    return s or "unknown"


def api_key(gender: str, player_id: Any) -> str | None:
    pid = safe_int(player_id)
    g = normalize_gender(gender)
    if pid is None or g not in {"men", "women"}:
        return None
    return f"{g}:api:{pid}"


def canonical_for_api(mapping: dict[str, dict[str, Any]], key: str | None) -> tuple[str | None, str]:
    if not key:
        return None, "missing_api_key"
    item = mapping.get(key)
    if not item:
        return None, "not_in_mapping"
    target = item.get("sackmann_player_key") or item.get("canonical_player_key") or item.get("target")
    status = safe_str(item.get("status")) or ("mapped" if target else "unmapped")
    return (safe_str(target), status) if target else (None, status)


def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


def rating_value(player: dict[str, Any] | None, layer: str, key: str) -> float:
    d = player.get(layer) if isinstance(player, dict) else None
    if isinstance(d, dict):
        v = safe_float(d.get(key))
        if v is not None:
            return v
    return RATING_INITIAL


def count_value(player: dict[str, Any] | None, layer: str, key: str) -> int:
    d = player.get(layer) if isinstance(player, dict) else None
    if isinstance(d, dict):
        v = safe_int(d.get(key))
        if v is not None:
            return v
    return 0


def grand_slam_component(player: dict[str, Any] | None) -> tuple[float, int]:
    gs_r = rating_value(player, "level", "grand_slam")
    tour_r = rating_value(player, "level", "atp_wta")
    gs_n = count_value(player, "level_matches", "grand_slam")
    tour_n = count_value(player, "level_matches", "atp_wta")
    if gs_n <= 0 and tour_n <= 0:
        return RATING_INITIAL, 0
    gs_w = gs_n + (8 if gs_n > 0 else 0)
    tour_w = tour_n + (12 if tour_n > 0 else 0)
    total = gs_w + tour_w
    return ((gs_r * gs_w + tour_r * tour_w) / total, gs_n + tour_n)


def level_component(player: dict[str, Any] | None, level: str) -> tuple[float, int]:
    if level == "grand_slam":
        return grand_slam_component(player)
    return rating_value(player, "level", level), count_value(player, "level_matches", level)


def blend_rating(level_r: float, surface_r: float, lw: float, sw: float) -> float:
    return (level_r * lw + surface_r * sw) / (lw + sw) if (lw + sw) > 0 else RATING_INITIAL


def extract_picks(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("picks", "value_picks", "predictions"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    raise ValueError("Expected tennis value predictions JSON as list or dict with picks")


def build_odds_context(payload: Any) -> dict[str, dict[str, Any]]:
    matches: list[Any] = []
    if isinstance(payload, dict):
        for key in ("matches", "fixtures", "events"):
            if isinstance(payload.get(key), list):
                matches = payload[key]
                break
    elif isinstance(payload, list):
        matches = payload
    out: dict[str, dict[str, Any]] = {}
    for m in matches:
        if not isinstance(m, dict):
            continue
        for key in (m.get("event_key"), m.get("event_id"), m.get("fixture_id")):
            if key is not None and safe_str(key):
                out[safe_str(key)] = m
    return out


def nested(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def pick_surface(pick: dict[str, Any], odds_context: dict[str, dict[str, Any]]) -> tuple[str, str]:
    s = normalize_surface(pick.get("surface"))
    if s != "unknown":
        return s, "pick"
    event_key = safe_str(pick.get("event_key") or pick.get("fixture_id") or pick.get("event_id"))
    ctx = odds_context.get(event_key, {})
    s = normalize_surface(ctx.get("surface"))
    if s != "unknown":
        return s, "odds"
    raw = ctx.get("raw_fixture") if isinstance(ctx.get("raw_fixture"), dict) else {}
    s = normalize_surface(raw.get("surface") or raw.get("event_surface"))
    if s != "unknown":
        return s, "raw_fixture"
    return "unknown", "missing"


def decide_pick(pick: dict[str, Any], *, players: dict[str, dict[str, Any]], api_mapping: dict[str, dict[str, Any]], odds_context: dict[str, dict[str, Any]]) -> dict[str, Any]:
    gender = normalize_gender(pick.get("gender"))
    level = normalize_level(pick.get("tour_level"), pick.get("event_type"), pick.get("qualification"))
    surface, surface_source = pick_surface(pick, odds_context)

    p_api = api_key(gender, pick.get("player_key"))
    o_api = api_key(gender, pick.get("opponent_key"))
    p_ckey, p_status = canonical_for_api(api_mapping, p_api)
    o_ckey, o_status = canonical_for_api(api_mapping, o_api)

    mapping_status = "mapped"
    if not p_ckey and not o_ckey:
        mapping_status = f"both_unmapped:{p_status}|{o_status}"
    elif not p_ckey:
        mapping_status = f"player_unmapped:{p_status}"
    elif not o_ckey:
        mapping_status = f"opponent_unmapped:{o_status}"

    odds = safe_float(pick.get("odds"))
    implied = safe_float(pick.get("implied_prob"))
    if implied is None and odds and odds > 0:
        implied = 1.0 / odds
    old_prob = safe_float(pick.get("model_prob"))
    old_edge = safe_float(pick.get("edge"))
    if old_edge is None and old_prob is not None and implied is not None:
        old_edge = old_prob - implied

    base = {
        "pick_id": safe_str(pick.get("pick_id")) or f"{safe_str(pick.get('fixture_id'))}:{safe_str(pick.get('player_key'))}",
        "event_key": pick.get("event_key"),
        "fixture_id": pick.get("fixture_id"),
        "status": "pending",
        "date": pick.get("date"),
        "time": pick.get("time"),
        "gender": gender,
        "level": level,
        "surface": surface,
        "surface_source": surface_source,
        "tournament": pick.get("tournament"),
        "round": pick.get("round"),
        "match": pick.get("match"),
        "pick": pick.get("player_name") or pick.get("bet"),
        "opponent": pick.get("opponent_name"),
        "side": pick.get("side"),
        "market_side": pick.get("market_side"),
        "odds": odds,
        "implied_prob": None if implied is None else round(implied, 8),
        "old_model_prob": old_prob,
        "old_edge": old_edge,
        "old_confidence": pick.get("confidence"),
        "old_quality_score": pick.get("quality_score"),
        "stake": pick.get("stake"),
        "stake_label": pick.get("stake_label"),
        "best_bookmaker": pick.get("best_bookmaker"),
        "market_median_odds": pick.get("market_median_odds"),
        "bookmakers_used": pick.get("bookmakers_used"),
        "favorite_type": pick.get("favorite_type"),
        "player_key": pick.get("player_key"),
        "opponent_key": pick.get("opponent_key"),
        "player_api_key": p_api,
        "opponent_api_key": o_api,
        "player_canonical_key": p_ckey,
        "opponent_canonical_key": o_ckey,
        "mapping_status": mapping_status,
        "player_last10_win_rate": nested(pick, "player_form", "last_10", "win_rate"),
        "opponent_last10_win_rate": nested(pick, "opponent_form", "last_10", "win_rate"),
        "h2h_matches": nested(pick, "h2h", "matches"),
        "h2h_player_wins": nested(pick, "h2h", "first_wins"),
        "h2h_opponent_wins": nested(pick, "h2h", "second_wins"),
        "created_at": pick.get("created_at"),
        "tle_created_at": now_utc_iso(),
    }

    pw = safe_float(base["player_last10_win_rate"])
    ow = safe_float(base["opponent_last10_win_rate"])
    base["last10_win_rate_diff"] = round(pw - ow, 6) if pw is not None and ow is not None else None

    def finish(decision: str, reason: str, *, confidence: str = "none", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        extra = extra or {}
        return {
            **base,
            "decision": decision,
            "confidence": confidence,
            "reason": reason,
            "tle_model": extra.get("tle_model"),
            "tle_prob": extra.get("tle_prob"),
            "tle_edge": extra.get("tle_edge"),
            "tle_min_level_matches": extra.get("tle_min_level_matches"),
            "tle_min_surface_matches": extra.get("tle_min_surface_matches"),
            "tle_player_level_rating": extra.get("tle_player_level_rating"),
            "tle_opponent_level_rating": extra.get("tle_opponent_level_rating"),
            "tle_player_surface_rating": extra.get("tle_player_surface_rating"),
            "tle_opponent_surface_rating": extra.get("tle_opponent_surface_rating"),
        }

    if level not in LEVEL_RULES:
        return finish("NO_BET", "unsupported_level")
    rule = LEVEL_RULES[level]
    model = rule["model"]

    if mapping_status != "mapped":
        return finish("NO_BET", "unmapped_player", extra={"tle_model": model})
    if implied is None:
        return finish("NO_BET", "missing_odds", extra={"tle_model": model})

    player = players.get(p_ckey or "")
    opponent = players.get(o_ckey or "")
    if not isinstance(player, dict) or not isinstance(opponent, dict):
        return finish("NO_BET", "missing_rating_player", extra={"tle_model": model})

    if rule["needs_surface"] and surface not in VALID_SURFACES:
        return finish("NO_BET", "unknown_surface", extra={"tle_model": model})

    p_level_r, p_level_n = level_component(player, level)
    o_level_r, o_level_n = level_component(opponent, level)
    p_surface_r = rating_value(player, "surface", surface) if surface in VALID_SURFACES else RATING_INITIAL
    o_surface_r = rating_value(opponent, "surface", surface) if surface in VALID_SURFACES else RATING_INITIAL
    p_surface_n = count_value(player, "surface_matches", surface) if surface in VALID_SURFACES else 0
    o_surface_n = count_value(opponent, "surface_matches", surface) if surface in VALID_SURFACES else 0

    lw = float(rule["level_weight"])
    sw = float(rule["surface_weight"])
    p_rating = blend_rating(p_level_r, p_surface_r, lw, sw) if sw > 0 else p_level_r
    o_rating = blend_rating(o_level_r, o_surface_r, lw, sw) if sw > 0 else o_level_r

    prob = expected(p_rating, o_rating)
    edge = prob - implied
    min_level = min(p_level_n, o_level_n)
    min_surface = min(p_surface_n, o_surface_n)

    extra = {
        "tle_model": model,
        "tle_prob": round(prob, 8),
        "tle_edge": round(edge, 8),
        "tle_min_level_matches": min_level,
        "tle_min_surface_matches": min_surface if rule["needs_surface"] else "",
        "tle_player_level_rating": round(p_level_r, 6),
        "tle_opponent_level_rating": round(o_level_r, 6),
        "tle_player_surface_rating": round(p_surface_r, 6) if rule["needs_surface"] else "",
        "tle_opponent_surface_rating": round(o_surface_r, 6) if rule["needs_surface"] else "",
    }

    if min_level < int(rule["min_level_matches"]):
        return finish("NO_BET", "min_level_not_met", extra=extra)
    if rule["needs_surface"] and min_surface < int(rule["min_surface_matches"]):
        return finish("NO_BET", "min_surface_not_met", extra=extra)
    if prob < float(rule["min_prob"]):
        return finish("NO_BET", "elo_disagrees_or_prob_too_low", extra=extra)
    if edge < float(rule["edge_bet"]):
        return finish("NO_BET", "elo_edge_too_low", extra=extra)

    if edge >= float(rule["edge_strong"]):
        return finish("STRONG_BET", "pass_strong_edge", confidence="strong", extra=extra)
    return finish("BET", "pass_edge", confidence="normal", extra=extra)


def load_existing_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return read_json_url_or_path(path)
    except Exception:
        return default


def payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("picks"), list):
        return [x for x in payload["picks"] if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def write_active_csv(rows: list[dict[str, Any]]) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    with ACTIVE_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in CSV_FIELDS})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--picks-source", default=DEFAULT_PICKS_URL)
    parser.add_argument("--odds-source", default=DEFAULT_ODDS_URL)
    parser.add_argument("--ratings-path", type=Path, default=None)
    parser.add_argument("--api-mapping", type=Path, default=API_MAPPING_JSON)
    parser.add_argument("--include-bet", action="store_true", default=True)
    args = parser.parse_args()

    picks_payload = read_json_url_or_path(args.picks_source)
    raw_picks = extract_picks(picks_payload)

    odds_context = {}
    odds_error = None
    try:
        odds_payload = read_json_url_or_path(args.odds_source)
        odds_context = build_odds_context(odds_payload)
    except Exception as exc:
        odds_error = str(exc)

    ratings_path = find_ratings_path(args.ratings_path)
    ratings = read_json_maybe_gz(ratings_path)
    players = get_players_from_ratings(ratings)
    api_mapping = read_api_mapping(args.api_mapping)

    evaluated = [decide_pick(p, players=players, api_mapping=api_mapping, odds_context=odds_context) for p in raw_picks]
    new_active = [r for r in evaluated if r["decision"] in {"BET", "STRONG_BET"}]

    existing_predictions_payload = load_existing_json(PREDICTIONS_JSON, {"picks": []})
    existing_active = [r for r in payload_items(existing_predictions_payload) if safe_str(r.get("status")).lower() == "pending"]

    # Keep old pending picks until settle removes them; add only new pick_ids.
    active_by_id = {safe_str(r.get("pick_id")): r for r in existing_active if safe_str(r.get("pick_id"))}
    added = 0
    for r in new_active:
        pid = safe_str(r.get("pick_id"))
        if pid and pid not in active_by_id:
            active_by_id[pid] = r
            added += 1

    active = list(active_by_id.values())
    active.sort(key=lambda r: (safe_str(r.get("date")), safe_str(r.get("time")), safe_str(r.get("tournament")), safe_str(r.get("match"))))

    results_payload = load_existing_json(RESULTS_JSON, {"picks": []})
    results_items = payload_items(results_payload)
    results_by_id = {safe_str(r.get("pick_id")): r for r in results_items if safe_str(r.get("pick_id"))}
    for r in new_active:
        pid = safe_str(r.get("pick_id"))
        if pid and pid not in results_by_id:
            results_by_id[pid] = r

    results_all = list(results_by_id.values())
    results_all.sort(key=lambda r: (safe_str(r.get("date")), safe_str(r.get("time")), safe_str(r.get("tournament")), safe_str(r.get("match"))))

    predictions_payload = {
        "generated_at": now_utc_iso(),
        "model": "TLE Elo Filter v1",
        "source": {
            "picks_source": args.picks_source,
            "odds_source": args.odds_source,
            "ratings_path": str(ratings_path),
            "api_mapping": str(args.api_mapping),
            "odds_error": odds_error,
        },
        "summary": {
            "active_picks": len(active),
            "new_added": added,
            "evaluated_today": len(evaluated),
            "today_bet_candidates": len(new_active),
        },
        "picks": active,
    }
    results_payload_out = {
        "generated_at": now_utc_iso(),
        "model": "TLE Elo Filter v1",
        "summary": {
            "total_tracked": len(results_all),
            "pending": sum(1 for r in results_all if safe_str(r.get("status")).lower() == "pending"),
            "settled": sum(1 for r in results_all if safe_str(r.get("status")).lower() in {"win", "loss", "void", "push", "settled"}),
        },
        "picks": results_all,
    }

    write_json(PREDICTIONS_JSON, predictions_payload)
    write_json(RESULTS_JSON, results_payload_out)
    write_active_csv(active)

    report = {
        "status": "ok",
        "generated_at": now_utc_iso(),
        "input_picks": len(raw_picks),
        "evaluated": len(evaluated),
        "new_active_candidates": len(new_active),
        "new_added_to_predictions": added,
        "active_predictions_total": len(active),
        "decision_counts_today": dict(sorted(Counter(r["decision"] for r in evaluated).items())),
        "reason_counts_today": dict(sorted(Counter(r["reason"] for r in evaluated).items())),
        "by_level_today": dict(sorted(Counter(r["level"] for r in evaluated).items())),
        "outputs": {
            "predictions_json": str(PREDICTIONS_JSON),
            "results_json": str(RESULTS_JSON),
            "active_csv": str(ACTIVE_CSV),
            "report_json": str(REPORT_JSON),
        },
        "notes": [
            "This workflow only filters picks produced by the tennis value model.",
            "Only BET and STRONG_BET are stored as active predictions.",
            "Existing pending predictions are kept until the settle workflow updates/removes them.",
            "Form and H2H are informational only in v1.",
        ],
    }
    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import re
import statistics
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# =========================
# CONFIG
# =========================

API_KEY = (
    os.getenv("TENNIS_API_KEY")
    or os.getenv("API_KEY")
    or os.getenv("API_TENNIS_KEY")
    or os.getenv("APITENNIS_KEY")
    or os.getenv("API_TENNIS_API_KEY")
    or os.getenv("TENNIS_VALUE_API_KEY")
)

BASE_URL = "https://api.api-tennis.com/tennis/"
TZ_NAME = "Europe/Ljubljana"
API_SLEEP_SECONDS = 0.35
REQUEST_TIMEOUT = 45
DEFAULT_MIN_START_MINUTES = 30

API_MAPPING_JSON = Path("data/metadata/api_tennis/player_mapping.json")
SURFACE_MAP_JSON = Path("data/metadata/tournament_surface_map.json")
API_TOURNAMENTS_METADATA_JSON = Path("data/raw/api_tennis/metadata/get_tournaments.json")

RATING_CANDIDATES = [
    Path("data/ratings/tle_player_ratings.json.gz"),
    Path("data/ratings/tle_player_ratings.json"),
    Path("data/ratings/player_ratings.json.gz"),
    Path("data/ratings/player_ratings.json"),
    Path("data/ratings/ratings.json.gz"),
    Path("data/ratings/ratings.json"),
]

BASE_DIR = Path("data/elo_standalone")
REPORT_DIR = Path("data/reports/elo_standalone")
PREDICTIONS_JSON = BASE_DIR / "predictions.json"
RESULTS_JSON = BASE_DIR / "results.json"
ACTIVE_CSV = BASE_DIR / "active_predictions.csv"
SCAN_CSV = REPORT_DIR / "scan_diagnostics.csv"
REPORT_JSON = REPORT_DIR / "predictions_report.json"
UNRESOLVED_SURFACE_JSON = REPORT_DIR / "unresolved_surface_report.json"

RATING_INITIAL = 1500.0
VALID_SURFACES = {"hard", "clay", "grass", "carpet"}

# Bootstrap map used only if data/metadata/tournament_surface_map.json does not exist yet.
DEFAULT_TOURNAMENT_SURFACE_MAP = {
    "atp london": "grass",
    "london atp": "grass",
    "queen": "grass",
    "queens": "grass",
    "atp halle": "grass",
    "halle atp": "grass",
    "wta berlin": "grass",
    "berlin wta": "grass",
    "wta nottingham": "grass",
    "nottingham wta": "grass",
    "challenger men singles nottingham": "grass",
    "challenger men singles parma": "clay",
    "challenger men singles poznan": "clay",
    "challenger men singles royan": "clay",
    "challenger women singles brescia": "clay",
    "challenger women singles figueira da foz": "hard",
    "challenger men singles dublin": "hard",
    "challenger men singles asuncion": "clay",
    "australian open": "hard",
    "roland garros": "clay",
    "french open": "clay",
    "wimbledon": "grass",
    "us open": "hard",
}

# Average odds rules: use average of non-outlier bookmaker odds, not the single best price.
MIN_BOOKMAKERS_EACH_SIDE = 3
MAX_ODDS_DEVIATION_FROM_MEDIAN = 0.12
MIN_CLEAN_BOOKMAKERS = 3
ODDS_MIN = 1.30
ODDS_MAX = 4.50

# Final v1 TLE model selector. Only qualifying min_level_matches changed 15 -> 10.
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
        "min_level_matches": 15,
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
        "min_level_matches": 10,
        "min_surface_matches": 0,
        "min_prob": 0.53,
        "edge_bet": 0.04,
        "edge_strong": 0.08,
    },
}

CSV_FIELDS = [
    "pick_id", "status", "decision", "confidence", "reason",
    "date", "time", "gender", "level", "surface", "surface_source",
    "raw_event_type_type", "raw_event_type_key", "raw_event_type", "raw_event_name",
    "raw_event_country_name", "raw_league_name", "raw_competition_name",
    "raw_tournament_name", "raw_tournament_round", "context_text",
    "tournament", "round", "match", "pick", "opponent", "side", "market_side",
    "odds", "avg_odds", "median_odds", "best_odds", "best_bookmaker", "implied_prob",
    "tle_model", "tle_prob", "tle_edge", "tle_min_level_matches", "tle_min_surface_matches",
    "bookmakers_raw", "bookmakers_clean", "outlier_bookmakers_removed",
    "stake", "stake_label",
    "player_key", "opponent_key", "player_api_key", "opponent_api_key",
    "player_canonical_key", "opponent_canonical_key", "mapping_status",
    "player_last5_win_rate", "opponent_last5_win_rate", "last5_win_rate_diff",
    "player_last10_win_rate", "opponent_last10_win_rate", "last10_win_rate_diff",
    "player_last10_set_diff_avg", "opponent_last10_set_diff_avg", "last10_set_diff_diff",
    "player_last10_game_diff_avg", "opponent_last10_game_diff_avg", "last10_game_diff_diff",
    "h2h_matches", "h2h_player_wins", "h2h_opponent_wins",
    "form_support", "h2h_support", "fatigue_flag", "created_at",
]

TOURNAMENT_SURFACE_MAP: dict[str, str] = {}
API_TOURNAMENT_SURFACE_BY_KEY: dict[str, str] = {}

# =========================
# HELPERS
# =========================


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_now() -> datetime:
    return datetime.now(ZoneInfo(TZ_NAME))


def local_today() -> datetime.date:
    return local_now().date()


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


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json_path(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return default
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def read_json_maybe_gz(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


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
    data = read_json_path(path, {})
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
    x = safe_str(s).lower()
    if not x:
        return "unknown"
    x = x.replace("_", " ").replace("-", " ")
    x = re.sub(r"\s+", " ", x).strip()
    if x in VALID_SURFACES:
        return x

    aliases = {
        "hardcourt": "hard", "hard court": "hard", "outdoor hard": "hard", "indoor hard": "hard",
        "synthetic hard": "hard", "cement": "hard", "acrylic": "hard", "decoturf": "hard",
        "plexicushion": "hard", "greenset": "hard",
        "claycourt": "clay", "clay court": "clay", "outdoor clay": "clay", "indoor clay": "clay",
        "red clay": "clay", "green clay": "clay", "har tru": "clay",
        "grasscourt": "grass", "grass court": "grass", "lawn": "grass",
        "carpet court": "carpet", "indoor carpet": "carpet",
    }
    if x in aliases:
        return aliases[x]

    # Handles API-Tennis values such as "Hard (Indoor)".
    if re.search(r"\bhard\b", x):
        return "hard"
    if re.search(r"\bclay\b", x):
        return "clay"
    if re.search(r"\bgrass\b", x) or re.search(r"\blawn\b", x):
        return "grass"
    if re.search(r"\bcarpet\b", x):
        return "carpet"
    return "unknown"


SURFACE_KEY_HINTS = ("surface", "sourface", "court", "ground", "terrain", "floor", "belag", "podlaga")


def iter_nested_api_values(obj: Any, prefix: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = safe_str(k)
            next_prefix = f"{prefix}.{key}" if prefix else key
            yield next_prefix, v
            yield from iter_nested_api_values(v, next_prefix)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            next_prefix = f"{prefix}[{i}]"
            yield from iter_nested_api_values(v, next_prefix)


def infer_surface_from_api_payload(match: dict[str, Any]) -> tuple[str, str]:
    for key, value in iter_nested_api_values(match):
        lk = key.lower()
        if any(hint in lk for hint in SURFACE_KEY_HINTS):
            surf = normalize_surface(value)
            if surf != "unknown":
                return surf, f"api_field:{key}"
    for key, value in iter_nested_api_values(match):
        if isinstance(value, (str, int, float)):
            surf = normalize_surface(value)
            if surf != "unknown":
                return surf, f"api_text:{key}"
    return "unknown", "missing"


def normalize_surface_map_key(s: Any) -> str:
    x = safe_str(s).lower()
    x = x.replace("-", " ").replace("_", " ")
    x = re.sub(r"\s+", " ", x).strip()
    return x


def read_tournament_surface_map(path: Path = SURFACE_MAP_JSON) -> dict[str, str]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        clean_default = {
            normalize_surface_map_key(k): normalize_surface(v)
            for k, v in DEFAULT_TOURNAMENT_SURFACE_MAP.items()
            if normalize_surface(v) in VALID_SURFACES
        }
        path.write_text(json.dumps(clean_default, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return clean_default

    raw = read_json_path(path, {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = normalize_surface_map_key(k)
        surf = normalize_surface(v)
        if key and surf in VALID_SURFACES:
            out[key] = surf
    return out


def write_tournament_surface_map(path: Path = SURFACE_MAP_JSON) -> None:
    clean = {
        normalize_surface_map_key(k): normalize_surface(v)
        for k, v in TOURNAMENT_SURFACE_MAP.items()
        if normalize_surface_map_key(k) and normalize_surface(v) in VALID_SURFACES
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _extract_tournament_rows(payload: Any) -> list[dict[str, Any]]:
    """Extract rows from either raw API response or 06a wrapped payload.

    06a writes:
      {
        "schema_version": 1,
        "source": "api_tennis",
        "method": "get_tournaments",
        "response": {"success": 1, "result": [...]}
      }

    Debug/manual scripts may write the raw API response directly:
      {"success": 1, "result": [...]}

    Keep both formats supported.
    """
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    # 06a wrapped file: data/raw/api_tennis/metadata/get_tournaments.json
    response = payload.get("response")
    if isinstance(response, dict):
        result = response.get("result")
        if isinstance(result, list):
            return [x for x in result if isinstance(x, dict)]
        if isinstance(result, dict):
            return [x for x in result.values() if isinstance(x, dict)]

    # Raw API response or future simplified cache format.
    result = payload.get("result", payload.get("tournaments"))
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        return [x for x in result.values() if isinstance(x, dict)]

    return []


def read_api_tournament_surface_map(path: Path = API_TOURNAMENTS_METADATA_JSON) -> dict[str, str]:
    """Read 06a output: data/raw/api_tennis/metadata/get_tournaments.json.

    API-Tennis uses typo field `tournament_sourface`; keep fallbacks for future variants.
    Output is tournament_key -> normalized surface.
    """
    payload = read_json_path(path, None)
    rows = _extract_tournament_rows(payload)
    out: dict[str, str] = {}
    for t in rows:
        key = safe_str(t.get("tournament_key"))
        raw_surface = (
            t.get("tournament_sourface")
            or t.get("tournament_surface")
            or t.get("surface")
            or t.get("court_surface")
            or t.get("court_type")
        )
        surf = normalize_surface(raw_surface)
        if key and surf in VALID_SURFACES:
            out[key] = surf
    return out


def infer_surface(match: dict[str, Any], context: dict[str, Any] | None = None) -> tuple[str, str]:
    # 1) Direct and nested get_fixtures payload first.
    direct_fields = [
        ("event_surface", match.get("event_surface")),
        ("surface", match.get("surface")),
        ("court_surface", match.get("court_surface")),
        ("tournament_surface", match.get("tournament_surface")),
        ("tournament_sourface", match.get("tournament_sourface")),
        ("event_court_type", match.get("event_court_type")),
    ]
    for key, value in direct_fields:
        surf = normalize_surface(value)
        if surf != "unknown":
            return surf, f"api_field:{key}"

    surf, source = infer_surface_from_api_payload(match)
    if surf != "unknown":
        return surf, source

    # 2) API tournament metadata from 06a output by tournament_key.
    tournament_key = safe_str(match.get("tournament_key"))
    if tournament_key and tournament_key in API_TOURNAMENT_SURFACE_BY_KEY:
        return API_TOURNAMENT_SURFACE_BY_KEY[tournament_key], "api_tournaments_metadata:tournament_sourface"

    # 3) Sometimes surface is hidden in raw text/context.
    if context is None:
        context = tournament_context(match)
    text = safe_str(context.get("context_text")).lower()
    context_surf = normalize_surface(text)
    if context_surf != "unknown":
        return context_surf, "context_text"

    # 4) Known local tournament map fallback.
    map_text = normalize_surface_map_key(text)
    for needle, surf in TOURNAMENT_SURFACE_MAP.items():
        if needle and needle in map_text:
            return surf, "tournament_surface_map"

    return "unknown", "missing"


def learn_surface_from_match(match: dict[str, Any], context: dict[str, Any], surface: str, source: str) -> None:
    if surface not in VALID_SURFACES:
        return
    if source not in {"context_text"} and not source.startswith("api_"):
        return
    text = normalize_surface_map_key(context.get("context_text"))
    if not text or len(text) < 5:
        return
    if text not in TOURNAMENT_SURFACE_MAP:
        TOURNAMENT_SURFACE_MAP[text] = surface


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
    text = f"{s} {ev}".lower()
    if "grand slam" in text:
        return "grand_slam"
    if "challenger" in text:
        return "challenger"
    if "itf" in text:
        return "itf"
    if "atp" in text or "wta" in text:
        return "atp_wta"
    return s or "unknown"


def tournament_context(match: dict[str, Any]) -> dict[str, Any]:
    text_parts = [
        match.get("event_type_type"), match.get("event_type_key"), match.get("event_type"),
        match.get("event_name"), match.get("event_country_name"), match.get("event_country_key"),
        match.get("tournament_name"), match.get("tournament_round"), match.get("tournament_key"),
        match.get("league_name"), match.get("league"), match.get("competition_name"),
    ]
    text = " ".join(safe_str(x) for x in text_parts if safe_str(x)).lower()
    compact = text.replace("-", " ").replace("_", " ")

    qualification = safe_str(match.get("event_qualification")).lower() == "true"
    if "qualif" in compact or "qualification" in compact:
        qualification = True

    if re.search(r"\b(women|woman|female|wta)\b", compact) or re.search(r"\bw(15|35|50|75|100)\b", compact):
        gender = "women"
    elif re.search(r"\b(men|man|male|atp)\b", compact) or re.search(r"\bm(15|25)\b", compact):
        gender = "men"
    else:
        gender = "unknown"

    if "grand slam" in compact or "australian open" in compact or "roland garros" in compact or "wimbledon" in compact or "us open" in compact:
        level = "grand_slam"
    elif "challenger" in compact or re.search(r"\bch(?:allenger)?\b", compact):
        level = "challenger"
    elif "itf" in compact or re.search(r"\bm(15|25)\b", compact) or re.search(r"\bw(15|35|50|75|100)\b", compact):
        level = "itf"
    elif "atp" in compact or "wta" in compact:
        level = "atp_wta"
    else:
        level = "unknown"

    # Keep old, backtest-aligned behavior: qualification is its own model/rating bucket.
    if qualification:
        level = "qualifying"

    return {"gender": gender, "level": level, "qualification": qualification, "context_text": compact}


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


def build_pick_id(event_key: Any, side: str, player_key: Any, avg_odds: Any) -> str:
    raw = f"tle_standalone_v1|{event_key}|{side}|{player_key}|{avg_odds}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

# =========================
# API
# =========================


def api_call(params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
    if not API_KEY:
        raise RuntimeError(
            "Missing API key. Add one of these repository secrets and expose it in the workflow env: "
            "TENNIS_API_KEY, API_KEY, API_TENNIS_KEY, APITENNIS_KEY, API_TENNIS_API_KEY, TENNIS_VALUE_API_KEY."
        )
    p = {k: v for k, v in params.items() if v is not None}
    p["APIkey"] = API_KEY
    url = BASE_URL + "?" + urllib.parse.urlencode(p)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TLE-standalone-elo-scanner/1.0"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return {}


def fetch_fixtures_for_date(date_s: str) -> list[dict[str, Any]]:
    data = api_call({"method": "get_fixtures", "date_start": date_s, "date_stop": date_s})
    time.sleep(API_SLEEP_SECONDS)
    result = data.get("result") if data.get("success") == 1 else []
    return result if isinstance(result, list) else []


def fetch_odds(event_key: Any) -> dict[str, Any]:
    data = api_call({"method": "get_odds", "event_key": event_key})
    time.sleep(API_SLEEP_SECONDS)
    result = data.get("result") if data.get("success") == 1 else {}
    if not isinstance(result, dict):
        return {}
    return result.get(str(event_key)) or result.get(event_key) or {}


def fetch_h2h(first_player_key: Any, second_player_key: Any) -> dict[str, Any]:
    data = api_call({"method": "get_H2H", "first_player_key": first_player_key, "second_player_key": second_player_key})
    time.sleep(API_SLEEP_SECONDS)
    result = data.get("result") if data.get("success") == 1 else {}
    return result if isinstance(result, dict) else {}

# =========================
# ODDS
# =========================


def trim_outlier_odds(items: list[dict[str, Any]]) -> dict[str, Any]:
    values = [safe_float(x.get("odds")) for x in items]
    values = [v for v in values if v is not None and v > 1.0]
    if not values:
        return {"avg_odds": None, "median_odds": None, "clean": [], "removed": []}
    med = float(statistics.median(values))
    clean, removed = [], []
    for item in items:
        v = safe_float(item.get("odds"))
        if v is None or v <= 1.0:
            continue
        gap = abs(v - med) / med if med else 999
        (clean if gap <= MAX_ODDS_DEVIATION_FROM_MEDIAN else removed).append(item)
    if len(clean) < MIN_CLEAN_BOOKMAKERS:
        clean, removed = items, []
    clean_values = [safe_float(x.get("odds")) for x in clean]
    clean_values = [v for v in clean_values if v is not None and v > 1.0]
    return {
        "avg_odds": round(sum(clean_values) / len(clean_values), 4) if clean_values else None,
        "median_odds": round(float(statistics.median(clean_values)), 4) if clean_values else None,
        "clean": clean,
        "removed": removed,
    }


def parse_home_away_odds(odds_blob: dict[str, Any]) -> dict[str, Any] | None:
    market = odds_blob.get("Home/Away")
    if not isinstance(market, dict):
        return None
    out = {}
    for key, label in [("Home", "home"), ("Away", "away")]:
        raw = market.get(key) or {}
        if not isinstance(raw, dict):
            return None
        items = []
        for book, odd in raw.items():
            v = safe_float(odd)
            if v and v > 1.0:
                items.append({"bookmaker": str(book), "odds": v})
        if len(items) < MIN_BOOKMAKERS_EACH_SIDE:
            return None
        trimmed = trim_outlier_odds(items)
        clean = trimmed["clean"]
        if len(clean) < MIN_CLEAN_BOOKMAKERS or trimmed["avg_odds"] is None:
            return None
        best = max(clean, key=lambda x: x["odds"])
        out[label] = {
            "avg_odds": trimmed["avg_odds"],
            "median_odds": trimmed["median_odds"],
            "best_odds": round(best["odds"], 4),
            "best_bookmaker": best["bookmaker"],
            "bookmakers_raw": len(items),
            "bookmakers_clean": len(clean),
            "outliers_removed": len(trimmed["removed"]),
            "raw": items,
            "clean": clean,
            "removed": trimmed["removed"],
        }
    return out

# =========================
# RATINGS / MODEL
# =========================


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


def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


def blend_rating(level_r: float, surface_r: float, lw: float, sw: float) -> float:
    return (level_r * lw + surface_r * sw) / (lw + sw) if (lw + sw) > 0 else RATING_INITIAL


def model_prob_for_side(
    player: dict[str, Any] | None,
    opponent: dict[str, Any] | None,
    *,
    level: str,
    surface: str,
    rule: dict[str, Any],
) -> dict[str, Any]:
    p_lr, p_ln = level_component(player, level)
    o_lr, o_ln = level_component(opponent, level)
    p_sr = rating_value(player, "surface", surface) if surface in VALID_SURFACES else RATING_INITIAL
    o_sr = rating_value(opponent, "surface", surface) if surface in VALID_SURFACES else RATING_INITIAL
    p_sn = count_value(player, "surface_matches", surface) if surface in VALID_SURFACES else 0
    o_sn = count_value(opponent, "surface_matches", surface) if surface in VALID_SURFACES else 0
    lw, sw = float(rule["level_weight"]), float(rule["surface_weight"])
    p_r = blend_rating(p_lr, p_sr, lw, sw) if sw > 0 else p_lr
    o_r = blend_rating(o_lr, o_sr, lw, sw) if sw > 0 else o_lr
    return {
        "prob": expected(p_r, o_r),
        "min_level_matches": min(p_ln, o_ln),
        "min_surface_matches": min(p_sn, o_sn),
        "player_level_rating": p_lr,
        "opponent_level_rating": o_lr,
        "player_surface_rating": p_sr,
        "opponent_surface_rating": o_sr,
    }

# =========================
# FORM / H2H INFO ONLY
# =========================


def get_match_winner_side(match: dict[str, Any], player_key: Any) -> bool | None:
    winner = match.get("event_winner")
    first_key = safe_int(match.get("first_player_key"))
    second_key = safe_int(match.get("second_player_key"))
    pkey = safe_int(player_key)
    if winner == "First Player":
        return first_key == pkey
    if winner == "Second Player":
        return second_key == pkey
    return None


def parse_final_sets(match: dict[str, Any], player_key: Any) -> tuple[int, int, int, int]:
    first_key = safe_int(match.get("first_player_key"))
    second_key = safe_int(match.get("second_player_key"))
    pkey = safe_int(player_key)
    scores = match.get("scores") or []
    sf = sa = gf = ga = 0
    for s in scores:
        a = safe_int(s.get("score_first")) or 0
        b = safe_int(s.get("score_second")) or 0
        if pkey == first_key:
            x, y = a, b
        elif pkey == second_key:
            x, y = b, a
        else:
            continue
        gf += x
        ga += y
        if x > y:
            sf += 1
        elif y > x:
            sa += 1
    return sf, sa, gf, ga


def normalize_player_results(raw_results: Any, player_key: Any, current_event_key: Any = None) -> list[dict[str, Any]]:
    rows = []
    if not isinstance(raw_results, list):
        return rows
    for match in raw_results:
        if safe_str(match.get("event_status")).lower() != "finished":
            continue
        if current_event_key and safe_str(match.get("event_key")) == safe_str(current_event_key):
            continue
        won = get_match_winner_side(match, player_key)
        if won is None:
            continue
        sf, sa, gf, ga = parse_final_sets(match, player_key)
        if sf + sa <= 0:
            continue
        rows.append({
            "event_key": match.get("event_key"),
            "date": match.get("event_date") or "",
            "won": bool(won),
            "set_diff": sf - sa,
            "game_diff": gf - ga,
        })
    rows.sort(key=lambda x: x.get("date") or "", reverse=True)
    return rows


def form_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    if total <= 0:
        return {"matches": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "set_diff_avg": 0.0, "game_diff_avg": 0.0, "fatigue_matches_7d": 0, "fatigue_matches_3d": 0}
    wins = sum(1 for r in rows if r["won"])
    today = local_today()
    f7 = f3 = 0
    for r in rows:
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            days = (today - d).days
            if 0 <= days <= 7:
                f7 += 1
            if 0 <= days <= 3:
                f3 += 1
        except Exception:
            pass
    return {
        "matches": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": round(wins / total, 4),
        "set_diff_avg": round(sum(r["set_diff"] for r in rows) / total, 4),
        "game_diff_avg": round(sum(r["game_diff"] for r in rows) / total, 4),
        "fatigue_matches_7d": f7,
        "fatigue_matches_3d": f3,
    }


def h2h_summary(h2h_rows: Any, first_key: Any, second_key: Any) -> dict[str, Any]:
    first_wins = second_wins = usable = 0
    if not isinstance(h2h_rows, list):
        h2h_rows = []
    for match in h2h_rows:
        if safe_str(match.get("event_status")).lower() != "finished":
            continue
        winner = match.get("event_winner")
        first_player_key = safe_int(match.get("first_player_key"))
        second_player_key = safe_int(match.get("second_player_key"))
        if winner == "First Player":
            winner_key = first_player_key
        elif winner == "Second Player":
            winner_key = second_player_key
        else:
            continue
        if winner_key == safe_int(first_key):
            first_wins += 1
            usable += 1
        elif winner_key == safe_int(second_key):
            second_wins += 1
            usable += 1
    return {"matches": usable, "first_wins": first_wins, "second_wins": second_wins}


def support_labels(*, selected_side: str, first_form: dict[str, Any], second_form: dict[str, Any], h2h: dict[str, Any]) -> dict[str, str]:
    if selected_side == "home":
        p_form, o_form = first_form, second_form
        p_h2h, o_h2h = h2h["first_wins"], h2h["second_wins"]
    else:
        p_form, o_form = second_form, first_form
        p_h2h, o_h2h = h2h["second_wins"], h2h["first_wins"]

    form_diff = (safe_float(p_form.get("win_rate")) or 0) - (safe_float(o_form.get("win_rate")) or 0)
    if form_diff >= 0.15:
        form_support = "FORM_SUPPORTS_PICK"
    elif form_diff <= -0.15:
        form_support = "FORM_AGAINST_PICK"
    else:
        form_support = "FORM_NEUTRAL"

    if h2h.get("matches", 0) >= 3:
        if p_h2h > o_h2h:
            h2h_support = "H2H_SUPPORTS_PICK"
        elif p_h2h < o_h2h:
            h2h_support = "H2H_AGAINST_PICK"
        else:
            h2h_support = "H2H_NEUTRAL"
    else:
        h2h_support = "H2H_TOO_SMALL"

    fatigue_gap = (safe_int(p_form.get("fatigue_matches_3d")) or 0) - (safe_int(o_form.get("fatigue_matches_3d")) or 0)
    if fatigue_gap >= 2:
        fatigue = "FATIGUE_RISK_PICK"
    elif fatigue_gap <= -2:
        fatigue = "FATIGUE_EDGE_PICK"
    else:
        fatigue = "FATIGUE_NEUTRAL"
    return {"form_support": form_support, "h2h_support": h2h_support, "fatigue_flag": fatigue}

# =========================
# SCANNER
# =========================


def is_doubles_or_team_match(match: dict[str, Any]) -> bool:
    text = " ".join([
        safe_str(match.get("event_type_type")),
        safe_str(match.get("event_type_key")),
        safe_str(match.get("tournament_name")),
        safe_str(match.get("event_first_player")),
        safe_str(match.get("event_second_player")),
    ]).lower()
    if "doubles" in text or "double" in text:
        return True
    if "teams" in text or "team" in text:
        return True
    first_name = safe_str(match.get("event_first_player"))
    second_name = safe_str(match.get("event_second_player"))
    return any(token in first_name or token in second_name for token in ["/", " / ", " & "])


def is_fixture_eligible(match: dict[str, Any]) -> tuple[bool, str]:
    status = safe_str(match.get("event_status")).lower()
    if status in {"finished", "cancelled", "postponed", "retired", "walkover"}:
        return False, f"status_{status}"
    if safe_str(match.get("event_live")) == "1":
        return False, "live"
    if is_doubles_or_team_match(match):
        return False, "doubles_or_team"
    if not match.get("event_key"):
        return False, "missing_event_key"
    if not match.get("first_player_key") or not match.get("second_player_key"):
        return False, "missing_player_key"
    if not match.get("event_first_player") or not match.get("event_second_player"):
        return False, "missing_player_name"
    return True, ""


def match_datetime(match: dict[str, Any]) -> tuple[str, str]:
    date_s = safe_str(match.get("event_date"))
    time_s = safe_str(match.get("event_time")) or "00:00"
    return date_s, time_s


def fixture_start_datetime(match: dict[str, Any]) -> datetime | None:
    date_s, time_s = match_datetime(match)
    if not date_s:
        return None
    try:
        return datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(TZ_NAME))
    except ValueError:
        try:
            return datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=ZoneInfo(TZ_NAME))
        except ValueError:
            return None


def starts_after_min_lead(match: dict[str, Any], min_start_minutes: int) -> bool:
    if min_start_minutes <= 0:
        return True
    start_dt = fixture_start_datetime(match)
    if start_dt is None:
        return True
    now_local = datetime.now(ZoneInfo(TZ_NAME))
    return start_dt >= now_local + timedelta(minutes=min_start_minutes)


def evaluate_side(
    *,
    match: dict[str, Any],
    side: str,
    odds_info: dict[str, Any],
    selected_key: int,
    selected_name: str,
    opponent_key: int,
    opponent_name: str,
    context: dict[str, Any],
    surface: str,
    surface_source: str,
    players: dict[str, dict[str, Any]],
    api_mapping: dict[str, dict[str, Any]],
    first_form5: dict[str, Any],
    second_form5: dict[str, Any],
    first_form10: dict[str, Any],
    second_form10: dict[str, Any],
    h2h: dict[str, Any],
) -> dict[str, Any]:
    gender = context["gender"]
    level = context["level"]
    p_api = api_key(gender, selected_key)
    o_api = api_key(gender, opponent_key)
    p_ckey, p_status = canonical_for_api(api_mapping, p_api)
    o_ckey, o_status = canonical_for_api(api_mapping, o_api)

    mapping_status = "mapped"
    if not p_ckey and not o_ckey:
        mapping_status = f"both_unmapped:{p_status}|{o_status}"
    elif not p_ckey:
        mapping_status = f"player_unmapped:{p_status}"
    elif not o_ckey:
        mapping_status = f"opponent_unmapped:{o_status}"

    avg_odds = safe_float(odds_info.get("avg_odds"))
    implied = 1.0 / avg_odds if avg_odds and avg_odds > 0 else None

    base = {
        "pick_id": build_pick_id(match.get("event_key"), side, selected_key, avg_odds),
        "event_key": match.get("event_key"),
        "fixture_id": match.get("event_key"),
        "status": "pending",
        "date": match_datetime(match)[0],
        "time": match_datetime(match)[1],
        "gender": gender,
        "level": level,
        "surface": surface,
        "surface_source": surface_source,
        "raw_event_type_type": safe_str(match.get("event_type_type")),
        "raw_event_type_key": safe_str(match.get("event_type_key")),
        "raw_event_type": safe_str(match.get("event_type")),
        "raw_event_name": safe_str(match.get("event_name")),
        "raw_event_country_name": safe_str(match.get("event_country_name")),
        "raw_league_name": safe_str(match.get("league_name")),
        "raw_competition_name": safe_str(match.get("competition_name")),
        "raw_tournament_name": safe_str(match.get("tournament_name")),
        "raw_tournament_round": safe_str(match.get("tournament_round")),
        "context_text": tournament_context(match).get("context_text", ""),
        "tournament": match.get("tournament_name") or "",
        "round": match.get("tournament_round") or "",
        "match": f"{match.get('event_first_player')} - {match.get('event_second_player')}",
        "pick": selected_name,
        "opponent": opponent_name,
        "side": side,
        "market_side": "Home" if side == "home" else "Away",
        "odds": avg_odds,
        "avg_odds": avg_odds,
        "median_odds": odds_info.get("median_odds"),
        "best_odds": odds_info.get("best_odds"),
        "best_bookmaker": odds_info.get("best_bookmaker"),
        "implied_prob": None if implied is None else round(implied, 8),
        "bookmakers_raw": odds_info.get("bookmakers_raw"),
        "bookmakers_clean": odds_info.get("bookmakers_clean"),
        "outlier_bookmakers_removed": odds_info.get("outliers_removed"),
        "player_key": selected_key,
        "opponent_key": opponent_key,
        "player_api_key": p_api,
        "opponent_api_key": o_api,
        "player_canonical_key": p_ckey,
        "opponent_canonical_key": o_ckey,
        "mapping_status": mapping_status,
        "created_at": local_now().isoformat(),
    }

    if side == "home":
        p5, o5, p10, o10 = first_form5, second_form5, first_form10, second_form10
    else:
        p5, o5, p10, o10 = second_form5, first_form5, second_form10, first_form10
    labels = support_labels(selected_side=side, first_form=first_form10, second_form=second_form10, h2h=h2h)
    base.update({
        "player_last5_win_rate": p5.get("win_rate"),
        "opponent_last5_win_rate": o5.get("win_rate"),
        "last5_win_rate_diff": round((p5.get("win_rate") or 0) - (o5.get("win_rate") or 0), 6),
        "player_last10_win_rate": p10.get("win_rate"),
        "opponent_last10_win_rate": o10.get("win_rate"),
        "last10_win_rate_diff": round((p10.get("win_rate") or 0) - (o10.get("win_rate") or 0), 6),
        "player_last10_set_diff_avg": p10.get("set_diff_avg"),
        "opponent_last10_set_diff_avg": o10.get("set_diff_avg"),
        "last10_set_diff_diff": round((p10.get("set_diff_avg") or 0) - (o10.get("set_diff_avg") or 0), 6),
        "player_last10_game_diff_avg": p10.get("game_diff_avg"),
        "opponent_last10_game_diff_avg": o10.get("game_diff_avg"),
        "last10_game_diff_diff": round((p10.get("game_diff_avg") or 0) - (o10.get("game_diff_avg") or 0), 6),
        "h2h_matches": h2h.get("matches"),
        "h2h_player_wins": h2h.get("first_wins") if side == "home" else h2h.get("second_wins"),
        "h2h_opponent_wins": h2h.get("second_wins") if side == "home" else h2h.get("first_wins"),
        **labels,
    })

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
            "stake": extra.get("stake"),
            "stake_label": extra.get("stake_label"),
        }

    if level not in LEVEL_RULES:
        return finish("NO_BET", "unsupported_level")
    rule = LEVEL_RULES[level]
    model = rule["model"]
    if mapping_status != "mapped":
        return finish("NO_BET", "unmapped_player", extra={"tle_model": model})
    if implied is None:
        return finish("NO_BET", "missing_average_odds", extra={"tle_model": model})
    if avg_odds < ODDS_MIN or avg_odds > ODDS_MAX:
        return finish("NO_BET", "odds_outside_range", extra={"tle_model": model})
    if rule["needs_surface"] and surface not in VALID_SURFACES:
        return finish("NO_BET", "unknown_surface", extra={"tle_model": model})

    player = players.get(p_ckey or "")
    opponent = players.get(o_ckey or "")
    if not isinstance(player, dict) or not isinstance(opponent, dict):
        return finish("NO_BET", "missing_rating_player", extra={"tle_model": model})

    mp = model_prob_for_side(player, opponent, level=level, surface=surface, rule=rule)
    prob = float(mp["prob"])
    edge = prob - implied
    min_level = int(mp["min_level_matches"])
    min_surface = int(mp["min_surface_matches"])
    extra = {
        "tle_model": model,
        "tle_prob": round(prob, 8),
        "tle_edge": round(edge, 8),
        "tle_min_level_matches": min_level,
        "tle_min_surface_matches": min_surface if rule["needs_surface"] else "",
        "stake": 1.0,
        "stake_label": "Standard",
    }

    if min_level < int(rule["min_level_matches"]):
        return finish("NO_BET", "min_level_not_met", extra=extra)
    if rule["needs_surface"] and min_surface < int(rule["min_surface_matches"]):
        return finish("NO_BET", "min_surface_not_met", extra=extra)
    if prob < float(rule["min_prob"]):
        return finish("NO_BET", "elo_prob_too_low", extra=extra)
    if edge < float(rule["edge_bet"]):
        return finish("NO_BET", "elo_edge_too_low", extra=extra)
    if edge >= float(rule["edge_strong"]):
        extra["stake"] = 1.25
        extra["stake_label"] = "Strong"
        return finish("STRONG_BET", "pass_strong_edge", confidence="strong", extra=extra)
    return finish("BET", "pass_edge", confidence="normal", extra=extra)


def scan_match(match: dict[str, Any], *, players: dict[str, dict[str, Any]], api_mapping: dict[str, dict[str, Any]], include_no_bet: bool) -> list[dict[str, Any]]:
    ok, _reason = is_fixture_eligible(match)
    if not ok:
        return []
    context = tournament_context(match)
    if context["gender"] not in {"men", "women"}:
        return []

    surface, surface_source = infer_surface(match, context)
    learn_surface_from_match(match, context, surface, surface_source)

    event_key = match.get("event_key")
    first_key = safe_int(match.get("first_player_key"))
    second_key = safe_int(match.get("second_player_key"))
    first_name = safe_str(match.get("event_first_player"))
    second_name = safe_str(match.get("event_second_player"))

    odds_blob = fetch_odds(event_key)
    parsed = parse_home_away_odds(odds_blob)
    if not parsed:
        return []

    h2h_blob = fetch_h2h(first_key, second_key)
    first_results = normalize_player_results(h2h_blob.get("firstPlayerResults"), first_key, current_event_key=event_key)
    second_results = normalize_player_results(h2h_blob.get("secondPlayerResults"), second_key, current_event_key=event_key)
    h2h = h2h_summary(h2h_blob.get("H2H"), first_key, second_key)
    first_form5 = form_summary(first_results[:5])
    second_form5 = form_summary(second_results[:5])
    first_form10 = form_summary(first_results[:10])
    second_form10 = form_summary(second_results[:10])

    rows = [
        evaluate_side(
            match=match, side="home", odds_info=parsed["home"], selected_key=first_key, selected_name=first_name,
            opponent_key=second_key, opponent_name=second_name, context=context, surface=surface, surface_source=surface_source,
            players=players, api_mapping=api_mapping, first_form5=first_form5, second_form5=second_form5,
            first_form10=first_form10, second_form10=second_form10, h2h=h2h,
        ),
        evaluate_side(
            match=match, side="away", odds_info=parsed["away"], selected_key=second_key, selected_name=second_name,
            opponent_key=first_key, opponent_name=first_name, context=context, surface=surface, surface_source=surface_source,
            players=players, api_mapping=api_mapping, first_form5=first_form5, second_form5=second_form5,
            first_form10=first_form10, second_form10=second_form10, h2h=h2h,
        ),
    ]
    if include_no_bet:
        return rows
    return [r for r in rows if r["decision"] in {"BET", "STRONG_BET"}]


def payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("picks"), list):
        return [x for x in payload["picks"] if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in CSV_FIELDS})

# =========================
# UNRESOLVED SURFACE REPORT
# =========================


def build_unresolved_surface_report(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("reason") != "unknown_surface":
            continue
        if r.get("level") not in {"atp_wta", "grand_slam", "challenger"}:
            continue
        key = normalize_surface_map_key(
            r.get("context_text")
            or f"{r.get('raw_event_type_type', '')} {r.get('raw_tournament_name', '')} {r.get('raw_tournament_round', '')}"
        )
        if not key:
            continue
        item = seen.setdefault(key, {
            "level": r.get("level"),
            "gender": r.get("gender"),
            "raw_event_type_type": r.get("raw_event_type_type"),
            "raw_tournament_name": r.get("raw_tournament_name"),
            "raw_tournament_round": r.get("raw_tournament_round"),
            "context_text": r.get("context_text"),
            "suggested_map_key": key,
            "matches": [],
        })
        match_name = r.get("match")
        if match_name and match_name not in item["matches"]:
            item["matches"].append(match_name)
    return sorted(seen.values(), key=lambda x: (safe_str(x.get("level")), safe_str(x.get("raw_tournament_name"))))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="Scan date YYYY-MM-DD; default today Europe/Ljubljana")
    parser.add_argument("--days-ahead", type=int, default=1)
    parser.add_argument("--ratings-path", type=Path, default=None)
    parser.add_argument("--api-mapping", type=Path, default=API_MAPPING_JSON)
    parser.add_argument("--include-no-bet", action="store_true", help="Store/report NO_BET rows too")
    parser.add_argument("--min-start-minutes", type=int, default=DEFAULT_MIN_START_MINUTES, help="Only create predictions for matches starting at least this many minutes from now.")
    parser.add_argument("--debug-report", action="store_true", help="Write scan_diagnostics.csv and include raw_context_samples in the report.")
    args = parser.parse_args()

    ratings_path = find_ratings_path(args.ratings_path)
    ratings = read_json_maybe_gz(ratings_path)
    players = get_players_from_ratings(ratings)
    api_mapping = read_api_mapping(args.api_mapping)

    global TOURNAMENT_SURFACE_MAP, API_TOURNAMENT_SURFACE_BY_KEY
    TOURNAMENT_SURFACE_MAP = read_tournament_surface_map(SURFACE_MAP_JSON)
    API_TOURNAMENT_SURFACE_BY_KEY = read_api_tournament_surface_map(API_TOURNAMENTS_METADATA_JSON)

    start_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else local_today()
    fixtures: list[dict[str, Any]] = []
    for i in range(args.days_ahead):
        d = start_date + timedelta(days=i)
        fixtures.extend(fetch_fixtures_for_date(d.strftime("%Y-%m-%d")))

    rows: list[dict[str, Any]] = []
    counters = Counter()
    for m in fixtures:
        ok, reason = is_fixture_eligible(m)
        if not ok:
            counters[f"fixture_skip_{reason}"] += 1
            continue
        if not starts_after_min_lead(m, args.min_start_minutes):
            counters["fixture_skip_starts_too_soon"] += 1
            continue
        try:
            built = scan_match(m, players=players, api_mapping=api_mapping, include_no_bet=True)
            rows.extend(built)
            counters["fixtures_scanned"] += 1
            if built:
                counters["fixtures_with_diagnostics"] += 1
            if any(r.get("decision") in {"BET", "STRONG_BET"} for r in built):
                counters["fixtures_with_output"] += 1
        except Exception as exc:
            counters["fixture_error"] += 1
            print(f"ERROR event_key={m.get('event_key')} {m.get('event_first_player')} - {m.get('event_second_player')}: {exc}")

    new_active = [r for r in rows if r["decision"] in {"BET", "STRONG_BET"}]

    old_predictions = read_json_path(PREDICTIONS_JSON, {"picks": []})
    old_active = [r for r in payload_items(old_predictions) if safe_str(r.get("status")).lower() == "pending"]
    active_by_id = {safe_str(r.get("pick_id")): r for r in old_active if safe_str(r.get("pick_id"))}
    added = 0
    for r in new_active:
        pid = safe_str(r.get("pick_id"))
        if pid and pid not in active_by_id:
            active_by_id[pid] = r
            added += 1
    active = list(active_by_id.values())
    active.sort(key=lambda r: (safe_str(r.get("date")), safe_str(r.get("time")), safe_str(r.get("tournament")), safe_str(r.get("match"))))

    old_results = read_json_path(RESULTS_JSON, {"picks": []})
    results_items = payload_items(old_results)
    results_by_id = {safe_str(r.get("pick_id")): r for r in results_items if safe_str(r.get("pick_id"))}
    for r in new_active:
        pid = safe_str(r.get("pick_id"))
        if pid and pid not in results_by_id:
            results_by_id[pid] = r
    results_all = list(results_by_id.values())
    results_all.sort(key=lambda r: (safe_str(r.get("date")), safe_str(r.get("time")), safe_str(r.get("tournament")), safe_str(r.get("match"))))

    write_json(PREDICTIONS_JSON, {
        "generated_at": now_utc_iso(),
        "model": "TLE Standalone Elo Scanner v3.4",
        "summary": {"active_picks": len(active), "new_added": added, "scan_rows": len(rows), "new_candidates": len(new_active)},
        "picks": active,
    })
    write_json(RESULTS_JSON, {
        "generated_at": now_utc_iso(),
        "model": "TLE Standalone Elo Scanner v3.4",
        "summary": {"total_tracked": len(results_all), "pending": sum(1 for r in results_all if safe_str(r.get("status")).lower() == "pending")},
        "picks": results_all,
    })
    write_csv(ACTIVE_CSV, active)

    raw_context_samples = []
    if args.debug_report:
        write_csv(SCAN_CSV, rows)
        for r in rows[:300]:
            raw_context_samples.append({
                "date": r.get("date"), "time": r.get("time"), "decision": r.get("decision"), "reason": r.get("reason"),
                "gender": r.get("gender"), "level": r.get("level"), "surface": r.get("surface"), "surface_source": r.get("surface_source"),
                "raw_event_type_type": r.get("raw_event_type_type"), "raw_event_type_key": r.get("raw_event_type_key"),
                "raw_event_type": r.get("raw_event_type"), "raw_event_name": r.get("raw_event_name"),
                "raw_tournament_name": r.get("raw_tournament_name"), "raw_tournament_round": r.get("raw_tournament_round"),
                "raw_league_name": r.get("raw_league_name"), "raw_competition_name": r.get("raw_competition_name"),
                "context_text": r.get("context_text"), "match": r.get("match"),
            })

    unresolved_surface = build_unresolved_surface_report(rows)
    write_tournament_surface_map(SURFACE_MAP_JSON)
    write_json(UNRESOLVED_SURFACE_JSON, {
        "generated_at": now_utc_iso(),
        "count": len(unresolved_surface),
        "items": unresolved_surface,
        "note": "Add suggested_map_key: hard/clay/grass/carpet to data/metadata/tournament_surface_map.json if API tournaments metadata does not cover these events.",
    })

    report = {
        "status": "ok",
        "generated_at": now_utc_iso(),
        "date_start": str(start_date),
        "days_ahead": args.days_ahead,
        "fixtures_total": len(fixtures),
        "scan_rows": len(rows),
        "new_candidates": len(new_active),
        "new_added_to_predictions": added,
        "active_predictions_total": len(active),
        "min_start_minutes": args.min_start_minutes,
        "surface_map_entries": len(TOURNAMENT_SURFACE_MAP),
        "api_tournament_surface_entries": len(API_TOURNAMENT_SURFACE_BY_KEY),
        "api_tournaments_metadata_json": str(API_TOURNAMENTS_METADATA_JSON),
        "unresolved_surface_count": len(unresolved_surface),
        "decision_counts_scan": dict(sorted(Counter(r["decision"] for r in rows).items())),
        "reason_counts_scan": dict(sorted(Counter(r["reason"] for r in rows).items())),
        "by_level_scan": dict(sorted(Counter(r["level"] for r in rows).items())),
        "by_surface_source_scan": dict(sorted(Counter(r.get("surface_source", "") for r in rows).items())),
        "counters": dict(sorted(counters.items())),
        "odds_rules": {
            "min_bookmakers_each_side": MIN_BOOKMAKERS_EACH_SIDE,
            "max_odds_deviation_from_median": MAX_ODDS_DEVIATION_FROM_MEDIAN,
            "min_clean_bookmakers": MIN_CLEAN_BOOKMAKERS,
            "odds_min": ODDS_MIN,
            "odds_max": ODDS_MAX,
            "pricing": "average odds after outlier removal",
        },
        "outputs": {
            "predictions_json": str(PREDICTIONS_JSON),
            "results_json": str(RESULTS_JSON),
            "active_csv": str(ACTIVE_CSV),
            "report_json": str(REPORT_JSON),
            "surface_map_json": str(SURFACE_MAP_JSON),
            "unresolved_surface_json": str(UNRESOLVED_SURFACE_JSON),
            **({"scan_diagnostics_csv": str(SCAN_CSV)} if args.debug_report else {}),
        },
        "notes": [
            "This is standalone: it scans all eligible API fixtures and odds, not only tennis value picks.",
            "Odds are average clean odds after removing bookmaker outliers.",
            "Form and H2H are stored as tracking fields only; they do not affect TLE probability in v1.",
            "Only BET and STRONG_BET rows are stored as active predictions.",
            "Surface now uses 06a API tournaments metadata by tournament_key before local fallback map.",
        ],
    }
    if args.debug_report:
        report["raw_context_samples"] = raw_context_samples
    write_json(REPORT_JSON, report)

    print(
        f"status=ok fixtures={len(fixtures)} scan_rows={len(rows)} "
        f"new_candidates={len(new_active)} new_added={added} active={len(active)} "
        f"min_start_minutes={args.min_start_minutes} unresolved_surface={len(unresolved_surface)}"
    )
    print(f"by_level={dict(sorted(Counter(r['level'] for r in rows).items()))}")
    print(f"decisions={dict(sorted(Counter(r['decision'] for r in rows).items()))}")
    print(f"reasons={dict(sorted(Counter(r['reason'] for r in rows).items()))}")
    print(f"surface_sources={dict(sorted(Counter(r.get('surface_source', '') for r in rows).items()))}")


if __name__ == "__main__":
    main()

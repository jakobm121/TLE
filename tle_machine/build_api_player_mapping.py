from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

try:
    from .utils import now_utc_iso, write_json
except Exception:
    def now_utc_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


API_SOURCE_DIR = Path("data/source/api_tennis")
API_RAW_ROOT = Path("data/raw/api_tennis")
API_PLAYER_CACHE_JSON = Path("data/raw/api_tennis/players/api_players.json")

RATINGS_JSON = Path("data/ratings/tle_player_ratings.json")
RATINGS_JSON_GZ = Path("data/ratings/tle_player_ratings.json.gz")

METADATA_DIR = Path("data/metadata/api_tennis")
REPORT_DIR = Path("data/reports/api_tennis")

MAPPING_JSON = METADATA_DIR / "player_mapping.json"
OVERRIDES_JSON = METADATA_DIR / "player_mapping_overrides.json"
REPORT_JSON = REPORT_DIR / "player_mapping_report.json"
REVIEW_CSV = REPORT_DIR / "player_mapping_review.csv"
CANDIDATES_CSV = REPORT_DIR / "player_mapping_candidates.csv"
ISSUES_CSV = REPORT_DIR / "player_mapping_issues.csv"

AUTO_SCORE_MIN = 0.935
AUTO_MARGIN_MIN = 0.055
INITIAL_SURNAME_SCORE_MIN = 0.880
AMBIGUOUS_MARGIN = 0.035

PARTICLES = {"de", "del", "de la", "da", "di", "van", "von", "la", "le", "du", "dos", "das"}

COUNTRY_WORDS = {
    "barbados",
    "venezuela",
    "costa",
    "rica",
    "guatemala",
    "puerto",
    "rico",
    "jamaica",
    "georgia",
    "kosovo",
    "ireland",
    "montenegro",
    "latvia",
    "azerbaijan",
    "north",
    "macedonia",
    "moldova",
    "serbia",
    "slovenia",
    "croatia",
    "france",
    "spain",
    "italy",
    "germany",
    "belgium",
    "china",
    "japan",
    "usa",
    "australia",
    "canada",
    "brazil",
    "argentina",
    "poland",
    "ukraine",
    "kazakhstan",
}


def read_json_any(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_safe(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return read_json_any(path)
    except Exception:
        return default


def read_jsonl_gz(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                obj = json.loads(line)
                if isinstance(obj, dict):
                    yield obj


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def norm_text(value: Any) -> str:
    return str(value or "").strip()


def get_any(d: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = d.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def unwrap_api_result(obj: Any) -> list[Any]:
    if isinstance(obj, dict):
        if isinstance(obj.get("result"), list):
            return obj["result"]
        response = obj.get("response")
        if isinstance(response, dict) and isinstance(response.get("result"), list):
            return response["result"]
        if isinstance(obj.get("data"), list):
            return obj["data"]
    if isinstance(obj, list):
        return obj
    return []


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def normalize_name(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = s.replace("'", " ").replace("`", " ").replace("Ã¢ÂÂ", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens_from_norm(norm: str) -> list[str]:
    return [t for t in (norm or "").split() if t]


def tokens(s: str) -> list[str]:
    return tokens_from_norm(normalize_name(s))


def token_signature_from_tokens(ts: list[str]) -> str:
    return " ".join(sorted(t for t in ts if t))


def compact_no_space_from_norm(norm: str) -> str:
    return re.sub(r"\s+", "", norm or "")


def surname_tokens_from_tokens(ts: list[str]) -> list[str]:
    if not ts:
        return []
    if len(ts) >= 2 and f"{ts[-2]} {ts[-1]}" in PARTICLES:
        return ts[-2:]
    return [ts[-1]]


def surname_key_from_tokens(ts: list[str]) -> str:
    return " ".join(surname_tokens_from_tokens(ts))


def compact_initial_from_tokens(ts: list[str]) -> str:
    if not ts:
        return ""
    if len(ts) == 1:
        return ts[0]
    return f"{ts[0][0]} {' '.join(ts[1:])}"


def initials_from_tokens(ts: list[str]) -> str:
    return "".join(t[0] for t in ts if t)


def looks_like_doubles_or_team(name: str) -> bool:
    if "/" in (name or ""):
        return True
    if " & " in (name or "").lower():
        return True
    ts = tokens(name)
    if len(ts) <= 2 and any(t in COUNTRY_WORDS for t in ts):
        return True
    return False


def is_non_singles_raw(fx: dict[str, Any], fname: str = "", sname: str = "") -> bool:
    etype = str(get_any(fx, ("event_type_type", "event_type", "type", "category_name")) or "").lower()
    tourn = str(get_any(fx, ("tournament_name", "event_name", "league_name", "tournament", "league")) or "").lower()
    names = f"{fname} {sname}".lower()

    if "double" in etype or "doubles" in etype:
        return True
    if "team" in etype or "teams" in etype:
        return True
    if "davis cup" in tourn or "billie jean" in tourn or "bjk cup" in tourn or "fed cup" in tourn:
        return True
    if "/" in names or " & " in names:
        return True

    return False


def normalize_gender(raw: Any) -> str:
    s = str(raw or "").lower()
    if "women" in s or "wta" in s or "female" in s or "girls" in s:
        return "women"
    if "men" in s or "atp" in s or "male" in s or "boys" in s or "challenger" in s:
        return "men"
    return ""


def infer_raw_gender(fx: dict[str, Any]) -> str:
    text = " | ".join(
        str(get_any(fx, (key,)) or "")
        for key in (
            "event_type_type",
            "event_type",
            "league_name",
            "tournament_name",
            "event_name",
            "category_name",
        )
    )
    return normalize_gender(text)


def ratio_norm(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def token_jaccard_norm(an: str, sn: str) -> float:
    aa, bb = set(tokens_from_norm(an)), set(tokens_from_norm(sn))
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def initial_surname_match_pre(api_initial: str, api_surname: str, sack_initial: str, sack_surname: str) -> bool:
    return bool(api_initial and api_surname and api_surname == sack_surname and api_initial == sack_initial)


@dataclass(slots=True)
class SackPlayer:
    key: str
    gender: str
    name: str
    matches: int
    levels: dict[str, Any]
    surfaces: dict[str, Any]
    norm: str = ""
    toks: list[str] = field(default_factory=list)
    last: str = ""
    surname: str = ""
    initial: str = ""
    compact: str = ""
    token_signature: str = ""
    compact_no_space: str = ""


@dataclass(slots=True)
class ApiPlayer:
    key: str
    gender: str
    names: Counter
    matches: int
    levels: Counter
    surfaces: Counter
    opponents: Counter
    tournaments: Counter
    name_sources: Counter = field(default_factory=Counter)
    data_sources: Counter = field(default_factory=Counter)

    @property
    def name(self) -> str:
        return self.names.most_common(1)[0][0] if self.names else ""


def decorate_sack_player(sp: SackPlayer) -> SackPlayer:
    sp.norm = normalize_name(sp.name)
    sp.toks = tokens_from_norm(sp.norm)
    sp.last = sp.toks[-1] if sp.toks else ""
    sp.surname = surname_key_from_tokens(sp.toks)
    sp.initial = sp.toks[0][0] if sp.toks else ""
    sp.compact = compact_initial_from_tokens(sp.toks)
    sp.token_signature = token_signature_from_tokens(sp.toks)
    sp.compact_no_space = compact_no_space_from_norm(sp.norm)
    return sp


def canonical_bare_api_key(api_key: str) -> str:
    raw = str(api_key or "").strip()
    if not raw:
        return ""
    m = re.match(r"^(men|women):api(?:_tennis)?:(.+)$", raw)
    if m:
        return m.group(2)
    return raw


def api_key_variants(api_key: str, gender: str) -> list[str]:
    raw = str(api_key)
    bare = canonical_bare_api_key(raw)
    variants = [raw]
    for kk in (bare, f"{gender}:api:{bare}", f"{gender}:api_tennis:{bare}"):
        if kk and kk not in variants:
            variants.append(kk)
    return variants


def load_api_player_cache(path: Path = API_PLAYER_CACHE_JSON) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    report = {
        "path": str(path),
        "exists": path.exists(),
        "entries": 0,
        "with_full_name": 0,
        "with_short_name": 0,
    }

    data = read_json_safe(path, {})
    if not isinstance(data, dict):
        report["error"] = "cache is not a JSON object"
        return {}, report

    cache: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        pid = norm_text(value.get("player_key")) or norm_text(key)
        if not pid:
            continue
        cache[pid] = value

    report["entries"] = len(cache)
    report["with_full_name"] = sum(1 for v in cache.values() if norm_text(v.get("player_full_name")))
    report["with_short_name"] = sum(1 for v in cache.values() if norm_text(v.get("player_name")))

    return cache, report


def cached_api_player_name(
    api_player_cache: dict[str, dict[str, Any]],
    api_key: str,
    fallback_name: str,
) -> tuple[str, str]:
    bare = canonical_bare_api_key(api_key)
    fallback = norm_text(fallback_name)

    if not bare:
        return fallback, "api_source"

    row = api_player_cache.get(bare)
    if not isinstance(row, dict):
        return fallback, "api_source"

    full_name = norm_text(row.get("player_full_name"))
    if full_name:
        return full_name, "api_cache_full_name"

    short_name = norm_text(row.get("player_name"))
    if short_name:
        return short_name, "api_cache_short_name"

    return fallback, "api_source"


def load_sack_players() -> dict[str, SackPlayer]:
    path = RATINGS_JSON if RATINGS_JSON.exists() else RATINGS_JSON_GZ
    data = read_json_any(path)
    players = data.get("players", data) if isinstance(data, dict) else {}
    out: dict[str, SackPlayer] = {}

    for key, p in players.items():
        gender = p.get("gender") or (key.split(":", 1)[0] if ":" in key else "")
        name = p.get("name") or ""
        if not gender or not name:
            continue

        sp = SackPlayer(
            key=key,
            gender=gender,
            name=name,
            matches=int(p.get("matches") or 0),
            levels=dict(p.get("level") or {}),
            surfaces=dict(p.get("surface") or {}),
        )
        out[key] = decorate_sack_player(sp)

    return out


def extract_player(match: dict[str, Any], side: str) -> tuple[str, str] | None:
    obj = match.get(side) or {}
    if not isinstance(obj, dict):
        return None

    key = obj.get("player_key") or obj.get("api_player_key") or obj.get("key")
    name = obj.get("name") or obj.get("player_name")

    if not key or not name:
        return None

    return str(key), str(name)


def upsert_api_player(
    api_players: dict[str, ApiPlayer],
    counters: Counter,
    *,
    gender: str,
    raw_key: str,
    raw_name: str,
    opponent_name: str = "",
    level: str = "unknown",
    surface: str = "unknown",
    tournament: str = "",
    data_source: str,
    api_player_cache: dict[str, dict[str, Any]],
) -> None:
    if gender not in {"men", "women"}:
        counters["skipped_unknown_gender"] += 1
        return

    if not raw_key or not raw_name:
        counters["skipped_missing_key_or_name"] += 1
        return

    if looks_like_doubles_or_team(raw_name):
        counters["skipped_doubles_or_team_player_name"] += 1
        return

    bare = canonical_bare_api_key(raw_key)
    canonical_api_key = f"{gender}:api:{bare}" if bare else str(raw_key)

    name, name_source = cached_api_player_name(api_player_cache, canonical_api_key, raw_name)
    opp_name, _ = cached_api_player_name(api_player_cache, opponent_name, opponent_name)

    if name_source == "api_cache_full_name":
        counters["api_player_cache_full_name_used"] += 1
    elif name_source == "api_cache_short_name":
        counters["api_player_cache_short_name_used"] += 1

    if name != raw_name:
        counters["api_player_cache_name_changed"] += 1

    if canonical_api_key not in api_players:
        api_players[canonical_api_key] = ApiPlayer(
            key=canonical_api_key,
            gender=gender,
            names=Counter(),
            matches=0,
            levels=Counter(),
            surfaces=Counter(),
            opponents=Counter(),
            tournaments=Counter(),
        )

    ap = api_players[canonical_api_key]
    ap.names[name] += 1
    ap.matches += 1
    ap.levels[level] += 1
    ap.surfaces[surface] += 1
    ap.opponents[opp_name or opponent_name] += 1
    ap.tournaments[tournament] += 1
    ap.name_sources[name_source] += 1
    ap.data_sources[data_source] += 1


def load_api_players_from_source(
    api_players: dict[str, ApiPlayer],
    api_player_cache: dict[str, dict[str, Any]],
    counters: Counter,
) -> None:
    for path in sorted(API_SOURCE_DIR.glob("tle_api_matches_*.jsonl.gz")):
        for m in read_jsonl_gz(path):
            gender = m.get("gender") or ""
            level = m.get("level") or "unknown"
            surface = m.get("surface") or "unknown"
            tournament = m.get("tourney_name") or m.get("tournament_name") or ""

            w = extract_player(m, "winner")
            l = extract_player(m, "loser")
            if not w or not l:
                continue

            upsert_api_player(
                api_players,
                counters,
                gender=gender,
                raw_key=w[0],
                raw_name=w[1],
                opponent_name=l[1],
                level=level,
                surface=surface,
                tournament=tournament,
                data_source="api_source",
                api_player_cache=api_player_cache,
            )
            upsert_api_player(
                api_players,
                counters,
                gender=gender,
                raw_key=l[0],
                raw_name=l[1],
                opponent_name=w[1],
                level=level,
                surface=surface,
                tournament=tournament,
                data_source="api_source",
                api_player_cache=api_player_cache,
            )


def raw_player_from_fixture(fx: dict[str, Any], side: str) -> tuple[str, str]:
    if side == "first":
        key = get_any(fx, ("first_player_key", "event_first_player_key", "player_key", "home_player_key", "home_team_key"))
        name = get_any(fx, ("event_first_player", "first_player", "first_player_name", "player_name", "event_home_team", "home_player", "home_team"))
    else:
        key = get_any(fx, ("second_player_key", "event_second_player_key", "away_player_key", "away_team_key"))
        name = get_any(fx, ("event_second_player", "second_player", "second_player_name", "event_away_team", "away_player", "away_team"))
    return norm_text(key), norm_text(name)


def raw_files(root: Path) -> list[Path]:
    candidates: list[Path] = []

    for subdir in ("odds", "fixtures", "results"):
        d = root / subdir
        if d.exists():
            candidates.extend(sorted(d.glob("*.json")))

    candidates.extend(sorted(root.glob("odds_*.json")))
    candidates.extend(sorted(root.glob("fixtures_*.json")))
    candidates.extend(sorted(root.glob("*.json")))

    # Keep only real raw payload files. Exclude player cache/metadata/report-like files.
    out: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        parts = set(path.parts)
        if "players" in parts or "metadata" in parts:
            continue
        out.append(path)

    return out


def load_api_players_from_raw(
    api_players: dict[str, ApiPlayer],
    api_player_cache: dict[str, dict[str, Any]],
    counters: Counter,
    raw_root: Path = API_RAW_ROOT,
) -> None:
    for path in raw_files(raw_root):
        payload = read_json_safe(path, {})
        rows = unwrap_api_result(payload)
        counters["raw_mapping_files_seen"] += 1
        counters["raw_mapping_rows_seen"] += len(rows)

        for fx in rows:
            if not isinstance(fx, dict):
                continue

            fkey, fname = raw_player_from_fixture(fx, "first")
            skey, sname = raw_player_from_fixture(fx, "second")

            if is_non_singles_raw(fx, fname, sname):
                counters["raw_mapping_skipped_non_singles"] += 1
                continue

            gender = infer_raw_gender(fx)
            level = str(get_any(fx, ("event_type_type", "event_type", "type", "category_name")) or "raw")
            tournament = str(get_any(fx, ("tournament_name", "event_name", "league_name", "tournament", "league")) or "")
            surface = str(get_any(fx, ("event_surface", "surface", "tournament_surface", "league_surface")) or "unknown")

            upsert_api_player(
                api_players,
                counters,
                gender=gender,
                raw_key=fkey,
                raw_name=fname,
                opponent_name=sname,
                level=level,
                surface=surface,
                tournament=tournament,
                data_source="api_raw",
                api_player_cache=api_player_cache,
            )
            upsert_api_player(
                api_players,
                counters,
                gender=gender,
                raw_key=skey,
                raw_name=sname,
                opponent_name=fname,
                level=level,
                surface=surface,
                tournament=tournament,
                data_source="api_raw",
                api_player_cache=api_player_cache,
            )


def load_api_players(
    api_player_cache: dict[str, dict[str, Any]],
    counters: Counter,
    include_raw: bool,
    raw_root: Path,
) -> dict[str, ApiPlayer]:
    api_players: dict[str, ApiPlayer] = {}
    load_api_players_from_source(api_players, api_player_cache, counters)
    counters["api_players_after_source"] = len(api_players)

    if include_raw:
        load_api_players_from_raw(api_players, api_player_cache, counters, raw_root)
        counters["api_players_after_raw"] = len(api_players)

    return api_players


def parse_override_value(v: Any) -> str | None:
    if v in {None, "", "null"}:
        return None
    if isinstance(v, dict):
        t = v.get("target_player_key") or v.get("sackmann_player_key") or v.get("target")
        return None if t in {None, "", "null"} else str(t)
    return str(v)


def load_overrides() -> dict[str, str | None]:
    if not OVERRIDES_JSON.exists():
        OVERRIDES_JSON.parent.mkdir(parents=True, exist_ok=True)
        write_json(OVERRIDES_JSON, {})
        return {}

    data = read_json_any(OVERRIDES_JSON)
    if not isinstance(data, dict):
        return {}

    return {str(k): parse_override_value(v) for k, v in data.items()}


@dataclass
class SackIndex:
    by_gender: dict[str, list[SackPlayer]]
    by_norm: dict[tuple[str, str], list[SackPlayer]]
    by_compact: dict[tuple[str, str], list[SackPlayer]]
    by_last: dict[tuple[str, str], list[SackPlayer]]
    by_surname: dict[tuple[str, str], list[SackPlayer]]
    by_initial: dict[tuple[str, str], list[SackPlayer]]
    by_token_signature: dict[tuple[str, str], list[SackPlayer]]
    by_compact_no_space: dict[tuple[str, str], list[SackPlayer]]


def build_sack_index(sack_players: dict[str, SackPlayer]) -> SackIndex:
    idx = SackIndex(defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list))
    for sp in sack_players.values():
        idx.by_gender[sp.gender].append(sp)
        if sp.norm:
            idx.by_norm[(sp.gender, sp.norm)].append(sp)
        if sp.compact:
            idx.by_compact[(sp.gender, sp.compact)].append(sp)
        if sp.last:
            idx.by_last[(sp.gender, sp.last)].append(sp)
        if sp.surname:
            idx.by_surname[(sp.gender, sp.surname)].append(sp)
        if sp.initial:
            idx.by_initial[(sp.gender, sp.initial)].append(sp)
        if sp.token_signature:
            idx.by_token_signature[(sp.gender, sp.token_signature)].append(sp)
        if sp.compact_no_space:
            idx.by_compact_no_space[(sp.gender, sp.compact_no_space)].append(sp)
    return idx


def api_name_features(name: str) -> dict[str, Any]:
    norm = normalize_name(name)
    ts = tokens_from_norm(norm)
    return {
        "norm": norm,
        "tokens": ts,
        "last": ts[-1] if ts else "",
        "surname": surname_key_from_tokens(ts),
        "initial": ts[0][0] if ts else "",
        "compact": compact_initial_from_tokens(ts),
        "token_signature": token_signature_from_tokens(ts),
        "compact_no_space": compact_no_space_from_norm(norm),
    }


def score_candidate_features(api_feat: dict[str, Any], sp: SackPlayer) -> tuple[float, str]:
    an = api_feat["norm"]
    sn = sp.norm

    if not an or not sn:
        return 0.0, "empty"

    if an == sn:
        return 1.0, "exact_normalized"

    # API-Tennis often returns names as "Surname Middle First" while Sackmann
    # usually stores "First Middle Surname". Same tokens, different order.
    if api_feat.get("token_signature") and api_feat.get("token_signature") == sp.token_signature:
        return 0.985, "token_set_exact"

    # Handles apostrophe/space variants:
    #   Stefano D Agostino vs Stefano Dagostino
    #   Francesca Dell Edera vs Francesca Delledera
    if api_feat.get("compact_no_space") and api_feat.get("compact_no_space") == sp.compact_no_space:
        return 0.982, "compact_no_space"

    if api_feat["compact"] and api_feat["compact"] == sp.compact:
        return 0.970, "exact_initial_form"

    if initial_surname_match_pre(api_feat["initial"], api_feat["surname"], sp.initial, sp.surname):
        base = ratio_norm(an, sn)
        tj = token_jaccard_norm(an, sn)
        score = max(0.900, min(0.970, 0.84 + 0.10 * base + 0.06 * tj))
        return score, "initial_surname"

    r = ratio_norm(an, sn)
    tj = token_jaccard_norm(an, sn)
    last_bonus = 0.06 if api_feat["last"] and api_feat["last"] == sp.last else 0.0
    init_bonus = 0.03 if api_feat["initial"] and api_feat["initial"] == sp.initial else 0.0
    score = min(0.999, 0.72 * r + 0.18 * tj + last_bonus + init_bonus)
    method = "fuzzy_same_last" if last_bonus else "fuzzy"
    return score, method


def candidate_pool_fast(api_player: ApiPlayer, idx: SackIndex) -> list[SackPlayer]:
    feat = api_name_features(api_player.name)
    gender = api_player.gender
    pool_by_key: dict[str, SackPlayer] = {}

    def add_many(items: list[SackPlayer]) -> None:
        for sp in items:
            pool_by_key[sp.key] = sp

    if feat["norm"]:
        add_many(idx.by_norm.get((gender, feat["norm"]), []))
    if feat["token_signature"]:
        add_many(idx.by_token_signature.get((gender, feat["token_signature"]), []))
    if feat["compact_no_space"]:
        add_many(idx.by_compact_no_space.get((gender, feat["compact_no_space"]), []))
    if feat["compact"]:
        add_many(idx.by_compact.get((gender, feat["compact"]), []))
    if feat["surname"]:
        add_many(idx.by_surname.get((gender, feat["surname"]), []))
    if feat["last"]:
        add_many(idx.by_last.get((gender, feat["last"]), []))

    if len(pool_by_key) < 4 and feat["initial"]:
        an = feat["norm"]
        api_token_set = set(feat["tokens"])
        for sp in idx.by_initial.get((gender, feat["initial"]), []):
            if sp.key in pool_by_key:
                continue
            if api_token_set & set(sp.toks) or ratio_norm(an, sp.norm) >= 0.55:
                pool_by_key[sp.key] = sp

    if not pool_by_key:
        scored: list[tuple[float, SackPlayer]] = []
        for sp in idx.by_gender.get(gender, []):
            sc, _ = score_candidate_features(feat, sp)
            if sc >= 0.78:
                scored.append((sc, sp))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _, sp in scored[:20]:
            pool_by_key[sp.key] = sp

    return list(pool_by_key.values())


def find_override(api_key: str, gender: str, overrides: dict[str, str | None]) -> tuple[bool, str | None, str | None]:
    for k in api_key_variants(api_key, gender):
        if k in overrides:
            return True, overrides[k], k
    return False, None, None


def build_mapping(api_player_cache_path: Path = API_PLAYER_CACHE_JSON, include_raw: bool = True, raw_root: Path = API_RAW_ROOT) -> None:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    counters = Counter()

    sack_players = load_sack_players()
    api_player_cache, api_player_cache_report = load_api_player_cache(api_player_cache_path)
    api_players = load_api_players(api_player_cache, counters, include_raw, raw_root)
    overrides = load_overrides()
    sack_index = build_sack_index(sack_players)

    counters["api_player_cache_entries"] = api_player_cache_report.get("entries", 0)
    counters["api_player_cache_with_full_name"] = api_player_cache_report.get("with_full_name", 0)
    counters["api_player_cache_with_short_name"] = api_player_cache_report.get("with_short_name", 0)

    mapping: dict[str, Any] = {
        "generated_at": now_utc_iso(),
        "source": "api_tennis",
        "target": "sackmann",
        "mapping": {},
    }

    review_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    issue_rows: list[dict[str, Any]] = []
    reverse: dict[str, list[str]] = defaultdict(list)

    for api_key, ap in sorted(api_players.items()):
        counters["api_players"] += 1
        counters[f"api_gender_{ap.gender}"] += 1

        name = ap.name
        has_override, target, override_key = find_override(api_key, ap.gender, overrides)

        if has_override:
            status = "manual_unmapped" if target is None else "manual_mapped"

            if target and target not in sack_players:
                status = "manual_invalid_target"
                issue_rows.append({"api_player_key": api_key, "api_name": name, "issue": "manual_invalid_target", "detail": target})
                target = None

            mapping["mapping"][api_key] = {
                "status": status,
                "sackmann_player_key": target,
                "api_name": name,
                "gender": ap.gender,
                "confidence": 1.0 if target else 0.0,
                "method": "manual_override",
                "override_key": override_key,
                "api_matches": ap.matches,
                "api_name_variants": dict(ap.names.most_common(10)),
                "api_name_sources": dict(ap.name_sources),
                "api_data_sources": dict(ap.data_sources),
            }
            counters[status] += 1
            if target:
                reverse[target].append(api_key)
            continue

        feat = api_name_features(name)
        candidates: list[tuple[float, str, SackPlayer]] = []

        for sp in candidate_pool_fast(ap, sack_index):
            sc, method = score_candidate_features(feat, sp)
            if sc < 0.760:
                continue
            match_bonus = min(0.012, math.log1p(max(sp.matches, 0)) / 1000.0)
            final_score = min(0.999, sc + match_bonus)
            candidates.append((final_score, method, sp))

        candidates.sort(key=lambda x: x[0], reverse=True)
        top = candidates[:10]

        for rank, (sc, method, sp) in enumerate(top, start=1):
            candidate_rows.append(
                {
                    "api_player_key": api_key,
                    "api_name": name,
                    "gender": ap.gender,
                    "api_matches": ap.matches,
                    "rank": rank,
                    "sackmann_player_key": sp.key,
                    "sackmann_name": sp.name,
                    "sackmann_matches": sp.matches,
                    "score": round(sc, 6),
                    "method": method,
                }
            )

        if not top:
            status = "unmapped"
            target = None
            conf = 0.0
            method = "no_candidate"
            margin = None
        else:
            best_sc, best_method, best_sp = top[0]
            second_sc = top[1][0] if len(top) > 1 else 0.0
            margin = best_sc - second_sc

            accept = False

            # Strong deterministic matches do not need a large margin.
            # They only need to be unique among candidates with the same strong method.
            if best_method == "exact_normalized" and best_sc >= 0.999:
                same_exact = [c for c in top if c[1] == "exact_normalized" and c[0] >= best_sc - 0.001]
                accept = len(same_exact) == 1
            elif best_method == "token_set_exact" and best_sc >= 0.980:
                same_token_exact = [c for c in top if c[1] == "token_set_exact" and c[0] >= best_sc - 0.005]
                accept = len(same_token_exact) == 1
            elif best_method == "compact_no_space" and best_sc >= 0.980:
                same_compact = [c for c in top if c[1] == "compact_no_space" and c[0] >= best_sc - 0.005]
                accept = len(same_compact) == 1
            elif best_method == "exact_initial_form" and best_sc >= 0.965 and margin >= 0.020:
                accept = True
            elif best_sc >= AUTO_SCORE_MIN and margin >= AUTO_MARGIN_MIN:
                accept = True
            elif best_method == "initial_surname" and best_sc >= INITIAL_SURNAME_SCORE_MIN and margin >= AUTO_MARGIN_MIN:
                same_initial_surname = [c for c in top if c[1] == "initial_surname" and c[0] >= best_sc - AMBIGUOUS_MARGIN]
                accept = len(same_initial_surname) == 1

            if accept:
                status = "auto_mapped"
                target = best_sp.key
                conf = best_sc
                method = best_method
            else:
                status = "ambiguous" if best_sc >= 0.830 else "unmapped"
                target = None
                conf = best_sc
                method = best_method

        mapping["mapping"][api_key] = {
            "status": status,
            "sackmann_player_key": target,
            "api_name": name,
            "gender": ap.gender,
            "confidence": round(conf, 6),
            "method": method,
            "margin": None if margin is None else round(margin, 6),
            "api_matches": ap.matches,
            "api_name_variants": dict(ap.names.most_common(10)),
            "api_name_sources": dict(ap.name_sources),
            "api_data_sources": dict(ap.data_sources),
            "api_levels": dict(ap.levels),
            "api_surfaces": dict(ap.surfaces),
        }

        counters[status] += 1
        if target:
            reverse[target].append(api_key)

        if status != "auto_mapped":
            best = top[0][2] if top else None
            review_rows.append(
                {
                    "api_player_key": api_key,
                    "api_name": name,
                    "gender": ap.gender,
                    "api_matches": ap.matches,
                    "status": status,
                    "best_score": round(conf, 6),
                    "best_method": method,
                    "best_sackmann_key": best.key if best else "",
                    "best_sackmann_name": best.name if best else "",
                    "second_score_margin": "" if margin is None else round(margin, 6),
                    "api_name_sources": json.dumps(dict(ap.name_sources), ensure_ascii=False, sort_keys=True),
                    "api_data_sources": json.dumps(dict(ap.data_sources), ensure_ascii=False, sort_keys=True),
                    "api_levels": json.dumps(dict(ap.levels), ensure_ascii=False, sort_keys=True),
                    "api_surfaces": json.dumps(dict(ap.surfaces), ensure_ascii=False, sort_keys=True),
                }
            )

    for sack_key, api_keys in reverse.items():
        if len(api_keys) > 3:
            issue_rows.append(
                {
                    "api_player_key": ";".join(api_keys),
                    "api_name": "",
                    "issue": "many_api_aliases_for_one_sackmann_player",
                    "detail": sack_key,
                }
            )
            counters["issue_many_api_aliases"] += 1

    mapping["summary"] = dict(counters)
    mapping["api_player_cache"] = api_player_cache_report
    mapping["policy"] = {
        "auto_score_min": AUTO_SCORE_MIN,
        "auto_margin_min": AUTO_MARGIN_MIN,
        "initial_surname_score_min": INITIAL_SURNAME_SCORE_MIN,
        "manual_overrides": str(OVERRIDES_JSON),
        "principle": "Accept only high-confidence or unique initial-surname matches; ambiguous candidates are sent to review.",
        "performance_note": "Fast version uses indexed candidate pools by gender/name/surname/initial; acceptance thresholds are unchanged.",
        "name_enrichment": "API names are enriched from data/raw/api_tennis/players/api_players.json before matching.",
        "raw_player_inclusion": "Mapping includes both imported API source players and raw API odds/fixtures/results players, so today upcoming players get mapping entries before they have finished results.",
        "strong_auto_accept": "Unique exact_normalized, token_set_exact, and compact_no_space matches are auto-accepted even when score margin is small.",
    }

    write_json(MAPPING_JSON, mapping)

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "outputs": {
            "mapping_json": str(MAPPING_JSON),
            "review_csv": str(REVIEW_CSV),
            "candidates_csv": str(CANDIDATES_CSV),
            "issues_csv": str(ISSUES_CSV),
            "overrides_json": str(OVERRIDES_JSON),
        },
        "api_player_cache": api_player_cache_report,
        "counters": dict(counters),
        "api_players": len(api_players),
        "sackmann_players": len(sack_players),
        "review_needed": len(review_rows),
        "issues_total": len(issue_rows),
    }
    write_json(REPORT_JSON, report)

    write_csv(
        REVIEW_CSV,
        review_rows,
        [
            "api_player_key",
            "api_name",
            "gender",
            "api_matches",
            "status",
            "best_score",
            "best_method",
            "best_sackmann_key",
            "best_sackmann_name",
            "second_score_margin",
            "api_name_sources",
            "api_data_sources",
            "api_levels",
            "api_surfaces",
        ],
    )
    write_csv(
        CANDIDATES_CSV,
        candidate_rows,
        [
            "api_player_key",
            "api_name",
            "gender",
            "api_matches",
            "rank",
            "sackmann_player_key",
            "sackmann_name",
            "sackmann_matches",
            "score",
            "method",
        ],
    )
    write_csv(ISSUES_CSV, issue_rows, ["api_player_key", "api_name", "issue", "detail"])

    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-player-cache", type=Path, default=API_PLAYER_CACHE_JSON)
    parser.add_argument("--raw-root", type=Path, default=API_RAW_ROOT)
    parser.add_argument("--no-raw", action="store_true", help="Only map players from imported API source; skip raw odds/fixtures/results players.")
    args = parser.parse_args(argv)
    build_mapping(args.api_player_cache, include_raw=not args.no_raw, raw_root=args.raw_root)


if __name__ == "__main__":
    main()

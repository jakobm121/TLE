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
from typing import Any

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
API_PLAYER_CACHE_JSON = Path("data/raw/api_tennis/players/api_players.json")

RATINGS_JSON = Path("data/ratings/tle_player_ratings.json")
RATINGS_JSON_GZ = Path("data/ratings/tle_player_ratings.json.gz")

METADATA_DIR = Path("data/metadata/api_tennis")
REPORT_DIR = Path("data/reports/api_tennis")

MAPPING_JSON = METADATA_DIR / "player_mapping.json"
OVERRIDES_JSON = METADATA_DIR / "player_mapping_overrides.json"
PLAYER_ALIASES_JSON = Path("data/metadata/sackmann/player_aliases.json")
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


def load_player_aliases(path: Path = PLAYER_ALIASES_JSON) -> dict[str, str]:
    data = read_json_safe(path, {})
    if not isinstance(data, dict):
        return {}

    aliases: dict[str, str] = {}
    for k, v in data.items():
        kk = str(k or "").strip()
        vv = str(v or "").strip()
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


def resolve_player_alias(player_key: str, aliases: dict[str, str], counters: Counter | None = None, prefix: str = "alias") -> str:
    original = str(player_key or "").strip()
    if not original:
        return original

    current = original
    seen: set[str] = set()
    while current in aliases:
        if current in seen:
            if counters is not None:
                counters[f"{prefix}_cycle_detected"] += 1
            return current
        seen.add(current)
        current = aliases[current]

    if counters is not None and current != original:
        counters[f"{prefix}_resolved_player_keys"] += 1

    return current


def add_counts(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    out = dict(dst or {})
    for k, v in dict(src or {}).items():
        try:
            out[k] = int(out.get(k) or 0) + int(v or 0)
        except Exception:
            out[k] = out.get(k, v)
    return out


def read_jsonl_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def norm_text(value: Any) -> str:
    return str(value or "").strip()


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


def token_key_from_tokens(ts: list[str]) -> str:
    return " ".join(sorted(t for t in ts if t))


def initials_from_token_list(ts: list[str]) -> str:
    return "".join(t[0] for t in ts if t)


def one_char_initial_tokens(ts: list[str]) -> list[str]:
    return [t for t in ts if len(t) == 1 and t.isalpha()]


def non_initial_tokens(ts: list[str]) -> list[str]:
    return [t for t in ts if len(t) > 1]


def tokens(s: str) -> list[str]:
    return tokens_from_norm(normalize_name(s))


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


def last_token_from_norm(norm: str) -> str:
    ts = tokens_from_norm(norm)
    return ts[-1] if ts else ""


def last_token(s: str) -> str:
    return last_token_from_norm(normalize_name(s))


def surname_tokens(s: str) -> list[str]:
    return surname_tokens_from_tokens(tokens(s))


def initials(s: str) -> str:
    return initials_from_tokens(tokens(s))


def compact_initial_form(s: str) -> str:
    return compact_initial_from_tokens(tokens(s))


def looks_like_doubles_or_team(name: str) -> bool:
    if "/" in (name or ""):
        return True
    ts = tokens(name)
    if len(ts) <= 2 and any(t in COUNTRY_WORDS for t in ts):
        return True
    return False


def ratio_norm(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def token_jaccard_norm(an: str, sn: str) -> float:
    aa, bb = set(tokens_from_norm(an)), set(tokens_from_norm(sn))
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def near_unordered_token_match(api_tokens: list[str], sack_tokens: list[str]) -> bool:
    """Candidate-only: same number of name tokens, order-insensitive,
    allowing one small typo/transliteration.

    Example:
      "garbiela vasilescu arina" -> "arina gabriela vasilescu"

    This is intentionally conservative:
    - requires 3+ tokens
    - same token count
    - at least two exact shared tokens
    - all remaining tokens must be very similar
    """
    api_tokens = [t for t in api_tokens if t]
    sack_tokens = [t for t in sack_tokens if t]
    if len(api_tokens) < 3 or len(api_tokens) != len(sack_tokens):
        return False

    api_remaining = list(api_tokens)
    sack_remaining = list(sack_tokens)
    exact_shared = 0

    # Remove exact token matches first, regardless of order.
    for t in list(api_remaining):
        if t in sack_remaining:
            api_remaining.remove(t)
            sack_remaining.remove(t)
            exact_shared += 1

    if exact_shared < 2:
        return False

    if len(api_remaining) != len(sack_remaining):
        return False

    # Remaining unmatched tokens must be typo-close.
    for at in api_remaining:
        best_i = -1
        best_ratio = 0.0
        for i, st in enumerate(sack_remaining):
            r = ratio_norm(at, st)
            if r > best_ratio:
                best_ratio = r
                best_i = i
        if best_i < 0 or best_ratio < 0.84:
            return False
        sack_remaining.pop(best_i)

    return True


def initial_surname_match_pre(api_initial: str, api_surname: str, sack_initial: str, sack_surname: str) -> bool:
    return bool(api_initial and api_surname and api_surname == sack_surname and api_initial == sack_initial)


def multi_initial_surname_match(api_feat: dict[str, Any], sp: "SackPlayer") -> bool:
    """Candidate-only helper for API short forms such as V. N. Sarganella.

    Requirements:
    - same surname
    - API has at least two one-letter initials
    - those initials appear in Sackmann player's token initials in the same order
    - at least one non-initial API token overlaps Sackmann tokens; usually the surname
    """
    if not api_feat.get("surname") or api_feat.get("surname") != sp.surname:
        return False
    api_initials = api_feat.get("initial_tokens") or []
    if len(api_initials) < 2:
        return False
    sp_initials = sp.all_initials or ""
    pos = -1
    for ini in api_initials:
        pos = sp_initials.find(ini, pos + 1)
        if pos < 0:
            return False
    api_non_initial = set(api_feat.get("non_initial_tokens") or [])
    if not api_non_initial:
        return False
    return bool(api_non_initial & set(sp.toks))


def expanded_initials_surname_match(api_feat: dict[str, Any], sp: "SackPlayer") -> bool:
    """Candidate-only helper for API expanded names vs Sackmann initials.

    Example:
      API       "John Wolf Jeffrey"
      Sackmann  "J.J. Wolf" / "J J Wolf"

    Requirements:
    - Sackmann candidate has one-letter initial tokens and a surname
    - Sackmann surname appears somewhere in the API tokens, even if API order is wrong
    - the one-letter Sackmann initials can be matched by API non-surname name tokens
    """
    api_tokens = api_feat.get("tokens") or []
    if not api_tokens or not sp.surname:
        return False

    sp_initial_tokens = one_char_initial_tokens(sp.toks)
    if len(sp_initial_tokens) < 2:
        return False

    surname_parts = set(tokens_from_norm(sp.surname))
    if not surname_parts or not surname_parts.issubset(set(api_tokens)):
        return False

    api_name_tokens = [t for t in api_tokens if t not in surname_parts and len(t) > 1]
    if not api_name_tokens:
        return False

    available_initials = [t[0] for t in api_name_tokens]
    used = [False] * len(available_initials)

    for ini in sp_initial_tokens:
        found = False
        for i, api_ini in enumerate(available_initials):
            if not used[i] and api_ini == ini:
                used[i] = True
                found = True
                break
        if not found:
            return False

    return True


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
    token_key: str = ""
    all_initials: str = ""


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
    sp.token_key = token_key_from_tokens(sp.toks)
    sp.all_initials = initials_from_token_list(sp.toks)
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


def load_sack_players(player_aliases: dict[str, str] | None = None, counters: Counter | None = None) -> dict[str, SackPlayer]:
    aliases = player_aliases or {}
    path = RATINGS_JSON if RATINGS_JSON.exists() else RATINGS_JSON_GZ
    data = read_json_any(path)
    players = data.get("players", data) if isinstance(data, dict) else {}
    out: dict[str, SackPlayer] = {}

    for key, p in players.items():
        raw_key = str(key)
        canonical_key = resolve_player_alias(raw_key, aliases, counters, "sackmann_candidate_alias")
        gender = p.get("gender") or (canonical_key.split(":", 1)[0] if ":" in canonical_key else "")
        name = p.get("name") or ""
        if not gender or not name:
            continue

        matches = int(p.get("matches") or 0)
        levels = dict(p.get("level") or {})
        surfaces = dict(p.get("surface") or {})

        if canonical_key in out:
            # Merge duplicate Sackmann IDs into one candidate profile so 09 does not
            # offer alias IDs as separate candidate players.
            existing = out[canonical_key]
            existing.matches += matches
            existing.levels = add_counts(existing.levels, levels)
            existing.surfaces = add_counts(existing.surfaces, surfaces)

            # Prefer the existing canonical name, but if it is empty for some reason,
            # keep the alias name.
            if not existing.name and name:
                existing.name = name

            decorate_sack_player(existing)
            if counters is not None and canonical_key != raw_key:
                counters["sackmann_alias_candidate_merged"] += 1
            continue

        sp = SackPlayer(
            key=canonical_key,
            gender=gender,
            name=name,
            matches=matches,
            levels=levels,
            surfaces=surfaces,
        )
        out[canonical_key] = decorate_sack_player(sp)

        if counters is not None and canonical_key != raw_key:
            counters["sackmann_alias_candidate_created_from_alias"] += 1

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


def load_api_players(
    api_player_cache: dict[str, dict[str, Any]],
    counters: Counter,
) -> dict[str, ApiPlayer]:
    api_players: dict[str, ApiPlayer] = {}

    for path in sorted(API_SOURCE_DIR.glob("tle_api_matches_*.jsonl.gz")):
        for m in read_jsonl_gz(path):
            gender = m.get("gender") or ""
            level = m.get("level") or "unknown"
            surface = m.get("surface") or "unknown"
            tournament = m.get("tourney_name") or m.get("tournament_name") or ""

            w = extract_player(m, "winner")
            l = extract_player(m, "loser")
            if not w or not l or gender not in {"men", "women"}:
                continue

            for (key, raw_name), (_opp_key, raw_opp_name) in ((w, l), (l, w)):
                if looks_like_doubles_or_team(raw_name):
                    continue

                bare = canonical_bare_api_key(key)
                canonical_api_key = f"{gender}:api:{bare}" if bare else str(key)

                name, name_source = cached_api_player_name(api_player_cache, canonical_api_key, raw_name)
                opp_name, _opp_name_source = cached_api_player_name(api_player_cache, _opp_key, raw_opp_name)

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
                ap.opponents[opp_name] += 1
                ap.tournaments[tournament] += 1
                ap.name_sources[name_source] += 1

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
    by_token_key: dict[tuple[str, str], list[SackPlayer]]


def build_sack_index(sack_players: dict[str, SackPlayer]) -> SackIndex:
    idx = SackIndex(defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list))
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
        if sp.token_key:
            idx.by_token_key[(sp.gender, sp.token_key)].append(sp)
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
        "token_key": token_key_from_tokens(ts),
        "all_initials": initials_from_token_list(ts),
        "initial_tokens": one_char_initial_tokens(ts),
        "non_initial_tokens": non_initial_tokens(ts),
    }


def score_candidate_features(api_feat: dict[str, Any], sp: SackPlayer) -> tuple[float, str]:
    an = api_feat["norm"]
    sn = sp.norm

    if not an or not sn:
        return 0.0, "empty"

    if an == sn:
        return 1.0, "exact_normalized"

    # Exact same name tokens regardless of order:
    # e.g. "John Wolf Jeffrey" == "Jeffrey John Wolf".
    # This is strong enough to auto-map when there is no close duplicate.
    if api_feat.get("token_key") and api_feat.get("token_key") == sp.token_key:
        return 0.995, "exact_token_set"

    if api_feat["compact"] and api_feat["compact"] == sp.compact:
        return 0.970, "exact_initial_form"

    if initial_surname_match_pre(api_feat["initial"], api_feat["surname"], sp.initial, sp.surname):
        base = ratio_norm(an, sn)
        tj = token_jaccard_norm(an, sn)
        score = max(0.900, min(0.970, 0.84 + 0.10 * base + 0.06 * tj))
        return score, "initial_surname"

    # Candidate-only: two or more initials + same surname,
    # e.g. "V. N. Sarganella" -> "Virginia Nora Sarganella".
    # Candidate-only rules must run after stronger exact/compact/initial-surname
    # rules so they cannot downgrade a previously auto-mapped candidate.
    if multi_initial_surname_match(api_feat, sp):
        return 0.920, "multi_initial_surname"

    # Candidate-only: API expanded full name vs Sackmann initials,
    # e.g. "John Wolf Jeffrey" -> "J.J. Wolf".
    # Kept below AUTO_SCORE_MIN so it goes to review, not auto-map.
    if expanded_initials_surname_match(api_feat, sp):
        return 0.925, "expanded_initials_surname"

    # Candidate-only: unordered full names with one likely typo,
    # e.g. "Garbiela Vasilescu Arina" -> "Arina Gabriela Vasilescu".
    # Kept below AUTO_SCORE_MIN even after match bonus, so it goes to review.
    if near_unordered_token_match(api_feat.get("tokens") or [], sp.toks):
        return 0.915, "near_token_set_typo"

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

    # Strong, cheap candidate sets first.
    if feat["norm"]:
        add_many(idx.by_norm.get((gender, feat["norm"]), []))
    if feat.get("token_key"):
        add_many(idx.by_token_key.get((gender, feat["token_key"]), []))
    if feat["compact"]:
        add_many(idx.by_compact.get((gender, feat["compact"]), []))
    if feat["surname"]:
        add_many(idx.by_surname.get((gender, feat["surname"]), []))
    if feat["last"]:
        add_many(idx.by_last.get((gender, feat["last"]), []))

    # If still too few candidates, use same initial but cheaply pre-filter by token overlap / short ratio.
    if len(pool_by_key) < 4 and feat["initial"]:
        an = feat["norm"]
        api_token_set = set(feat["tokens"])
        for sp in idx.by_initial.get((gender, feat["initial"]), []):
            if sp.key in pool_by_key:
                continue
            if api_token_set & set(sp.toks) or ratio_norm(an, sp.norm) >= 0.55:
                pool_by_key[sp.key] = sp

    # Rare fallback for transliteration / API typo.
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


def build_mapping(api_player_cache_path: Path = API_PLAYER_CACHE_JSON, rebuild_auto_mapping: bool = False, player_aliases_path: Path = PLAYER_ALIASES_JSON) -> None:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    counters = Counter()

    player_aliases = load_player_aliases(player_aliases_path)
    counters["player_aliases_loaded"] = len(player_aliases)
    counters["rebuild_auto_mapping"] = int(bool(rebuild_auto_mapping))

    sack_players = load_sack_players(player_aliases, counters)
    api_player_cache, api_player_cache_report = load_api_player_cache(api_player_cache_path)
    api_players = load_api_players(api_player_cache, counters)
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

            target_original = target
            if target:
                target = resolve_player_alias(target, player_aliases, counters, "override_alias")
                if target != target_original:
                    counters["overrides_alias_targets_resolved"] += 1
                    issue_rows.append({
                        "api_player_key": api_key,
                        "api_name": name,
                        "issue": "override_alias_target_resolved",
                        "detail": f"{target_original} -> {target}",
                    })

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

        # Collapse any remaining duplicate candidates that resolve to the same
        # canonical Sackmann key. Keep the highest-scoring version.
        best_by_key: dict[str, tuple[float, str, SackPlayer]] = {}
        for sc0, method0, sp0 in candidates:
            old = best_by_key.get(sp0.key)
            if old is None or sc0 > old[0]:
                best_by_key[sp0.key] = (sc0, method0, sp0)

        candidates = list(best_by_key.values())
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
            if best_sc >= AUTO_SCORE_MIN and margin >= AUTO_MARGIN_MIN:
                accept = True
            elif best_method in {"exact_normalized", "exact_token_set", "exact_initial_form"} and best_sc >= 0.965 and margin >= 0.020:
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
        "player_aliases": str(player_aliases_path),
        "rebuild_auto_mapping": bool(rebuild_auto_mapping),
        "principle": "Accept only high-confidence or unique initial-surname matches; ambiguous candidates are sent to review.",
        "performance_note": "Fast version uses indexed candidate pools by gender/name/surname/initial; acceptance thresholds are unchanged.",
        "name_enrichment": "API source names are enriched from data/raw/api_tennis/players/api_players.json before matching.",
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
            "player_aliases_json": str(player_aliases_path),
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
    parser.add_argument("--player-aliases", type=Path, default=PLAYER_ALIASES_JSON)
    parser.add_argument(
        "--rebuild-auto-mapping",
        action="store_true",
        help="Clean rebuild mode. Existing player_mapping.json is ignored; overrides are still applied if present.",
    )
    args = parser.parse_args(argv)
    build_mapping(
        api_player_cache_path=args.api_player_cache,
        rebuild_auto_mapping=args.rebuild_auto_mapping,
        player_aliases_path=args.player_aliases,
    )


if __name__ == "__main__":
    main()

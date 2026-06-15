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
    "barbados", "venezuela", "costa", "rica", "guatemala", "puerto", "rico", "jamaica", "georgia",
    "kosovo", "ireland", "montenegro", "latvia", "azerbaijan", "north", "macedonia", "moldova",
    "serbia", "slovenia", "croatia", "france", "spain", "italy", "germany", "belgium", "china",
    "japan", "usa", "australia", "canada", "brazil", "argentina", "poland", "ukraine", "kazakhstan",
}


def read_json_any(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def normalize_name(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = s.replace("'", " ").replace("`", " ").replace("â", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens_from_norm(norm: str) -> list[str]:
    return [t for t in (norm or "").split() if t]


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
    return sp


def api_key_variants(api_key: str, gender: str) -> list[str]:
    raw = str(api_key)
    if raw.startswith("men:api") or raw.startswith("women:api"):
        bare = raw.split(":")[-1]
    else:
        bare = raw
    return [
        raw,
        f"{gender}:api:{bare}",
        f"{gender}:api_tennis:{bare}",
    ]


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


def load_api_players() -> dict[str, ApiPlayer]:
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
            for (key, name), (_opp_key, opp_name) in ((w, l), (l, w)):
                if looks_like_doubles_or_team(name):
                    continue
                canonical_api_key = key if key.startswith(f"{gender}:api") else f"{gender}:api:{key}"
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


def build_sack_index(sack_players: dict[str, SackPlayer]) -> SackIndex:
    idx = SackIndex(defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list))
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
    }


def score_candidate_features(api_feat: dict[str, Any], sp: SackPlayer) -> tuple[float, str]:
    an = api_feat["norm"]
    sn = sp.norm
    if not an or not sn:
        return 0.0, "empty"
    if an == sn:
        return 1.0, "exact_normalized"

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

    # Strong, cheap candidate sets first.
    if feat["norm"]:
        add_many(idx.by_norm.get((gender, feat["norm"]), []))
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
            # Avoid old O(N) fuzzy over all players; this is initial bucket only.
            if api_token_set & set(sp.toks) or ratio_norm(an, sp.norm) >= 0.55:
                pool_by_key[sp.key] = sp

    # Rare fallback for transliteration / API typo. This can still scan same-gender, but only when indexed pool failed.
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


def build_mapping() -> None:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    sack_players = load_sack_players()
    api_players = load_api_players()
    overrides = load_overrides()
    sack_index = build_sack_index(sack_players)

    mapping: dict[str, Any] = {
        "generated_at": now_utc_iso(),
        "source": "api_tennis",
        "target": "sackmann",
        "mapping": {},
    }

    counters = Counter()
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
            candidate_rows.append({
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
            })

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
            elif best_method in {"exact_normalized", "exact_initial_form"} and best_sc >= 0.965 and margin >= 0.020:
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
            "api_levels": dict(ap.levels),
            "api_surfaces": dict(ap.surfaces),
        }
        counters[status] += 1
        if target:
            reverse[target].append(api_key)
        if status != "auto_mapped":
            best = top[0][2] if top else None
            review_rows.append({
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
                "api_levels": json.dumps(dict(ap.levels), ensure_ascii=False, sort_keys=True),
                "api_surfaces": json.dumps(dict(ap.surfaces), ensure_ascii=False, sort_keys=True),
            })

    for sack_key, api_keys in reverse.items():
        if len(api_keys) > 3:
            issue_rows.append({
                "api_player_key": ";".join(api_keys),
                "api_name": "",
                "issue": "many_api_aliases_for_one_sackmann_player",
                "detail": sack_key,
            })
            counters["issue_many_api_aliases"] += 1

    mapping["summary"] = dict(counters)
    mapping["policy"] = {
        "auto_score_min": AUTO_SCORE_MIN,
        "auto_margin_min": AUTO_MARGIN_MIN,
        "initial_surname_score_min": INITIAL_SURNAME_SCORE_MIN,
        "manual_overrides": str(OVERRIDES_JSON),
        "principle": "Accept only high-confidence or unique initial-surname matches; ambiguous candidates are sent to review.",
        "performance_note": "Fast version uses indexed candidate pools by gender/name/surname/initial; acceptance thresholds are unchanged.",
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
        "counters": dict(counters),
        "api_players": len(api_players),
        "sackmann_players": len(sack_players),
        "review_needed": len(review_rows),
        "issues_total": len(issue_rows),
    }
    write_json(REPORT_JSON, report)

    def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    write_csv(REVIEW_CSV, review_rows, [
        "api_player_key", "api_name", "gender", "api_matches", "status", "best_score", "best_method",
        "best_sackmann_key", "best_sackmann_name", "second_score_margin", "api_levels", "api_surfaces",
    ])
    write_csv(CANDIDATES_CSV, candidate_rows, [
        "api_player_key", "api_name", "gender", "api_matches", "rank", "sackmann_player_key",
        "sackmann_name", "sackmann_matches", "score", "method",
    ])
    write_csv(ISSUES_CSV, issue_rows, ["api_player_key", "api_name", "issue", "detail"])

    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args(argv)
    build_mapping()


if __name__ == "__main__":
    main()

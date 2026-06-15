from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
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


def tokens(s: str) -> list[str]:
    return [t for t in normalize_name(s).split() if t]


def last_token(s: str) -> str:
    ts = tokens(s)
    return ts[-1] if ts else ""


def surname_tokens(s: str) -> list[str]:
    ts = tokens(s)
    if not ts:
        return []
    if len(ts) >= 2 and f"{ts[-2]} {ts[-1]}" in PARTICLES:
        return ts[-2:]
    return [ts[-1]]


def initials(s: str) -> str:
    return "".join(t[0] for t in tokens(s) if t)


def looks_like_doubles_or_team(name: str) -> bool:
    n = normalize_name(name)
    if "/" in (name or ""):
        return True
    ts = tokens(name)
    if len(ts) <= 2 and any(t in COUNTRY_WORDS for t in ts):
        return True
    return False


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def token_jaccard(a: str, b: str) -> float:
    aa, bb = set(tokens(a)), set(tokens(b))
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def initial_surname_match(api_name: str, sack_name: str) -> bool:
    api_ts = tokens(api_name)
    sack_ts = tokens(sack_name)
    if len(api_ts) < 2 or len(sack_ts) < 2:
        return False
    api_last = surname_tokens(api_name)
    sack_last = surname_tokens(sack_name)
    if not api_last or not sack_last:
        return False
    if " ".join(api_last) != " ".join(sack_last):
        return False
    return api_ts[0][0] == sack_ts[0][0]


def compact_initial_form(s: str) -> str:
    ts = tokens(s)
    if not ts:
        return ""
    if len(ts) == 1:
        return ts[0]
    return f"{ts[0][0]} {' '.join(ts[1:])}"


def score_candidate(api_name: str, sack_name: str) -> tuple[float, str]:
    an = normalize_name(api_name)
    sn = normalize_name(sack_name)
    if not an or not sn:
        return 0.0, "empty"
    if an == sn:
        return 1.0, "exact_normalized"

    api_compact = compact_initial_form(api_name)
    sack_compact = compact_initial_form(sack_name)
    if api_compact and api_compact == sack_compact:
        return 0.970, "exact_initial_form"

    if initial_surname_match(api_name, sack_name):
        base = ratio(an, sn)
        tj = token_jaccard(api_name, sack_name)
        score = max(0.900, min(0.970, 0.84 + 0.10 * base + 0.06 * tj))
        return score, "initial_surname"

    r = ratio(an, sn)
    tj = token_jaccard(api_name, sack_name)
    last_bonus = 0.06 if last_token(api_name) and last_token(api_name) == last_token(sack_name) else 0.0
    init_bonus = 0.03 if initials(api_name) and initials(sack_name) and initials(api_name)[0] == initials(sack_name)[0] else 0.0
    score = min(0.999, 0.72 * r + 0.18 * tj + last_bonus + init_bonus)
    method = "fuzzy"
    if last_bonus:
        method = "fuzzy_same_last"
    return score, method


@dataclass
class SackPlayer:
    key: str
    gender: str
    name: str
    matches: int
    levels: dict[str, Any]
    surfaces: dict[str, Any]


@dataclass
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
        out[key] = SackPlayer(
            key=key,
            gender=gender,
            name=name,
            matches=int(p.get("matches") or 0),
            levels=dict(p.get("level") or {}),
            surfaces=dict(p.get("surface") or {}),
        )
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
            for (key, name), (opp_key, opp_name) in ((w, l), (l, w)):
                if looks_like_doubles_or_team(name):
                    continue
                if key not in api_players:
                    api_players[key] = ApiPlayer(
                        key=key,
                        gender=gender,
                        names=Counter(),
                        matches=0,
                        levels=Counter(),
                        surfaces=Counter(),
                        opponents=Counter(),
                        tournaments=Counter(),
                    )
                ap = api_players[key]
                ap.names[name] += 1
                ap.matches += 1
                ap.levels[level] += 1
                ap.surfaces[surface] += 1
                ap.opponents[opp_name] += 1
                ap.tournaments[tournament] += 1
    return api_players


def load_overrides() -> dict[str, str | None]:
    if not OVERRIDES_JSON.exists():
        OVERRIDES_JSON.parent.mkdir(parents=True, exist_ok=True)
        write_json(OVERRIDES_JSON, {})
        return {}
    data = read_json_any(OVERRIDES_JSON)
    if not isinstance(data, dict):
        return {}
    return {str(k): (None if v in {None, "", "null"} else str(v)) for k, v in data.items()}


def candidate_pool(api_player: ApiPlayer, sack_by_gender: dict[str, list[SackPlayer]]) -> list[SackPlayer]:
    name = api_player.name
    lt = last_token(name)
    st = " ".join(surname_tokens(name))
    init = initials(name)[:1]
    pool = []
    for sp in sack_by_gender.get(api_player.gender, []):
        if lt and lt == last_token(sp.name):
            pool.append(sp)
            continue
        if st and st == " ".join(surname_tokens(sp.name)):
            pool.append(sp)
            continue
        if init and initials(sp.name).startswith(init):
            # Keep only plausible fuzzy candidates to avoid O(N) huge list.
            if ratio(normalize_name(name), normalize_name(sp.name)) >= 0.55:
                pool.append(sp)
    if not pool:
        # rare fallback for transliteration: top fuzzy from all same-gender players
        scored = []
        for sp in sack_by_gender.get(api_player.gender, []):
            sc, _ = score_candidate(name, sp.name)
            if sc >= 0.78:
                scored.append((sc, sp))
        scored.sort(key=lambda x: x[0], reverse=True)
        pool = [sp for _, sp in scored[:20]]
    return pool


def build_mapping() -> None:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    sack_players = load_sack_players()
    api_players = load_api_players()
    overrides = load_overrides()

    sack_by_gender: dict[str, list[SackPlayer]] = defaultdict(list)
    for sp in sack_players.values():
        sack_by_gender[sp.gender].append(sp)

    mapping: dict[str, Any] = {
        "generated_at": now_utc_iso(),
        "source": "api_tennis",
        "target": "sackmann",
        "mapping": {},
    }

    counters = Counter()
    review_rows = []
    candidate_rows = []
    issue_rows = []
    reverse: dict[str, list[str]] = defaultdict(list)

    for api_key, ap in sorted(api_players.items()):
        counters["api_players"] += 1
        counters[f"api_gender_{ap.gender}"] += 1
        name = ap.name

        if api_key in overrides:
            target = overrides[api_key]
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
                "api_matches": ap.matches,
            }
            counters[status] += 1
            if target:
                reverse[target].append(api_key)
            continue

        candidates = []
        for sp in candidate_pool(ap, sack_by_gender):
            sc, method = score_candidate(name, sp.name)
            if sc < 0.760:
                continue
            # Slightly prefer active/relevant players with more matches, but cap the bonus.
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
                # Accept only if surname+initial is essentially unique.
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
            "top_candidate": candidate_rows[-1] if False else None,
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

    # one Sackmann player can have API aliases, but too many is suspicious.
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

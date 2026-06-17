from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from .utils import now_utc_iso, write_json
except Exception:
    def now_utc_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


RAW_ROOT = Path("data/raw/api_tennis")
API_SOURCE_DIR = Path("data/source/api_tennis")
MAPPING_JSON = Path("data/metadata/api_tennis/player_mapping.json")
OVERRIDES_JSON = Path("data/metadata/api_tennis/player_mapping_overrides.json")
CANDIDATES_CSV = Path("data/reports/api_tennis/player_mapping_candidates.csv")
REPORT_DIR = Path("data/reports/api_tennis")

API_PLAYER_CACHE_JSON = Path("data/raw/api_tennis/players/api_players.json")

ACCEPTED_STATUSES = {"auto_mapped", "manual_mapped"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_safe(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return read_json(path)
    except Exception:
        return default


def read_json_any(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return read_json(path)


def iter_jsonl_gz(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                obj = json.loads(line)
                if isinstance(obj, dict):
                    yield obj


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def norm_text(value: Any) -> str:
    return str(value or "").strip()


def unwrap_api_result(obj: Any) -> list[Any]:
    if isinstance(obj, dict):
        if isinstance(obj.get("result"), list):
            return obj["result"]
        resp = obj.get("response")
        if isinstance(resp, dict) and isinstance(resp.get("result"), list):
            return resp["result"]
        if isinstance(obj.get("data"), list):
            return obj["data"]
    if isinstance(obj, list):
        return obj
    return []


def get_any(d: dict[str, Any], keys: list[str]) -> Any:
    for k in keys:
        if k in d and d.get(k) not in (None, ""):
            return d.get(k)
    return None


def normalize_gender(raw: Any) -> str:
    s = str(raw or "").lower()
    if "women" in s or "wta" in s or "female" in s or "girls" in s:
        return "women"
    if "men" in s or "atp" in s or "male" in s or "boys" in s or "challenger" in s:
        return "men"
    return ""


def api_key_variants(raw_key: str, gender: str = "") -> list[str]:
    k = str(raw_key or "").strip()
    if not k:
        return []

    variants: list[str] = []
    if k not in variants:
        variants.append(k)

    genders = [gender] if gender in {"men", "women"} else []

    # If key already includes gender, also create api/api_tennis alias variants.
    m = re.match(r"^(men|women):api(?:_tennis)?:(.+)$", k)
    if m:
        g, bare = m.group(1), m.group(2)
        if g not in genders:
            genders.append(g)
        if bare not in variants:
            variants.append(bare)
    else:
        bare = k

    for g in genders:
        for prefix in ("api", "api_tennis"):
            kk = f"{g}:{prefix}:{bare}"
            if kk not in variants:
                variants.append(kk)

    return variants


def display_api_key(raw_key: str, gender: str = "") -> str:
    k = str(raw_key or "").strip()
    if not k:
        return ""
    if re.match(r"^(men|women):api(?:_tennis)?:", k):
        return k.replace(":api_tennis:", ":api:")
    if gender in {"men", "women"}:
        return f"{gender}:api:{k}"
    return k


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
    raw_key: str,
    fallback_name: str,
) -> tuple[str, str]:
    """Return (name, source) for raw API player key.

    Priority:
    1. 06b player_full_name
    2. 06b player_name
    3. raw fixture/source name
    """
    key = norm_text(raw_key)
    fallback = norm_text(fallback_name)

    if not key:
        return fallback, "fixture"

    row = api_player_cache.get(key)
    if not isinstance(row, dict):
        return fallback, "fixture"

    full_name = norm_text(row.get("player_full_name"))
    if full_name:
        return full_name, "api_cache_full_name"

    short_name = norm_text(row.get("player_name"))
    if short_name:
        return short_name, "api_cache_short_name"

    return fallback, "fixture"


def load_overrides() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not OVERRIDES_JSON.exists():
        return out

    data = read_json(OVERRIDES_JSON)
    if not isinstance(data, dict):
        return out

    for api_key, val in data.items():
        key = str(api_key or "").strip()
        if not key:
            continue

        if val is None:
            out[key] = {"status": "manual_unmapped", "sackmann_player_key": ""}
        elif isinstance(val, str):
            out[key] = {"status": "manual_mapped", "sackmann_player_key": val, "method": "manual_override"}
        elif isinstance(val, dict):
            target = val.get("target_player_key") or val.get("sackmann_player_key") or val.get("target") or ""
            status = val.get("status") or ("manual_mapped" if target else "manual_unmapped")
            out[key] = {"status": status, "sackmann_player_key": target, "method": "manual_override", **val}

    return out


def raw_player_from_fixture(fx: dict[str, Any], side: str) -> tuple[str, str]:
    if side == "first":
        key = get_any(fx, ["first_player_key", "event_first_player_key", "player_key", "home_player_key", "home_team_key"])
        name = get_any(fx, ["event_first_player", "first_player", "first_player_name", "player_name", "event_home_team", "home_player", "home_team"])
    else:
        key = get_any(fx, ["second_player_key", "event_second_player_key", "away_player_key", "away_team_key"])
        name = get_any(fx, ["event_second_player", "second_player", "second_player_name", "event_away_team", "away_player", "away_team"])
    return str(key or "").strip(), str(name or "").strip()


def canonical_player_from_match(m: dict[str, Any], side: str) -> tuple[str, str]:
    obj = m.get(side) or {}
    if not isinstance(obj, dict):
        return "", ""
    key = get_any(obj, ["player_key", "api_player_key", "key"])
    name = get_any(obj, ["name", "player_name"])
    return str(key or "").strip(), str(name or "").strip()


def find_entry(mapping: dict[str, Any], overrides: dict[str, Any], raw_key: str, gender: str = "") -> tuple[dict[str, Any], str, str]:
    """Return (entry, matched_key, source). Overrides are checked first so today manual fixes work immediately."""
    for k in api_key_variants(raw_key, gender):
        e = overrides.get(k)
        if isinstance(e, dict) and e:
            return e, k, "override"

    for k in api_key_variants(raw_key, gender):
        e = mapping.get(k)
        if isinstance(e, dict) and e:
            return e, k, "mapping"

    return {}, "", ""


def is_entry_mapped(e: dict[str, Any]) -> bool:
    return e.get("status") in ACCEPTED_STATUSES and bool(e.get("sackmann_player_key"))


def is_mapped(mapping: dict[str, Any], overrides: dict[str, Any], raw_key: str, gender: str = "") -> bool:
    e, _, _ = find_entry(mapping, overrides, raw_key, gender)
    return is_entry_mapped(e)


def match_date_from_raw(fx: dict[str, Any]) -> str:
    raw = get_any(fx, ["event_date", "date", "fixture_date", "match_date"])
    if raw:
        s = str(raw)
        m = re.search(r"\d{4}-\d{2}-\d{2}", s)
        if m:
            return m.group(0)
    return ""


def fixture_id(fx: dict[str, Any]) -> str:
    return str(get_any(fx, ["event_key", "fixture_key", "match_key", "id"]) or "").strip()


def tournament_name_raw(fx: dict[str, Any]) -> str:
    return str(get_any(fx, ["tournament_name", "event_name", "league_name", "tournament", "league"]) or "").strip()


def event_type_raw(fx: dict[str, Any]) -> str:
    return str(get_any(fx, ["event_type_type", "event_type", "type", "category_name"]) or "").strip()


def is_non_singles_raw(fx: dict[str, Any], fname: str = "", sname: str = "") -> bool:
    etype = event_type_raw(fx).lower()
    tourn = tournament_name_raw(fx).lower()
    names = f"{fname} {sname}".lower()

    if "double" in etype or "doubles" in etype:
        return True
    if "team" in etype or "teams" in etype:
        return True
    if "davis cup" in tourn or "billie jean" in tourn or "bjk cup" in tourn or "fed cup" in tourn:
        return True
    if "/" in names:
        return True

    return False


def load_today_raw(date_s: str, explicit_inputs: list[Path]) -> list[dict[str, Any]]:
    paths: list[Path] = []
    if explicit_inputs:
        paths.extend(explicit_inputs)
    else:
        candidates = [
            RAW_ROOT / "odds" / f"{date_s}.json",
            RAW_ROOT / "fixtures" / f"{date_s}.json",
            RAW_ROOT / "results" / f"{date_s}.json",
            RAW_ROOT / f"odds_{date_s}.json",
            RAW_ROOT / f"fixtures_{date_s}.json",
            RAW_ROOT / f"{date_s}.json",
        ]
        paths.extend([p for p in candidates if p.exists()])

    out: list[dict[str, Any]] = []
    for p in paths:
        if not p.exists():
            continue
        try:
            rows = unwrap_api_result(read_json_any(p))
        except Exception:
            continue
        for fx in rows:
            if isinstance(fx, dict):
                fx = dict(fx)
                fx["_source_path"] = str(p)
                out.append(fx)

    return out


def load_today_from_source(date_s: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(API_SOURCE_DIR.glob("tle_api_matches_*.jsonl.gz")):
        for m in iter_jsonl_gz(p):
            if str(m.get("date") or "") == date_s:
                mm = dict(m)
                mm["_source_path"] = str(p)
                out.append(mm)
    return out


def load_candidates() -> dict[str, list[dict[str, str]]]:
    by_api: dict[str, list[dict[str, str]]] = defaultdict(list)
    if not CANDIDATES_CSV.exists():
        return by_api

    with CANDIDATES_CSV.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            api_key = str(r.get("api_player_key") or "").strip()
            if not api_key:
                continue

            by_api[api_key].append(r)

            # Also index api/api_tennis aliases and bare key where possible.
            for v in api_key_variants(api_key, str(r.get("gender") or "")):
                by_api[v].append(r)

    return by_api


def candidate_rows_for_api(cands: dict[str, list[dict[str, str]]], api_key: str, gender: str = "") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_targets: set[str] = set()

    for k in api_key_variants(api_key, gender):
        for r in cands.get(k) or []:
            target = (
                r.get("sackmann_player_key")
                or r.get("candidate_sackmann_key")
                or r.get("candidate_key")
                or ""
            )
            if not target or target in seen_targets:
                continue
            seen_targets.add(target)
            rows.append(r)

    def score(r: dict[str, str]) -> float:
        try:
            return float(r.get("score") or 0)
        except Exception:
            return 0.0

    return sorted(rows, key=score, reverse=True)


def best_candidate(cands: dict[str, list[dict[str, str]]], api_key: str, gender: str = "") -> dict[str, str]:
    rows = candidate_rows_for_api(cands, api_key, gender)
    return rows[0] if rows else {}


def candidate_key(row: dict[str, str]) -> str:
    return row.get("sackmann_player_key") or row.get("candidate_sackmann_key") or row.get("candidate_key") or ""


def candidate_name(row: dict[str, str]) -> str:
    return row.get("sackmann_name") or row.get("candidate_name") or row.get("candidate_sackmann_name") or ""


def candidate_score(row: dict[str, str]) -> str:
    return row.get("score") or row.get("candidate_score") or ""


def candidate_method(row: dict[str, str]) -> str:
    return row.get("method") or row.get("candidate_method") or row.get("match_method") or ""


def candidate_matches(row: dict[str, str]) -> str:
    return (
        row.get("sackmann_matches")
        or row.get("candidate_matches")
        or row.get("matches")
        or row.get("match_count")
        or ""
    )


def score_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def candidate_margin(rows: list[dict[str, str]]) -> str:
    if len(rows) < 2:
        return ""
    margin = score_float(candidate_score(rows[0])) - score_float(candidate_score(rows[1]))
    return f"{margin:.6f}"


def is_ambiguous_candidate_set(rows: list[dict[str, str]]) -> bool:
    if len(rows) < 2:
        return False

    top = rows[0]
    second = rows[1]
    margin = score_float(candidate_score(top)) - score_float(candidate_score(second))
    top_method = candidate_method(top).lower()
    second_method = candidate_method(second).lower()

    initial_methods = {"exact_initial_form", "initial_surname", "initial_surname+country"}
    if top_method in initial_methods and second_method in initial_methods and margin < 0.025:
        return True

    return margin < 0.010


def suggested_override(api_gender: str, api_key: str, cand: dict[str, str], *, ambiguous: bool = False) -> str:
    if ambiguous:
        return ""
    target = candidate_key(cand)
    if not target:
        return ""
    return json.dumps({display_api_key(api_key, api_gender): target}, ensure_ascii=False)

def suggested_override(api_gender: str, api_key: str, cand: dict[str, str]) -> str:
    target = cand.get("sackmann_player_key") or cand.get("candidate_sackmann_key") or cand.get("candidate_key") or ""
    if not target:
        return ""
    return json.dumps({display_api_key(api_key, api_gender): target}, ensure_ascii=False)


def upsert_player_record(
    players: dict[str, dict[str, Any]],
    *,
    disp_key: str,
    raw_key: str,
    api_name: str,
    api_name_source: str,
    gender: str,
    entry: dict[str, Any],
    matched_key: str,
    mapping_source: str,
    mapped: bool,
    tournament: str,
    event_type: str,
    opponent: str,
) -> None:
    p = players.setdefault(
        disp_key,
        {
            "api_player_key": disp_key,
            "raw_api_player_key": raw_key,
            "api_name": api_name,
            "api_name_source": api_name_source,
            "gender": entry.get("gender") or gender,
            "mapping_status": entry.get("status", "missing_mapping_entry"),
            "mapping_source": mapping_source,
            "matched_mapping_key": matched_key,
            "sackmann_player_key": entry.get("sackmann_player_key", ""),
            "today_match_count": 0,
            "opponents_today": Counter(),
            "event_names": Counter(),
            "event_type_types": Counter(),
            "mapped": mapped,
        },
    )

    # If same display key appears later with better name from cache, upgrade name for review.
    if api_name_source == "api_cache_full_name" and p.get("api_name_source") != "api_cache_full_name":
        p["api_name"] = api_name
        p["api_name_source"] = api_name_source
    elif api_name_source == "api_cache_short_name" and p.get("api_name_source") == "fixture":
        p["api_name"] = api_name
        p["api_name_source"] = api_name_source

    # If same display key appears later with an override/mapping, upgrade record.
    if mapped and not p.get("mapped"):
        p["mapped"] = True
        p["mapping_status"] = entry.get("status", "")
        p["mapping_source"] = mapping_source
        p["matched_mapping_key"] = matched_key
        p["sackmann_player_key"] = entry.get("sackmann_player_key", "")

    p["today_match_count"] += 1
    p["event_names"][tournament] += 1
    p["event_type_types"][event_type] += 1
    if opponent:
        p["opponents_today"][opponent] += 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat(), help="UTC date YYYY-MM-DD. Default: today UTC.")
    parser.add_argument("--input", action="append", default=[], help="Optional explicit raw JSON file. Can be repeated.")
    parser.add_argument("--source-fallback", action="store_true", help="If no raw odds/fixtures found, audit imported API source matches for the date.")
    parser.add_argument("--api-player-cache", type=Path, default=API_PLAYER_CACHE_JSON)
    args = parser.parse_args(argv)

    date_s = args.date
    explicit_inputs = [Path(p) for p in args.input]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    out_json = REPORT_DIR / f"today_mapping_audit_{date_s}.json"
    out_review = REPORT_DIR / f"today_mapping_review_{date_s}.csv"
    latest_json = REPORT_DIR / "today_mapping_audit.json"
    latest_review = REPORT_DIR / "today_mapping_review.csv"

    if not MAPPING_JSON.exists():
        report = {
            "generated_at": now_utc_iso(),
            "date": date_s,
            "status": "error",
            "error": f"Missing {MAPPING_JSON}",
        }
        write_json(out_json, report)
        write_json(latest_json, report)
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        raise SystemExit(1)

    mapping_obj = read_json(MAPPING_JSON)
    mapping = mapping_obj.get("mapping") if isinstance(mapping_obj, dict) else {}
    if not isinstance(mapping, dict):
        mapping = {}

    overrides = load_overrides()
    api_player_cache, api_player_cache_report = load_api_player_cache(args.api_player_cache)

    raw_rows = load_today_raw(date_s, explicit_inputs)
    source_used = "raw"
    source_rows: list[dict[str, Any]] = []

    if not raw_rows and args.source_fallback:
        source_rows = load_today_from_source(date_s)
        source_used = "api_source"

    candidates = load_candidates()
    counters = Counter()
    counters["api_player_cache_entries"] = api_player_cache_report.get("entries", 0)
    counters["api_player_cache_with_full_name"] = api_player_cache_report.get("with_full_name", 0)
    counters["api_player_cache_with_short_name"] = api_player_cache_report.get("with_short_name", 0)

    players: dict[str, dict[str, Any]] = {}
    matches: list[dict[str, Any]] = []

    if raw_rows:
        for fx in raw_rows:
            d = match_date_from_raw(fx) or date_s
            if d != date_s and not explicit_inputs:
                continue

            fkey, raw_fname = raw_player_from_fixture(fx, "first")
            skey, raw_sname = raw_player_from_fixture(fx, "second")

            # Doubles/team detection uses raw names because cached full names are single-player names.
            if is_non_singles_raw(fx, raw_fname, raw_sname):
                counters["raw_skipped_non_singles"] += 1
                continue

            fname, fname_source = cached_api_player_name(api_player_cache, fkey, raw_fname)
            sname, sname_source = cached_api_player_name(api_player_cache, skey, raw_sname)

            if fname_source == "api_cache_full_name":
                counters["api_player_cache_full_name_used"] += 1
            elif fname_source == "api_cache_short_name":
                counters["api_player_cache_short_name_used"] += 1
            if sname_source == "api_cache_full_name":
                counters["api_player_cache_full_name_used"] += 1
            elif sname_source == "api_cache_short_name":
                counters["api_player_cache_short_name_used"] += 1

            if fname != raw_fname:
                counters["api_player_cache_name_changed"] += 1
                counters["api_player_cache_first_name_changed"] += 1
            if sname != raw_sname:
                counters["api_player_cache_name_changed"] += 1
                counters["api_player_cache_second_name_changed"] += 1

            gender = normalize_gender(event_type_raw(fx))
            tid = fixture_id(fx)
            tournament = tournament_name_raw(fx)
            etype = event_type_raw(fx)

            for key, name, name_source, side in (
                (fkey, fname, fname_source, "first"),
                (skey, sname, sname_source, "second"),
            ):
                if not key:
                    continue

                disp_key = display_api_key(key, gender)
                e, matched_key, source = find_entry(mapping, overrides, key, gender)
                mapped = is_entry_mapped(e)
                opp = sname if side == "first" else fname

                upsert_player_record(
                    players,
                    disp_key=disp_key,
                    raw_key=key,
                    api_name=name,
                    api_name_source=name_source,
                    gender=gender,
                    entry=e,
                    matched_key=matched_key,
                    mapping_source=source,
                    mapped=mapped,
                    tournament=tournament,
                    event_type=etype,
                    opponent=opp,
                )

            fm = is_mapped(mapping, overrides, fkey, gender)
            sm = is_mapped(mapping, overrides, skey, gender)
            coverage = "both_mapped" if fm and sm else "one_mapped" if fm or sm else "none_mapped"

            matches.append(
                {
                    "match_id": tid,
                    "date": date_s,
                    "gender": gender,
                    "event_type_type": etype,
                    "tournament": tournament,
                    "first_api_key": display_api_key(fkey, gender),
                    "first_name": fname,
                    "first_name_source": fname_source,
                    "first_mapped": fm,
                    "second_api_key": display_api_key(skey, gender),
                    "second_name": sname,
                    "second_name_source": sname_source,
                    "second_mapped": sm,
                    "coverage": coverage,
                    "source_path": fx.get("_source_path", ""),
                }
            )
    else:
        for m in source_rows:
            wk, wn = canonical_player_from_match(m, "winner")
            lk, ln = canonical_player_from_match(m, "loser")
            gender = str(m.get("gender") or "")
            tournament = str(m.get("tourney_name") or m.get("tournament_name") or "")
            level = str(m.get("level") or "")
            mid = str(m.get("match_id") or m.get("api_event_key") or m.get("event_key") or "")

            for key, name, opp in ((wk, wn, ln), (lk, ln, wn)):
                if not key:
                    continue

                disp_key = display_api_key(key, gender)
                e, matched_key, source = find_entry(mapping, overrides, key, gender)
                mapped = is_entry_mapped(e)

                upsert_player_record(
                    players,
                    disp_key=disp_key,
                    raw_key=key,
                    api_name=name,
                    api_name_source="api_source",
                    gender=gender,
                    entry=e,
                    matched_key=matched_key,
                    mapping_source=source,
                    mapped=mapped,
                    tournament=tournament,
                    event_type=level,
                    opponent=opp,
                )

            wm = is_mapped(mapping, overrides, wk, gender)
            lm = is_mapped(mapping, overrides, lk, gender)
            coverage = "both_mapped" if wm and lm else "one_mapped" if wm or lm else "none_mapped"

            matches.append(
                {
                    "match_id": mid,
                    "date": date_s,
                    "gender": gender,
                    "event_type_type": level,
                    "tournament": tournament,
                    "first_api_key": display_api_key(wk, gender),
                    "first_name": wn,
                    "first_name_source": "api_source",
                    "first_mapped": wm,
                    "second_api_key": display_api_key(lk, gender),
                    "second_name": ln,
                    "second_name_source": "api_source",
                    "second_mapped": lm,
                    "coverage": coverage,
                    "source_path": m.get("_source_path", ""),
                }
            )

    for m in matches:
        counters["today_matches"] += 1
        counters[f"match_coverage_{m['coverage']}"] += 1
        if m["coverage"] != "both_mapped":
            counters["blocked_matches"] += 1

    counters["today_players"] = len(players)
    counters["mapped_players"] = sum(1 for p in players.values() if p["mapped"])
    counters["unmapped_players"] = sum(1 for p in players.values() if not p["mapped"])
    counters["mapped_from_overrides"] = sum(1 for p in players.values() if p.get("mapped") and p.get("mapping_source") == "override")
    counters["mapped_from_mapping_json"] = sum(1 for p in players.values() if p.get("mapped") and p.get("mapping_source") == "mapping")

    review_rows: list[dict[str, Any]] = []
    counters["review_candidates_available"] = 0
    counters["review_no_candidates_suppressed"] = 0
    counters["review_ambiguous_candidates"] = 0

    for key, p in sorted(players.items(), key=lambda kv: (kv[1]["mapped"], -kv[1]["today_match_count"], kv[1]["api_name"])):
        if p["mapped"]:
            continue

        cand_rows = candidate_rows_for_api(candidates, key, str(p.get("gender") or ""))
        if not cand_rows:
            counters["review_no_candidates_suppressed"] += 1
            continue

        counters["review_candidates_available"] += 1
        ambiguous = is_ambiguous_candidate_set(cand_rows)
        if ambiguous:
            counters["review_ambiguous_candidates"] += 1

        cand1 = cand_rows[0] if len(cand_rows) > 0 else {}
        cand2 = cand_rows[1] if len(cand_rows) > 1 else {}
        cand3 = cand_rows[2] if len(cand_rows) > 2 else {}
        margin = candidate_margin(cand_rows)

        review_rows.append(
            {
                "api_player_key": key,
                "raw_api_player_key": p.get("raw_api_player_key", ""),
                "api_name": p["api_name"],
                "api_name_source": p.get("api_name_source", ""),
                "gender": p["gender"],
                "mapping_status": p["mapping_status"],
                "today_match_count": p["today_match_count"],

                "opponent_name": " | ".join(k for k, _ in p["opponents_today"].most_common(5)),
                "opponents_today": " | ".join(k for k, _ in p["opponents_today"].most_common(5)),
                "tournament": " | ".join(k for k, _ in p["event_names"].most_common(5)),
                "event_names": " | ".join(k for k, _ in p["event_names"].most_common(5)),
                "event_type": " | ".join(k for k, _ in p["event_type_types"].most_common(5)),
                "event_type_types": " | ".join(k for k, _ in p["event_type_types"].most_common(5)),

                "candidate_count": len(cand_rows),
                "candidate_margin": margin,
                "candidate_ambiguity": str(bool(ambiguous)).lower(),

                "candidate_1_key": candidate_key(cand1),
                "candidate_1_name": candidate_name(cand1),
                "candidate_1_score": candidate_score(cand1),
                "candidate_1_method": candidate_method(cand1),
                "candidate_1_matches": candidate_matches(cand1),

                "candidate_2_key": candidate_key(cand2),
                "candidate_2_name": candidate_name(cand2),
                "candidate_2_score": candidate_score(cand2),
                "candidate_2_method": candidate_method(cand2),
                "candidate_2_matches": candidate_matches(cand2),

                "candidate_3_key": candidate_key(cand3),
                "candidate_3_name": candidate_name(cand3),
                "candidate_3_score": candidate_score(cand3),
                "candidate_3_method": candidate_method(cand3),
                "candidate_3_matches": candidate_matches(cand3),

                "best_candidate_key": candidate_key(cand1),
                "best_candidate_name": candidate_name(cand1),
                "score": candidate_score(cand1),
                "margin": margin,
                "method": candidate_method(cand1),
                "suggested_override_json": suggested_override(str(p["gender"]), key, cand1, ambiguous=ambiguous),

                "accept_candidate_rank": "",
                "manual_sackmann_key": "",
                "reject": "",
                "review_note": "",
            }
        )

    fieldnames = [
        "api_player_key",
        "raw_api_player_key",
        "api_name",
        "api_name_source",
        "gender",
        "mapping_status",
        "today_match_count",
        "opponent_name",
        "opponents_today",
        "tournament",
        "event_names",
        "event_type",
        "event_type_types",
        "candidate_count",
        "candidate_margin",
        "candidate_ambiguity",
        "candidate_1_key",
        "candidate_1_name",
        "candidate_1_score",
        "candidate_1_method",
        "candidate_1_matches",
        "candidate_2_key",
        "candidate_2_name",
        "candidate_2_score",
        "candidate_2_method",
        "candidate_2_matches",
        "candidate_3_key",
        "candidate_3_name",
        "candidate_3_score",
        "candidate_3_method",
        "candidate_3_matches",
        "best_candidate_key",
        "best_candidate_name",
        "score",
        "margin",
        "method",
        "suggested_override_json",
        "accept_candidate_rank",
        "manual_sackmann_key",
        "reject",
        "review_note",
    ]
    write_csv(out_review, review_rows, fieldnames)
    write_csv(latest_review, review_rows, fieldnames)

    report = {
        "generated_at": now_utc_iso(),
        "date": date_s,
        "status": "ok" if counters["today_matches"] else "no_matches_found",
        "scanner_ready": counters["blocked_matches"] == 0 and counters["today_matches"] > 0,
        "source_used": source_used,
        "raw_root": str(RAW_ROOT),
        "mapping_path": str(MAPPING_JSON),
        "overrides_path": str(OVERRIDES_JSON),
        "api_player_cache": api_player_cache_report,
        "counters": dict(sorted(counters.items())),
        "today_matches": counters["today_matches"],
        "today_players": counters["today_players"],
        "mapped_players": counters["mapped_players"],
        "unmapped_players": counters["unmapped_players"],
        "blocked_matches": counters["blocked_matches"],
        "outputs": {
            "audit_json": str(out_json),
            "latest_audit_json": str(latest_json),
            "review_csv": str(out_review),
            "latest_review_csv": str(latest_review),
        },
        "notes": [
            "Default audit is singles-only; doubles/team fixtures are skipped for betting scanner mapping.",
            "Today audit checks both player_mapping.json and player_mapping_overrides.json, so manual daily overrides work immediately.",
            "Raw-mode audit now enriches display names from data/raw/api_tennis/players/api_players.json.",
            "Scanner should block/NO_BET every singles match where coverage is not both_mapped.",
        ],
    }
    write_json(out_json, report)
    write_json(latest_json, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

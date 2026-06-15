from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
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
CANDIDATES_CSV = Path("data/reports/api_tennis/player_mapping_candidates.csv")
REPORT_DIR = Path("data/reports/api_tennis")

ACCEPTED_STATUSES = {"auto_mapped", "manual_mapped"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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


def raw_player_from_fixture(fx: dict[str, Any], side: str) -> tuple[str, str]:
    # API-Tennis get_fixtures / get_odds variants.
    if side == "first":
        key = get_any(fx, ["first_player_key", "event_first_player_key", "player_key", "home_player_key"])
        name = get_any(fx, ["event_first_player", "first_player", "first_player_name", "player_name", "event_home_team", "home_player"])
    else:
        key = get_any(fx, ["second_player_key", "event_second_player_key", "away_player_key"])
        name = get_any(fx, ["event_second_player", "second_player", "second_player_name", "event_away_team", "away_player"])
    return str(key or "").strip(), str(name or "").strip()


def canonical_player_from_match(m: dict[str, Any], side: str) -> tuple[str, str]:
    obj = m.get(side) or {}
    if not isinstance(obj, dict):
        return "", ""
    key = get_any(obj, ["player_key", "api_player_key", "key"])
    name = get_any(obj, ["name", "player_name"])
    return str(key or "").strip(), str(name or "").strip()


def mapping_entry(mapping: dict[str, Any], api_key: str) -> dict[str, Any]:
    e = mapping.get(api_key)
    return e if isinstance(e, dict) else {}


def is_mapped(mapping: dict[str, Any], api_key: str) -> bool:
    e = mapping_entry(mapping, api_key)
    return bool(api_key) and e.get("status") in ACCEPTED_STATUSES and bool(e.get("sackmann_player_key"))


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
            if api_key:
                by_api[api_key].append(r)
    return by_api


def best_candidate(cands: dict[str, list[dict[str, str]]], api_key: str) -> dict[str, str]:
    rows = cands.get(api_key) or []
    if not rows:
        return {}
    def score(r: dict[str, str]) -> float:
        try:
            return float(r.get("score") or 0)
        except Exception:
            return 0.0
    return sorted(rows, key=score, reverse=True)[0]


def suggested_override(api_gender: str, api_key: str, cand: dict[str, str]) -> str:
    target = cand.get("sackmann_player_key") or cand.get("candidate_sackmann_key") or cand.get("candidate_key") or ""
    if not target:
        return ""
    # Target already includes gender in our current mapping/rating keys.
    return json.dumps({f"{api_gender}:api_tennis:{api_key}": target}, ensure_ascii=False)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat(), help="UTC date YYYY-MM-DD. Default: today UTC.")
    parser.add_argument("--input", action="append", default=[], help="Optional explicit raw JSON file. Can be repeated.")
    parser.add_argument("--source-fallback", action="store_true", help="If no raw odds/fixtures found, audit imported API source matches for the date.")
    args = parser.parse_args(argv)

    date_s = args.date
    explicit_inputs = [Path(p) for p in args.input]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = REPORT_DIR / f"today_mapping_audit_{date_s}.json"
    out_review = REPORT_DIR / f"today_mapping_review_{date_s}.csv"
    latest_json = REPORT_DIR / "today_mapping_audit.json"
    latest_review = REPORT_DIR / "today_mapping_review.csv"

    if not MAPPING_JSON.exists():
        report = {"generated_at": now_utc_iso(), "date": date_s, "status": "error", "error": f"Missing {MAPPING_JSON}"}
        write_json(out_json, report)
        write_json(latest_json, report)
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        raise SystemExit(1)

    mapping_obj = read_json(MAPPING_JSON)
    mapping = mapping_obj.get("mapping") if isinstance(mapping_obj, dict) else {}
    if not isinstance(mapping, dict):
        mapping = {}

    raw_rows = load_today_raw(date_s, explicit_inputs)
    source_used = "raw"
    source_rows: list[dict[str, Any]] = []
    if not raw_rows and args.source_fallback:
        source_rows = load_today_from_source(date_s)
        source_used = "api_source"

    candidates = load_candidates()
    counters = Counter()
    players: dict[str, dict[str, Any]] = {}
    matches: list[dict[str, Any]] = []

    if raw_rows:
        for fx in raw_rows:
            d = match_date_from_raw(fx) or date_s
            if d != date_s and not explicit_inputs:
                continue
            fkey, fname = raw_player_from_fixture(fx, "first")
            skey, sname = raw_player_from_fixture(fx, "second")
            gender = normalize_gender(event_type_raw(fx))
            tid = fixture_id(fx)
            tournament = tournament_name_raw(fx)
            etype = event_type_raw(fx)
            for key, name, side in ((fkey, fname, "first"), (skey, sname, "second")):
                if not key:
                    continue
                e = mapping_entry(mapping, key)
                mapped = is_mapped(mapping, key)
                p = players.setdefault(key, {
                    "api_player_key": key,
                    "api_name": name,
                    "gender": e.get("gender") or gender,
                    "mapping_status": e.get("status", "missing_mapping_entry"),
                    "sackmann_player_key": e.get("sackmann_player_key", ""),
                    "today_match_count": 0,
                    "opponents_today": Counter(),
                    "event_names": Counter(),
                    "event_type_types": Counter(),
                    "mapped": mapped,
                })
                p["today_match_count"] += 1
                p["event_names"][tournament] += 1
                p["event_type_types"][etype] += 1
                opp = sname if side == "first" else fname
                if opp:
                    p["opponents_today"][opp] += 1
            fm = is_mapped(mapping, fkey)
            sm = is_mapped(mapping, skey)
            coverage = "both_mapped" if fm and sm else "one_mapped" if fm or sm else "none_mapped"
            matches.append({"match_id": tid, "date": date_s, "gender": gender, "event_type_type": etype, "tournament": tournament,
                            "first_api_key": fkey, "first_name": fname, "first_mapped": fm,
                            "second_api_key": skey, "second_name": sname, "second_mapped": sm,
                            "coverage": coverage, "source_path": fx.get("_source_path", "")})
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
                e = mapping_entry(mapping, key)
                mapped = is_mapped(mapping, key)
                p = players.setdefault(key, {"api_player_key": key, "api_name": name, "gender": e.get("gender") or gender,
                    "mapping_status": e.get("status", "missing_mapping_entry"), "sackmann_player_key": e.get("sackmann_player_key", ""),
                    "today_match_count": 0, "opponents_today": Counter(), "event_names": Counter(), "event_type_types": Counter(), "mapped": mapped})
                p["today_match_count"] += 1
                p["event_names"][tournament] += 1
                p["event_type_types"][level] += 1
                if opp:
                    p["opponents_today"][opp] += 1
            wm = is_mapped(mapping, wk)
            lm = is_mapped(mapping, lk)
            coverage = "both_mapped" if wm and lm else "one_mapped" if wm or lm else "none_mapped"
            matches.append({"match_id": mid, "date": date_s, "gender": gender, "event_type_type": level, "tournament": tournament,
                            "first_api_key": wk, "first_name": wn, "first_mapped": wm,
                            "second_api_key": lk, "second_name": ln, "second_mapped": lm,
                            "coverage": coverage, "source_path": m.get("_source_path", "")})

    for m in matches:
        counters["today_matches"] += 1
        counters[f"match_coverage_{m['coverage']}"] += 1
        if m["coverage"] != "both_mapped":
            counters["blocked_matches"] += 1
    counters["today_players"] = len(players)
    counters["mapped_players"] = sum(1 for p in players.values() if p["mapped"])
    counters["unmapped_players"] = sum(1 for p in players.values() if not p["mapped"])

    review_rows: list[dict[str, Any]] = []
    for key, p in sorted(players.items(), key=lambda kv: (kv[1]["mapped"], -kv[1]["today_match_count"], kv[1]["api_name"])):
        if p["mapped"]:
            continue
        cand = best_candidate(candidates, key)
        score = cand.get("score", "")
        margin = cand.get("margin", "") or cand.get("score_margin", "")
        target = cand.get("sackmann_player_key") or cand.get("candidate_sackmann_key") or cand.get("candidate_key") or ""
        target_name = cand.get("sackmann_name") or cand.get("candidate_name") or cand.get("candidate_sackmann_name") or ""
        review_rows.append({
            "api_player_key": key,
            "api_name": p["api_name"],
            "gender": p["gender"],
            "mapping_status": p["mapping_status"],
            "today_match_count": p["today_match_count"],
            "opponents_today": " | ".join(k for k, _ in p["opponents_today"].most_common(5)),
            "event_names": " | ".join(k for k, _ in p["event_names"].most_common(5)),
            "event_type_types": " | ".join(k for k, _ in p["event_type_types"].most_common(5)),
            "best_candidate_key": target,
            "best_candidate_name": target_name,
            "score": score,
            "margin": margin,
            "method": cand.get("method", ""),
            "suggested_override_json": suggested_override(str(p["gender"]), key, cand),
        })

    fieldnames = ["api_player_key", "api_name", "gender", "mapping_status", "today_match_count", "opponents_today", "event_names", "event_type_types", "best_candidate_key", "best_candidate_name", "score", "margin", "method", "suggested_override_json"]
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
        "counters": dict(sorted(counters.items())),
        "today_matches": counters["today_matches"],
        "today_players": counters["today_players"],
        "mapped_players": counters["mapped_players"],
        "unmapped_players": counters["unmapped_players"],
        "blocked_matches": counters["blocked_matches"],
        "outputs": {"audit_json": str(out_json), "latest_audit_json": str(latest_json), "review_csv": str(out_review), "latest_review_csv": str(latest_review)},
        "notes": [
            "Scanner should block/NO_BET every match where coverage is not both_mapped.",
            "Use today_mapping_review.csv to add only confirmed mappings into player_mapping_overrides.json, then rerun 09, 10, and this audit.",
        ],
    }
    write_json(out_json, report)
    write_json(latest_json, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

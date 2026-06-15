from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

DEFAULT_SACKMANN_MANIFEST = Path("data/source/sackmann/manifest.json")
DEFAULT_TODAY_REVIEW = Path("data/reports/api_tennis/today_mapping_review.csv")
DEFAULT_OUTPUT_CSV = Path("data/reports/api_tennis/today_mapping_sackmann_candidate_search.csv")
DEFAULT_OUTPUT_JSON = Path("data/reports/api_tennis/today_mapping_sackmann_candidate_search.json")

MAPPED_STATUSES = {"auto_mapped", "manual_mapped"}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def strip_accents(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def norm_text(value: Any) -> str:
    text = strip_accents(clean(value)).lower()
    text = text.replace("â", "'")
    text = re.sub(r"[^a-z0-9']+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm_text(value).replace("'", "")).strip("_")


def tokens(value: Any) -> list[str]:
    return [t for t in norm_text(value).replace("'", "").split() if t]


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_jsonl_gz(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                row = json.loads(line)
                if isinstance(row, dict):
                    yield row


def manifest_paths(manifest_path: Path) -> list[Path]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing Sackmann manifest: {manifest_path}")
    manifest = read_json(manifest_path)
    files = manifest.get("year_files") or []
    paths: list[Path] = []
    for item in files:
        p = item.get("path") if isinstance(item, dict) else None
        if not p:
            continue
        path = Path(p)
        if not path.is_absolute():
            path = Path.cwd() / path
        paths.append(path)
    return paths


def iter_manifest_matches(manifest_path: Path) -> Iterable[dict[str, Any]]:
    for path in manifest_paths(manifest_path):
        if not path.exists():
            raise FileNotFoundError(f"Missing source file from manifest {manifest_path}: {path}")
        yield from read_jsonl_gz(path)


def player_from_match(match: dict[str, Any], side: str) -> tuple[str, str, str]:
    p = match.get(side) if isinstance(match.get(side), dict) else {}
    return clean(match.get("gender")), clean(p.get("player_key")), clean(p.get("name"))


def build_sackmann_players(manifest_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    players: dict[str, dict[str, Any]] = {}
    match_counts: Counter[str] = Counter()
    for m in iter_manifest_matches(manifest_path):
        if clean(m.get("source")) and clean(m.get("source")) != "sackmann":
            continue
        for side in ("winner", "loser"):
            gender, key, name = player_from_match(m, side)
            if not key or not name or gender not in {"men", "women"}:
                continue
            match_counts[key] += 1
            if key not in players:
                ts = tokens(name)
                players[key] = {
                    "gender": gender,
                    "sackmann_player_key": key,
                    "sackmann_name": name,
                    "name_norm": norm_key(name),
                    "tokens": ts,
                    "last": ts[-1] if ts else "",
                    "first_initial": ts[0][0] if ts and ts[0] else "",
                }
    for key, p in players.items():
        p["sackmann_matches"] = match_counts[key]
    by_gender: dict[str, list[str]] = defaultdict(list)
    for k, p in players.items():
        by_gender[p["gender"]].append(k)
    return players, by_gender


def api_name_parts(name: str) -> dict[str, Any]:
    raw = clean(name)
    nts = tokens(raw)
    initials = []
    # detect initials from visible raw segments: C. O'Connell, V. C. Loureiro J.
    for part in raw.replace("â", "'").split():
        p = strip_accents(part).strip().strip(",")
        if re.fullmatch(r"[A-Za-z]\.", p):
            initials.append(p[0].lower())
    # surname as last non-initial token, ignore one-letter suffix at end if preceding token exists
    non_initial = [t for t in nts if len(t) > 1]
    last = non_initial[-1] if non_initial else (nts[-1] if nts else "")
    if len(nts) >= 2 and len(nts[-1]) == 1 and len(nts[-2]) > 1:
        last = nts[-2]
    first_initial = initials[0] if initials else (nts[0][0] if nts else "")
    return {"raw": raw, "tokens": nts, "initials": initials, "last": last, "first_initial": first_initial, "norm": norm_key(raw)}


def score_candidate(api_name: str, candidate: dict[str, Any]) -> tuple[float, str]:
    api = api_name_parts(api_name)
    cand_tokens = candidate["tokens"]
    cand_norm = candidate["name_norm"]
    c_last = candidate["last"]
    c_first_initial = candidate["first_initial"]

    ratio = SequenceMatcher(None, api["norm"], cand_norm).ratio()
    method = "fuzzy"
    score = ratio * 0.72

    if api["last"] and api["last"] == c_last:
        score += 0.16
        method = "same_last"
    elif api["last"] and api["last"] in cand_tokens:
        score += 0.11
        method = "last_inside_name"

    if api["first_initial"] and api["first_initial"] == c_first_initial:
        score += 0.12
        method = "initial_surname" if method in {"same_last", "last_inside_name"} else "same_initial"

    # exact API abbreviation: C O'Connell -> Christopher O'Connell
    if api["last"] and api["last"] == c_last and api["first_initial"] and api["first_initial"] == c_first_initial:
        score = max(score, 0.975)
        method = "exact_initial_form"

    # multi-initial support, e.g. V. C. Loureiro J.
    if len(api["initials"]) >= 2 and api["last"] and api["last"] == c_last:
        cand_initials = [t[0] for t in cand_tokens[:-1] if t]
        if cand_initials[: len(api["initials"])] == api["initials"]:
            score = max(score, 0.955)
            method = "multi_initial_surname"

    return round(min(score, 0.999), 6), method


def load_review(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing today mapping review CSV: {path}")
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def is_relevant_row(row: dict[str, str]) -> bool:
    name = clean(row.get("api_name"))
    event_type = clean(row.get("event_type_types"))
    if not name or "/" in name:
        return False
    lower = f"{event_type} {clean(row.get('event_names'))}".lower()
    if "doubles" in lower or "teams" in lower or "davis cup" in lower or "billie jean king" in lower or "fed cup" in lower:
        return False
    status = clean(row.get("mapping_status"))
    return status not in MAPPED_STATUSES


def main() -> int:
    ap = argparse.ArgumentParser(description="Search Sackmann source players for candidates for today's unmapped API players.")
    ap.add_argument("--sackmann-manifest", default=str(DEFAULT_SACKMANN_MANIFEST))
    ap.add_argument("--today-review", default=str(DEFAULT_TODAY_REVIEW))
    ap.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    ap.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()

    players, by_gender = build_sackmann_players(Path(args.sackmann_manifest))
    review = [r for r in load_review(Path(args.today_review)) if is_relevant_row(r)]

    out_rows: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    for r in review:
        api_key = clean(r.get("api_player_key"))
        api_name = clean(r.get("api_name"))
        gender = clean(r.get("gender"))
        counters["review_players"] += 1
        scored = []
        for key in by_gender.get(gender, []):
            p = players[key]
            score, method = score_candidate(api_name, p)
            if score >= 0.70:
                scored.append((score, method, p))
        scored.sort(key=lambda x: (x[0], int(x[2].get("sackmann_matches") or 0)), reverse=True)
        top = scored[: max(1, args.top)]
        if not top:
            counters["no_candidate"] += 1
            out_rows.append({
                **r,
                "rank": "",
                "sackmann_player_key": "",
                "sackmann_name": "",
                "sackmann_matches": "",
                "score": "",
                "margin": "",
                "method": "",
                "suggested_action": "no_candidate",
                "suggested_override_json": "",
            })
            continue
        best_score = top[0][0]
        second_score = top[1][0] if len(top) > 1 else 0.0
        margin = round(best_score - second_score, 6)
        if best_score >= 0.97 and margin >= 0.015:
            action = "safe_override"
        elif best_score >= 0.955 and margin >= 0.030:
            action = "review_likely"
        elif best_score >= 0.94:
            action = "manual_review"
        else:
            action = "weak_candidate"
        counters[action] += 1
        for rank, (score, method, p) in enumerate(top, start=1):
            override = ""
            if rank == 1 and action in {"safe_override", "review_likely"}:
                override = json.dumps({api_key: p["sackmann_player_key"]}, ensure_ascii=False)
            out_rows.append({
                **r,
                "rank": rank,
                "sackmann_player_key": p["sackmann_player_key"],
                "sackmann_name": p["sackmann_name"],
                "sackmann_matches": p.get("sackmann_matches", 0),
                "score": score,
                "margin": margin if rank == 1 else "",
                "method": method,
                "suggested_action": action if rank == 1 else "candidate",
                "suggested_override_json": override,
            })

    fieldnames = [
        "api_player_key", "api_raw_player_key", "api_name", "gender", "mapping_status", "today_match_count",
        "opponents_today", "event_names", "event_type_types", "rank", "sackmann_player_key", "sackmann_name",
        "sackmann_matches", "score", "margin", "method", "suggested_action", "suggested_override_json",
    ]
    write_csv(Path(args.output_csv), out_rows, fieldnames)
    report = {
        "status": "ok",
        "generated_at": now_utc_iso(),
        "today_review": str(args.today_review),
        "sackmann_manifest": str(args.sackmann_manifest),
        "players_in_review": len(review),
        "sackmann_players": len(players),
        "counters": dict(counters),
        "outputs": {"csv": str(args.output_csv), "json": str(args.output_json)},
    }
    write_json(Path(args.output_json), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

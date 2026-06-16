from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OVERRIDES_JSON = Path("data/metadata/api_tennis/player_mapping_overrides.json")
API_PLAYERS_JSON = Path("data/raw/api_tennis/players/api_players.json")
SACKMANN_METADATA_JSON = Path("data/metadata/sackmann/players.json")
SACKMANN_RATINGS_JSON = Path("data/ratings/tle_player_ratings.json")
SACKMANN_RATINGS_JSON_GZ = Path("data/ratings/tle_player_ratings.json.gz")
CANDIDATES_CSV = Path("data/reports/api_tennis/player_mapping_candidates.csv")

OUT_CSV = Path("data/reports/api_tennis/player_mapping_overrides_audit.csv")
OUT_JSON = Path("data/reports/api_tennis/player_mapping_overrides_audit.json")


COUNTRY_NAME_TO_CODE = {
    "argentina": "ARG",
    "australia": "AUS",
    "austria": "AUT",
    "belarus": "BLR",
    "belgium": "BEL",
    "bosnia and herzegovina": "BIH",
    "brazil": "BRA",
    "bulgaria": "BUL",
    "canada": "CAN",
    "chile": "CHI",
    "china": "CHN",
    "chinese taipei": "TPE",
    "colombia": "COL",
    "croatia": "CRO",
    "czech republic": "CZE",
    "czechia": "CZE",
    "denmark": "DEN",
    "ecuador": "ECU",
    "egypt": "EGY",
    "estonia": "EST",
    "finland": "FIN",
    "france": "FRA",
    "georgia": "GEO",
    "germany": "GER",
    "great britain": "GBR",
    "greece": "GRE",
    "hong kong": "HKG",
    "hungary": "HUN",
    "india": "IND",
    "ireland": "IRL",
    "israel": "ISR",
    "italy": "ITA",
    "japan": "JPN",
    "kazakhstan": "KAZ",
    "latvia": "LAT",
    "lithuania": "LTU",
    "mexico": "MEX",
    "moldova": "MDA",
    "netherlands": "NED",
    "new zealand": "NZL",
    "norway": "NOR",
    "paraguay": "PAR",
    "peru": "PER",
    "poland": "POL",
    "portugal": "POR",
    "romania": "ROU",
    "russia": "RUS",
    "serbia": "SRB",
    "slovakia": "SVK",
    "slovenia": "SLO",
    "south africa": "RSA",
    "south korea": "KOR",
    "spain": "ESP",
    "sweden": "SWE",
    "switzerland": "SUI",
    "taiwan": "TPE",
    "thailand": "THA",
    "tunisia": "TUN",
    "turkey": "TUR",
    "ukraine": "UKR",
    "united kingdom": "GBR",
    "united states": "USA",
    "united states of america": "USA",
    "uruguay": "URU",
    "uzbekistan": "UZB",
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def norm_text(s: Any) -> str:
    s = str(s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: Any) -> list[str]:
    return [t for t in norm_text(s).split() if t]


def token_set_key(s: Any) -> str:
    return " ".join(sorted(tokens(s)))


def compact(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", norm_text(s))


def parse_api_key(api_key: str) -> tuple[str, str]:
    parts = str(api_key).split(":")
    if len(parts) == 3 and parts[1] == "api":
        return parts[0], parts[2]
    return "", str(api_key)


def parse_api_birth_year(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""

    # API-Tennis often uses DD.MM.YYYY.
    m = re.match(r"^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4})\s*$", s)
    if m:
        return m.group(3)

    # ISO or YYYY...
    m = re.match(r"^\s*(\d{4})", s)
    if m:
        return m.group(1)

    return ""


def normalize_api_country(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    up = s.upper()
    if len(up) == 3 and up.isalpha():
        return up
    return COUNTRY_NAME_TO_CODE.get(norm_text(s), "")


def make_api_info(row: dict[str, Any], *, api_key: str, gender: str, api_id: str) -> dict[str, Any]:
    full_name = (
        row.get("player_full_name")
        or row.get("full_name")
        or row.get("player_complete_name")
        or ""
    )
    short_name = (
        row.get("player_name")
        or row.get("short_name")
        or row.get("name")
        or ""
    )
    display_name = full_name or short_name

    bday = (
        row.get("player_bday")
        or row.get("birth_date")
        or row.get("birthday")
        or row.get("bday")
        or ""
    )
    country = (
        row.get("player_country")
        or row.get("country")
        or row.get("country_code")
        or ""
    )

    return {
        "api_player_key": api_key,
        "api_player_id": api_id,
        "gender": gender,
        "api_name": str(display_name or "").strip(),
        "api_full_name": str(full_name or "").strip(),
        "api_short_name": str(short_name or "").strip(),
        "api_bday": str(bday or "").strip(),
        "api_birth_year": parse_api_birth_year(bday),
        "api_country_raw": str(country or "").strip(),
        "api_country_code": normalize_api_country(country),
    }


def load_api_cache(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Load API cache robustly.

    Returns:
      api_by_exact_key: records keyed by men:api:123 / women:api:123 if known
      api_by_id: records keyed by plain API id, because api_players.json is often keyed by id only.
    """
    data = read_json(path, default={})
    by_key: dict[str, dict[str, Any]] = {}
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)

    if isinstance(data, dict):
        iterable = list(data.items())
    elif isinstance(data, list):
        iterable = []
        for row in data:
            if isinstance(row, dict):
                raw_id = row.get("player_id") or row.get("id") or row.get("api_player_id")
                if raw_id is not None:
                    iterable.append((str(raw_id), row))
    else:
        return by_key, by_id

    for raw_key, row in iterable:
        if not isinstance(row, dict):
            continue

        raw_key = str(raw_key)
        gender = str(row.get("gender") or row.get("player_gender") or "").strip()
        player_id = str(
            row.get("player_id")
            or row.get("id")
            or row.get("api_player_id")
            or raw_key.split(":")[-1]
        ).strip()

        if raw_key.startswith(("men:api:", "women:api:")):
            g, pid = parse_api_key(raw_key)
            key = raw_key
            info = make_api_info(row, api_key=key, gender=g, api_id=pid)
            by_key[key] = info
            by_id[pid].append(info)
            continue

        if player_id.startswith(("men:api:", "women:api:")):
            g, pid = parse_api_key(player_id)
            key = player_id
            info = make_api_info(row, api_key=key, gender=g, api_id=pid)
            by_key[key] = info
            by_id[pid].append(info)
            continue

        # Most common case: cache key/id is plain numeric. Gender is unknown here.
        # We keep it by id and later inject gender from override key.
        info = make_api_info(row, api_key="", gender=gender, api_id=player_id)
        by_id[player_id].append(info)

        if gender in {"men", "women"} and player_id:
            key = f"{gender}:api:{player_id}"
            by_key[key] = make_api_info(row, api_key=key, gender=gender, api_id=player_id)

    return by_key, by_id


def resolve_api_info(api_key: str, by_key: dict[str, dict[str, Any]], by_id: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    if api_key in by_key:
        return by_key[api_key]

    gender, api_id = parse_api_key(api_key)
    rows = by_id.get(api_id, [])
    if not rows:
        return {}

    # Prefer same gender if cache has gender; otherwise use the first id match and inject override gender/key.
    chosen = None
    for row in rows:
        if row.get("gender") == gender:
            chosen = row
            break
    if chosen is None:
        chosen = rows[0]

    out = dict(chosen)
    out["api_player_key"] = api_key
    out["api_player_id"] = api_id
    out["gender"] = gender
    return out


def load_sackmann_players(metadata_path: Path, ratings_json: Path, ratings_json_gz: Path) -> dict[str, dict[str, Any]]:
    meta = read_json(metadata_path, default={}) or {}
    out: dict[str, dict[str, Any]] = {}

    if isinstance(meta, dict):
        for key, row in meta.items():
            if not isinstance(row, dict):
                continue
            out[str(key)] = {
                "sackmann_player_key": str(key),
                "sackmann_player_id": str(row.get("sackmann_player_id") or str(key).split(":")[-1]),
                "gender": str(row.get("gender") or str(key).split(":")[0]),
                "sackmann_name": str(row.get("name") or "").strip(),
                "sackmann_country_code": str(row.get("country_code") or "").strip().upper(),
                "sackmann_birth_date": str(row.get("birth_date") or "").strip(),
                "sackmann_birth_year": str(row.get("birth_date") or "")[:4] if str(row.get("birth_date") or "")[:4].isdigit() else "",
                "sackmann_approx_birth_year": str(row.get("approx_birth_year") or "").strip(),
                "sackmann_hand": str(row.get("hand") or "").strip(),
                "sackmann_height": str(row.get("height") or "").strip(),
            }

    # Fill missing names from ratings file if available.
    ratings_path = ratings_json if ratings_json.exists() else ratings_json_gz
    ratings = read_json(ratings_path, default={}) if ratings_path.exists() else {}
    players = ratings.get("players", ratings) if isinstance(ratings, dict) else {}
    if isinstance(players, dict):
        for key, row in players.items():
            if not isinstance(row, dict):
                continue
            key = str(key)
            if key not in out:
                out[key] = {
                    "sackmann_player_key": key,
                    "sackmann_player_id": key.split(":")[-1],
                    "gender": key.split(":")[0],
                    "sackmann_name": str(row.get("name") or row.get("player_name") or "").strip(),
                    "sackmann_country_code": "",
                    "sackmann_birth_date": "",
                    "sackmann_birth_year": "",
                    "sackmann_approx_birth_year": "",
                    "sackmann_hand": "",
                    "sackmann_height": "",
                }
            elif not out[key].get("sackmann_name"):
                out[key]["sackmann_name"] = str(row.get("name") or row.get("player_name") or "").strip()

    return out


def load_candidates(path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    if not path.exists():
        return out

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            api_key = (
                row.get("api_player_key")
                or row.get("api_key")
                or row.get("source_player_key")
                or row.get("player_key")
                or ""
            )
            sack_key = (
                row.get("sackmann_player_key")
                or row.get("candidate_sackmann_player_key")
                or row.get("target_player_key")
                or row.get("candidate_key")
                or ""
            )
            if api_key and sack_key:
                out[(api_key, sack_key)].append(dict(row))

    return out


def classify_name(api_name: str, sack_name: str) -> tuple[str, str]:
    if not api_name or not sack_name:
        return "missing_name", "No API or Sackmann name."

    n_api = norm_text(api_name)
    n_sack = norm_text(sack_name)
    if n_api == n_sack:
        return "exact", "Exact normalized name match."

    if token_set_key(api_name) and token_set_key(api_name) == token_set_key(sack_name):
        return "token_set_exact", "Same name tokens in different order."

    if compact(api_name) and compact(api_name) == compact(sack_name):
        return "compact_exact", "Names match after removing spaces/punctuation."

    api_t = tokens(api_name)
    sack_t = tokens(sack_name)

    # Initial form: "S Webster" -> "Sophia Webster"
    if len(api_t) == 2 and len(api_t[0]) == 1 and len(sack_t) >= 2:
        if api_t[0] == sack_t[0][:1] and api_t[-1] == sack_t[-1]:
            return "initial_surname", "Initial + surname matches Sackmann full name."

    if api_t and sack_t and api_t[-1] == sack_t[-1]:
        return "same_surname", "Same surname, but not enough name evidence."

    if api_t and sack_t:
        common = set(api_t) & set(sack_t)
        if common:
            return "partial_token_overlap", f"Shared tokens: {', '.join(sorted(common))}."

    return "weak", "Weak or no name evidence."


def compare_years(api_year: str, sack_year: str, sack_approx_year: str) -> tuple[str, str]:
    sy = sack_year or sack_approx_year
    if not api_year or not sy:
        return "unknown", ""

    try:
        diff = abs(int(api_year) - int(sy))
    except Exception:
        return "unknown", ""

    if diff == 0:
        return "match", f"{api_year} == {sy}"
    if diff == 1:
        return "near", f"{api_year} vs {sy}"
    return "mismatch", f"{api_year} vs {sy}"


def verdict_for_row(
    *,
    api: dict[str, Any],
    sack: dict[str, Any],
    candidate_rows: list[dict[str, Any]],
) -> tuple[str, str]:
    api_name = api.get("api_name", "")
    sack_name = sack.get("sackmann_name", "")
    name_status, name_note = classify_name(api_name, sack_name)

    api_country = api.get("api_country_code", "")
    sack_country = sack.get("sackmann_country_code", "")
    country_status = "unknown"
    if api_country and sack_country:
        country_status = "match" if api_country == sack_country else "mismatch"

    year_status, year_note = compare_years(
        api.get("api_birth_year", ""),
        sack.get("sackmann_birth_year", ""),
        sack.get("sackmann_approx_birth_year", ""),
    )

    candidate_score = ""
    candidate_method = ""
    if candidate_rows:
        row = candidate_rows[0]
        for k in ("score", "candidate_score", "best_score"):
            if row.get(k):
                candidate_score = str(row.get(k))
                break
        for k in ("method", "candidate_method", "match_method", "best_method"):
            if row.get(k):
                candidate_method = str(row.get(k))
                break

    reasons = []

    if name_status in {"exact", "token_set_exact", "compact_exact"}:
        if country_status in {"match", "unknown"} and year_status in {"match", "near", "unknown"}:
            verdict = "SAFE"
        else:
            verdict = "CHECK"
        reasons.append(name_note)
    elif name_status == "initial_surname":
        if country_status == "match" or year_status in {"match", "near"}:
            verdict = "LIKELY_OK"
        else:
            verdict = "CHECK"
        reasons.append(name_note)
    elif name_status in {"same_surname", "partial_token_overlap"}:
        if country_status == "match" and year_status in {"match", "near"}:
            verdict = "LIKELY_OK"
        elif country_status == "match" or year_status in {"match", "near"}:
            verdict = "CHECK"
        else:
            verdict = "RISKY"
        reasons.append(name_note)
    else:
        verdict = "RISKY"
        reasons.append(name_note)

    if country_status == "match":
        reasons.append(f"Country match: {api_country}.")
    elif country_status == "mismatch":
        verdict = "RISKY" if name_status not in {"exact", "token_set_exact", "compact_exact"} else "CHECK"
        reasons.append(f"Country mismatch: API {api_country}, Sackmann {sack_country}.")

    if year_status == "match":
        reasons.append(f"Birth year match: {year_note}.")
    elif year_status == "near":
        reasons.append(f"Birth year near: {year_note}.")
    elif year_status == "mismatch":
        verdict = "RISKY" if name_status not in {"exact", "token_set_exact", "compact_exact"} else "CHECK"
        reasons.append(f"Birth year mismatch: {year_note}.")

    if candidate_method:
        reasons.append(f"Candidate method: {candidate_method}.")
    if candidate_score:
        reasons.append(f"Candidate score: {candidate_score}.")

    return verdict, " ".join(reasons)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--overrides", type=Path, default=OVERRIDES_JSON)
    parser.add_argument("--api-players", type=Path, default=API_PLAYERS_JSON)
    parser.add_argument("--sackmann-metadata", type=Path, default=SACKMANN_METADATA_JSON)
    parser.add_argument("--candidates", type=Path, default=CANDIDATES_CSV)
    parser.add_argument("--out-csv", type=Path, default=OUT_CSV)
    parser.add_argument("--out-json", type=Path, default=OUT_JSON)
    args = parser.parse_args(argv)

    overrides = read_json(args.overrides, default={}) or {}
    api_by_key, api_by_id = load_api_cache(args.api_players)
    sackmann_players = load_sackmann_players(args.sackmann_metadata, SACKMANN_RATINGS_JSON, SACKMANN_RATINGS_JSON_GZ)
    candidates = load_candidates(args.candidates)

    rows: list[dict[str, Any]] = []
    counts = defaultdict(int)

    for api_key, sack_key in sorted(overrides.items()):
        api_key = str(api_key)
        sack_key = str(sack_key)

        api = resolve_api_info(api_key, api_by_key, api_by_id)
        sack = sackmann_players.get(sack_key, {})
        candidate_rows = candidates.get((api_key, sack_key), [])

        if not api:
            verdict = "CHECK"
            reason = "API player not found in api_players cache by exact key or numeric id."
        elif not sack:
            verdict = "RISKY"
            reason = "Sackmann player key not found in metadata/ratings."
        else:
            verdict, reason = verdict_for_row(api=api, sack=sack, candidate_rows=candidate_rows)

        counts[verdict] += 1

        row = {
            "verdict": verdict,
            "api_player_key": api_key,
            "api_name": api.get("api_name", ""),
            "api_full_name": api.get("api_full_name", ""),
            "api_short_name": api.get("api_short_name", ""),
            "api_country_raw": api.get("api_country_raw", ""),
            "api_country_code": api.get("api_country_code", ""),
            "api_bday": api.get("api_bday", ""),
            "api_birth_year": api.get("api_birth_year", ""),
            "sackmann_player_key": sack_key,
            "sackmann_name": sack.get("sackmann_name", ""),
            "sackmann_country_code": sack.get("sackmann_country_code", ""),
            "sackmann_birth_date": sack.get("sackmann_birth_date", ""),
            "sackmann_birth_year": sack.get("sackmann_birth_year", ""),
            "sackmann_approx_birth_year": sack.get("sackmann_approx_birth_year", ""),
            "sackmann_hand": sack.get("sackmann_hand", ""),
            "sackmann_height": sack.get("sackmann_height", ""),
            "name_match_type": classify_name(api.get("api_name", ""), sack.get("sackmann_name", ""))[0] if api and sack else "",
            "candidate_rows_found": len(candidate_rows),
            "reason": reason,
        }
        rows.append(row)

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.out_csv.open("w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "verdict",
            "api_player_key",
            "api_name",
            "api_full_name",
            "api_short_name",
            "api_country_raw",
            "api_country_code",
            "api_bday",
            "api_birth_year",
            "sackmann_player_key",
            "sackmann_name",
            "sackmann_country_code",
            "sackmann_birth_date",
            "sackmann_birth_year",
            "sackmann_approx_birth_year",
            "sackmann_hand",
            "sackmann_height",
            "name_match_type",
            "candidate_rows_found",
            "reason",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "inputs": {
            "overrides": str(args.overrides),
            "api_players": str(args.api_players),
            "sackmann_metadata": str(args.sackmann_metadata),
            "candidates": str(args.candidates),
        },
        "outputs": {
            "audit_csv": str(args.out_csv),
            "audit_json": str(args.out_json),
        },
        "counts": {
            "overrides_total": len(overrides),
            "api_cache_exact_keys_loaded": len(api_by_key),
            "api_cache_numeric_ids_loaded": len(api_by_id),
            "sackmann_players_loaded": len(sackmann_players),
            **dict(sorted(counts.items())),
        },
        "notes": [
            "SAFE means strong name evidence and no country/year contradiction.",
            "LIKELY_OK means acceptable manual override but not strong enough for broad auto-rule.",
            "CHECK means keep only if manually verified.",
            "RISKY means remove or verify deeply before use.",
            "The audit resolves API records by numeric id when api_players.json is keyed without gender.",
        ],
        "rows": rows,
    }

    write_json(args.out_json, report)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

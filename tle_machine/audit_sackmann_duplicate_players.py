from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SACKMANN_PLAYERS_JSON = Path("data/metadata/sackmann/players.json")
OUTPUT_CSV = Path("data/reports/sackmann/duplicate_players_audit.csv")
OUTPUT_JSON = Path("data/reports/sackmann/duplicate_players_audit.json")
ALIAS_TEMPLATE_JSON = Path("data/reports/sackmann/player_aliases_template.json")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def strip_accents(s: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", s or "")
        if not unicodedata.combining(ch)
    )


def norm_name(s: str) -> str:
    s = strip_accents(str(s or "")).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def player_key_gender(key: str) -> str:
    if key.startswith("men:"):
        return "men"
    if key.startswith("women:"):
        return "women"
    return ""


def clean(v: Any) -> str:
    return str(v or "").strip()


def intish(v: Any) -> int:
    try:
        if v is None or v == "":
            return 0
        return int(float(v))
    except Exception:
        return 0


def get_player_key(key: str, row: dict[str, Any]) -> str:
    return (
        clean(row.get("player_key"))
        or clean(row.get("sackmann_player_key"))
        or clean(row.get("key"))
        or clean(key)
    )


def get_name(row: dict[str, Any]) -> str:
    return (
        clean(row.get("name"))
        or clean(row.get("player_name"))
        or clean(row.get("full_name"))
        or clean(row.get("canonical_name"))
    )


def get_match_count(row: dict[str, Any]) -> int:
    for k in ["matches", "match_count", "matches_total", "total_matches", "rows", "appearances"]:
        n = intish(row.get(k))
        if n:
            return n
    return 0


def get_country(row: dict[str, Any]) -> str:
    return (
        clean(row.get("country_code"))
        or clean(row.get("country"))
        or clean(row.get("ioc"))
        or clean(row.get("nationality"))
    ).upper()


def get_birth_year(row: dict[str, Any]) -> str:
    y = (
        clean(row.get("birth_year"))
        or clean(row.get("approx_birth_year"))
        or clean(row.get("dob_year"))
    )
    if y:
        return str(intish(y)) if intish(y) else y

    bdate = clean(row.get("birth_date")) or clean(row.get("dob"))
    m = re.match(r"^(\d{4})", bdate)
    return m.group(1) if m else ""


def risk_level(items: list[dict[str, Any]]) -> str:
    countries = {clean(x["country"]) for x in items if clean(x["country"])}
    years = {clean(x["birth_year"]) for x in items if clean(x["birth_year"])}
    names = {clean(x["name_norm"]) for x in items if clean(x["name_norm"])}
    match_counts = [intish(x["match_count"]) for x in items]
    max_matches = max(match_counts) if match_counts else 0
    min_matches = min(match_counts) if match_counts else 0

    if len(names) != 1:
        return "HIGH"

    if len(countries) <= 1 and len(years) <= 1:
        if min_matches <= 2 and max_matches >= 10:
            return "LOW"
        return "MEDIUM"

    if len(countries) > 1 and len(years) > 1:
        return "HIGH"

    return "CHECK"


def recommended_key(items: list[dict[str, Any]]) -> str:
    # Keep the key with most historical evidence. If tied, prefer the latest/highest numeric id,
    # because duplicates often appear as older small fragments + newer active id.
    def numeric_tail(key: str) -> int:
        m = re.search(r":(\d+)$", key)
        return int(m.group(1)) if m else 0

    ranked = sorted(
        items,
        key=lambda x: (intish(x["match_count"]), numeric_tail(clean(x["player_key"]))),
        reverse=True,
    )
    return clean(ranked[0]["player_key"]) if ranked else ""


def load_players(path: Path) -> list[dict[str, Any]]:
    data = read_json(path, {})
    rows: list[dict[str, Any]] = []

    if isinstance(data, dict):
        # Common format: {"men:sackmann:...": {...}, ...}
        for k, v in data.items():
            if isinstance(v, dict):
                player_key = get_player_key(str(k), v)
                name = get_name(v)
                if not player_key or not name:
                    continue
                rows.append({
                    "player_key": player_key,
                    "gender": clean(v.get("gender")) or player_key_gender(player_key),
                    "name": name,
                    "name_norm": norm_name(name),
                    "country": get_country(v),
                    "birth_year": get_birth_year(v),
                    "hand": clean(v.get("hand")),
                    "height": clean(v.get("height")),
                    "match_count": get_match_count(v),
                })
        return rows

    if isinstance(data, list):
        for v in data:
            if not isinstance(v, dict):
                continue
            player_key = get_player_key("", v)
            name = get_name(v)
            if not player_key or not name:
                continue
            rows.append({
                "player_key": player_key,
                "gender": clean(v.get("gender")) or player_key_gender(player_key),
                "name": name,
                "name_norm": norm_name(name),
                "country": get_country(v),
                "birth_year": get_birth_year(v),
                "hand": clean(v.get("hand")),
                "height": clean(v.get("height")),
                "match_count": get_match_count(v),
            })
    return rows


def main() -> None:
    players = load_players(SACKMANN_PLAYERS_JSON)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for p in players:
        if not p["name_norm"]:
            continue
        groups[(p["gender"], p["name_norm"])].append(p)

    out_rows: list[dict[str, Any]] = []
    alias_template: dict[str, str] = {}

    group_count = 0
    player_duplicate_keys_count = 0

    for (gender, name_norm), items in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        # Unique player keys only
        uniq: dict[str, dict[str, Any]] = {}
        for item in items:
            uniq[item["player_key"]] = item
        items = list(uniq.values())

        if len(items) < 2:
            continue

        group_count += 1
        player_duplicate_keys_count += len(items)

        rec = recommended_key(items)
        risk = risk_level(items)
        countries = sorted({x["country"] for x in items if x["country"]})
        years = sorted({x["birth_year"] for x in items if x["birth_year"]})

        # Template only for LOW/MEDIUM/CHECK, but commented-by-report only not possible in JSON,
        # so it stays in reports directory. User should manually copy confirmed entries to
        # data/metadata/sackmann/player_aliases.json.
        if risk in {"LOW", "MEDIUM", "CHECK"} and rec:
            for item in items:
                key = item["player_key"]
                if key != rec:
                    alias_template[key] = rec

        for item in sorted(items, key=lambda x: intish(x["match_count"]), reverse=True):
            out_rows.append({
                "risk_level": risk,
                "gender": gender,
                "name_norm": name_norm,
                "display_name": item["name"],
                "player_key": item["player_key"],
                "match_count": item["match_count"],
                "country": item["country"],
                "birth_year": item["birth_year"],
                "hand": item["hand"],
                "height": item["height"],
                "recommended_canonical_key": rec,
                "all_player_keys": " | ".join(sorted(x["player_key"] for x in items)),
                "all_match_counts": " | ".join(f"{x['player_key']}={x['match_count']}" for x in sorted(items, key=lambda y: y["player_key"])),
                "all_countries": " | ".join(countries),
                "all_birth_years": " | ".join(years),
            })

    fieldnames = [
        "risk_level",
        "gender",
        "name_norm",
        "display_name",
        "player_key",
        "match_count",
        "country",
        "birth_year",
        "hand",
        "height",
        "recommended_canonical_key",
        "all_player_keys",
        "all_match_counts",
        "all_countries",
        "all_birth_years",
    ]

    write_csv(OUTPUT_CSV, out_rows, fieldnames)
    write_json(ALIAS_TEMPLATE_JSON, alias_template)

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "inputs": {
            "sackmann_players_json": str(SACKMANN_PLAYERS_JSON),
        },
        "outputs": {
            "duplicate_players_audit_csv": str(OUTPUT_CSV),
            "duplicate_players_audit_json": str(OUTPUT_JSON),
            "player_aliases_template_json": str(ALIAS_TEMPLATE_JSON),
        },
        "counts": {
            "players_loaded": len(players),
            "duplicate_name_groups": group_count,
            "duplicate_player_keys_in_groups": player_duplicate_keys_count,
            "audit_rows": len(out_rows),
            "alias_template_entries": len(alias_template),
            "risk_LOW_rows": sum(1 for r in out_rows if r["risk_level"] == "LOW"),
            "risk_MEDIUM_rows": sum(1 for r in out_rows if r["risk_level"] == "MEDIUM"),
            "risk_CHECK_rows": sum(1 for r in out_rows if r["risk_level"] == "CHECK"),
            "risk_HIGH_rows": sum(1 for r in out_rows if r["risk_level"] == "HIGH"),
        },
        "important": [
            "Do not blindly apply player_aliases_template.json.",
            "Review duplicate_players_audit.csv manually and copy only confirmed duplicate mappings into data/metadata/sackmann/player_aliases.json.",
            "Recommended canonical key is the key with most match history, tie-broken by higher numeric id.",
        ],
    }
    write_json(OUTPUT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

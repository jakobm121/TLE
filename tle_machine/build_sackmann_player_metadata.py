from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RAW_DIR = Path("data/raw/sackmann")
OUT_JSON = Path("data/metadata/sackmann/players.json")
REPORT_JSON = Path("data/reports/sackmann/player_metadata_report.json")

RATINGS_JSON = Path("data/ratings/tle_player_ratings.json")
RATINGS_JSON_GZ = Path("data/ratings/tle_player_ratings.json.gz")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def read_json_any(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text(encoding="utf-8"))


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_birth_date(value: Any) -> str:
    s = clean_text(value)
    if not s:
        return ""

    # Sackmann player files normally use YYYYMMDD.
    if len(s) == 8 and s.isdigit():
        y, m, d = s[:4], s[4:6], s[6:8]
        if y == "0000" or m == "00" or d == "00":
            return ""
        return f"{y}-{m}-{d}"

    # ISO fallback.
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]

    return ""


def normalize_date_yyyymmdd(value: Any) -> str:
    s = clean_text(value)
    if len(s) == 8 and s.isdigit():
        y, m, d = s[:4], s[4:6], s[6:8]
        if y != "0000" and m != "00" and d != "00":
            return f"{y}-{m}-{d}"
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return ""


def approximate_birth_year(match_date: str, age: Any) -> str:
    """Return approximate birth year from Sackmann match age.

    This is intentionally not stored as exact birth_date.
    """
    if not match_date:
        return ""
    try:
        year = int(match_date[:4])
        age_f = float(str(age or "").strip())
        if age_f <= 0:
            return ""
        return str(int(round(year - age_f)))
    except Exception:
        return ""


def gender_from_path(path: Path) -> str:
    parts = [p.lower() for p in path.parts]
    name = path.name.lower()
    if "wta" in parts or name.startswith("wta_"):
        return "women"
    if "atp" in parts or name.startswith("atp_"):
        return "men"
    return ""


def iter_raw_sackmann_csvs(raw_dir: Path) -> list[Path]:
    out: list[Path] = []
    if not raw_dir.exists():
        return out

    for p in sorted(raw_dir.rglob("*.csv")):
        name = p.name.lower()

        # Include match CSVs and optional player master files if user already has them.
        if (
            name.startswith("atp_matches_")
            or name.startswith("wta_matches_")
            or name in {"atp_players.csv", "wta_players.csv"}
        ):
            out.append(p)

    return out


def upsert_counter(c: Counter, value: str) -> None:
    if value:
        c[value] += 1


def ensure_player(meta: dict[str, Any], key: str, gender: str, player_id: str, name: str) -> dict[str, Any]:
    if key not in meta:
        meta[key] = {
            "player_key": key,
            "gender": gender,
            "sackmann_player_id": player_id,
            "name": name,
            "first_name": "",
            "last_name": "",
            "hand": "",
            "birth_date": "",
            "approx_birth_year": "",
            "country_code": "",
            "height": "",
            "metadata_sources": [],
            "_country_counter": Counter(),
            "_hand_counter": Counter(),
            "_height_counter": Counter(),
            "_approx_birth_year_counter": Counter(),
        }

    row = meta[key]
    if name and not row.get("name"):
        row["name"] = name
    return row


def read_player_master_csv(path: Path, gender: str, meta: dict[str, Any], counters: Counter) -> None:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            player_id = clean_text(row.get("player_id"))
            if not player_id:
                continue

            first_name = clean_text(row.get("first_name"))
            last_name = clean_text(row.get("last_name"))
            name = " ".join(x for x in (first_name, last_name) if x).strip()
            key = f"{gender}:sackmann:{player_id}"

            p = ensure_player(meta, key, gender, player_id, name)
            p["first_name"] = first_name
            p["last_name"] = last_name
            p["name"] = name or p.get("name", "")
            p["hand"] = clean_text(row.get("hand")) or p.get("hand", "")
            p["birth_date"] = normalize_birth_date(row.get("birth_date")) or p.get("birth_date", "")
            p["country_code"] = clean_text(row.get("country_code")).upper() or p.get("country_code", "")
            p["height"] = clean_text(row.get("height")) or p.get("height", "")
            p["metadata_sources"].append(str(path))

            counters["rows_player_master"] += 1


def read_match_csv(path: Path, gender: str, meta: dict[str, Any], counters: Counter) -> None:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            match_date = normalize_date_yyyymmdd(row.get("tourney_date"))

            for side in ("winner", "loser"):
                player_id = clean_text(row.get(f"{side}_id"))
                name = clean_text(row.get(f"{side}_name"))
                if not player_id or not name:
                    continue

                key = f"{gender}:sackmann:{player_id}"
                p = ensure_player(meta, key, gender, player_id, name)

                country = clean_text(row.get(f"{side}_ioc")).upper()
                hand = clean_text(row.get(f"{side}_hand"))
                height = clean_text(row.get(f"{side}_ht"))
                approx_year = approximate_birth_year(match_date, row.get(f"{side}_age"))

                upsert_counter(p["_country_counter"], country)
                upsert_counter(p["_hand_counter"], hand)
                upsert_counter(p["_height_counter"], height)
                upsert_counter(p["_approx_birth_year_counter"], approx_year)

                # Keep a small list of evidence files, not every row.
                if len(p["metadata_sources"]) < 5:
                    p["metadata_sources"].append(str(path))

                counters["rows_match_player_sides"] += 1


def finalize_metadata(meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    for key, p in meta.items():
        country_counter = p.pop("_country_counter", Counter())
        hand_counter = p.pop("_hand_counter", Counter())
        height_counter = p.pop("_height_counter", Counter())
        approx_year_counter = p.pop("_approx_birth_year_counter", Counter())

        if not p.get("country_code") and country_counter:
            p["country_code"] = country_counter.most_common(1)[0][0]
        if not p.get("hand") and hand_counter:
            p["hand"] = hand_counter.most_common(1)[0][0]
        if not p.get("height") and height_counter:
            p["height"] = height_counter.most_common(1)[0][0]
        if not p.get("approx_birth_year") and approx_year_counter:
            p["approx_birth_year"] = approx_year_counter.most_common(1)[0][0]

        p["country_code_variants"] = dict(country_counter.most_common(5))
        p["hand_variants"] = dict(hand_counter.most_common(5))
        p["height_variants"] = dict(height_counter.most_common(5))
        p["approx_birth_year_variants"] = dict(approx_year_counter.most_common(5))
        p["metadata_sources"] = sorted(set(p.get("metadata_sources") or []))[:10]

        out[key] = p

    return out


def load_rating_player_keys() -> set[str]:
    path = RATINGS_JSON if RATINGS_JSON.exists() else RATINGS_JSON_GZ
    if not path.exists():
        return set()

    data = read_json_any(path)
    players = data.get("players", data) if isinstance(data, dict) else {}
    if not isinstance(players, dict):
        return set()
    return set(str(k) for k in players.keys())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--out", type=Path, default=OUT_JSON)
    args = parser.parse_args(argv)

    counters = Counter()
    meta_work: dict[str, Any] = {}

    csv_paths = iter_raw_sackmann_csvs(args.raw_dir)
    counters["input_csv_files"] = len(csv_paths)

    for path in csv_paths:
        gender = gender_from_path(path)
        if gender not in {"men", "women"}:
            counters["skipped_unknown_gender_file"] += 1
            continue

        name = path.name.lower()
        if name in {"atp_players.csv", "wta_players.csv"}:
            read_player_master_csv(path, gender, meta_work, counters)
            counters["player_master_files_read"] += 1
        else:
            read_match_csv(path, gender, meta_work, counters)
            counters["match_files_read"] += 1

    metadata = finalize_metadata(meta_work)
    rating_keys = load_rating_player_keys()
    metadata_keys = set(metadata.keys())

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok" if metadata else "no_metadata_built",
        "inputs": {
            "raw_dir": str(args.raw_dir),
            "csv_files": [str(p) for p in csv_paths],
        },
        "outputs": {
            "metadata_json": str(args.out),
            "report_json": str(REPORT_JSON),
        },
        "counts": {
            **dict(counters),
            "metadata_players_total": len(metadata),
            "metadata_men": sum(1 for p in metadata.values() if p.get("gender") == "men"),
            "metadata_women": sum(1 for p in metadata.values() if p.get("gender") == "women"),
            "with_birth_date_exact": sum(1 for p in metadata.values() if p.get("birth_date")),
            "with_approx_birth_year": sum(1 for p in metadata.values() if p.get("approx_birth_year")),
            "with_country_code": sum(1 for p in metadata.values() if p.get("country_code")),
            "with_hand": sum(1 for p in metadata.values() if p.get("hand")),
            "with_height": sum(1 for p in metadata.values() if p.get("height")),
            "rating_players_total": len(rating_keys),
            "rating_players_with_metadata": len(rating_keys & metadata_keys),
            "rating_players_missing_metadata": len(rating_keys - metadata_keys),
            "metadata_players_not_in_ratings": len(metadata_keys - rating_keys),
        },
        "notes": [
            "This version does not download external Sackmann player files.",
            "It builds metadata from already-fetched Sackmann match CSVs in data/raw/sackmann.",
            "Exact birth_date is only available if atp_players.csv/wta_players.csv already exist locally.",
            "approx_birth_year is derived from match date and match age, and should not be treated as exact DOB.",
            "country_code is derived from winner_ioc/loser_ioc in match CSVs.",
        ],
    }

    write_json(args.out, metadata)
    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

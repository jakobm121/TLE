from __future__ import annotations

import argparse
import csv
import gzip
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RAW_DIR = Path("data/raw/sackmann")
OUT_JSON = Path("data/metadata/sackmann/players.json")
REPORT_JSON = Path("data/reports/sackmann/player_metadata_report.json")

ATP_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_players.csv"
WTA_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_players.csv"

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


def download_if_needed(url: str, path: Path, refresh: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not refresh:
        return False
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    path.write_bytes(data)
    return True


def normalize_birth_date(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""

    # Sackmann normally uses YYYYMMDD.
    if len(s) == 8 and s.isdigit():
        y, m, d = s[:4], s[4:6], s[6:8]
        if y == "0000" or m == "00" or d == "00":
            return ""
        return f"{y}-{m}-{d}"

    # Already ISO-ish.
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]

    return ""


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def read_players_csv(path: Path, gender: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

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

            out[key] = {
                "player_key": key,
                "gender": gender,
                "sackmann_player_id": player_id,
                "name": name,
                "first_name": first_name,
                "last_name": last_name,
                "hand": clean_text(row.get("hand")),
                "birth_date": normalize_birth_date(row.get("birth_date")),
                "country_code": clean_text(row.get("country_code")).upper(),
                "height": clean_text(row.get("height")),
                "source_file": str(path),
            }

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
    parser.add_argument("--refresh", action="store_true", help="Redownload Sackmann player CSV files.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--out", type=Path, default=OUT_JSON)
    args = parser.parse_args(argv)

    atp_path = args.raw_dir / "atp_players.csv"
    wta_path = args.raw_dir / "wta_players.csv"

    downloaded_atp = download_if_needed(ATP_URL, atp_path, refresh=args.refresh)
    downloaded_wta = download_if_needed(WTA_URL, wta_path, refresh=args.refresh)

    metadata: dict[str, dict[str, Any]] = {}
    men = read_players_csv(atp_path, "men")
    women = read_players_csv(wta_path, "women")
    metadata.update(men)
    metadata.update(women)

    rating_keys = load_rating_player_keys()
    metadata_keys = set(metadata.keys())

    report = {
        "generated_at": now_utc_iso(),
        "status": "ok",
        "inputs": {
            "atp_url": ATP_URL,
            "wta_url": WTA_URL,
            "atp_csv": str(atp_path),
            "wta_csv": str(wta_path),
            "downloaded_atp": downloaded_atp,
            "downloaded_wta": downloaded_wta,
        },
        "outputs": {
            "metadata_json": str(args.out),
            "report_json": str(REPORT_JSON),
        },
        "counts": {
            "metadata_players_total": len(metadata),
            "metadata_men": len(men),
            "metadata_women": len(women),
            "with_birth_date": sum(1 for p in metadata.values() if p.get("birth_date")),
            "with_country_code": sum(1 for p in metadata.values() if p.get("country_code")),
            "rating_players_total": len(rating_keys),
            "rating_players_with_metadata": len(rating_keys & metadata_keys),
            "rating_players_missing_metadata": len(rating_keys - metadata_keys),
            "metadata_players_not_in_ratings": len(metadata_keys - rating_keys),
        },
        "notes": [
            "This file does not replace ratings. It enriches existing Sackmann keys using the original Sackmann player_id.",
            "Key format is gender:sackmann:player_id, for example men:sackmann:210150.",
        ],
    }

    write_json(args.out, metadata)
    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import gzip
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RAW_DIR = Path("data/raw/sackmann")
OUT_JSON = Path("data/metadata/sackmann/players.json")
REPORT_JSON = Path("data/reports/sackmann/player_metadata_report.json")

# Correct Sackmann player filenames are:
# ATP: atp_players.csv
# WTA: wta_players.csv
# Keep fallback URLs because GitHub raw occasionally behaves differently.
ATP_URLS = [
    "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/refs/heads/master/atp_players.csv",
    "https://raw.githubusercontent.com/jeffsackmann/tennis_atp/refs/heads/master/atp_players.csv",
    "https://github.com/JeffSackmann/tennis_atp/raw/refs/heads/master/atp_players.csv",
]
WTA_URLS = [
    "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/refs/heads/master/wta_players.csv",
    "https://raw.githubusercontent.com/jeffsackmann/tennis_wta/refs/heads/master/wta_players.csv",
    "https://github.com/JeffSackmann/tennis_wta/raw/refs/heads/master/wta_players.csv",
]

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


def download_from_urls(urls: list[str], path: Path, refresh: bool = False) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)

    report = {
        "path": str(path),
        "downloaded": False,
        "used_url": "",
        "attempts": [],
    }

    if path.exists() and not refresh:
        report["status"] = "cached"
        return report

    last_error = ""
    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "TLE-Sackmann-Metadata/1.0",
                    "Accept": "text/csv,text/plain,*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()

            # Basic guard: player CSV must have player_id header.
            head = data[:300].decode("utf-8", errors="replace").lower()
            if "player_id" not in head:
                raise RuntimeError(f"Downloaded content does not look like player CSV. First bytes: {head[:120]!r}")

            path.write_bytes(data)
            report["downloaded"] = True
            report["used_url"] = url
            report["status"] = "downloaded"
            report["attempts"].append({"url": url, "status": "ok", "bytes": len(data)})
            return report

        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            status_code = getattr(e, "code", "")
            report["attempts"].append({"url": url, "status": "error", "code": status_code, "error": last_error})

    report["status"] = "error"
    report["error"] = last_error
    raise RuntimeError(f"Could not download {path.name}. Attempts: {json.dumps(report['attempts'], ensure_ascii=False)}")


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

        required = {"player_id", "first_name", "last_name"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"{path} is missing required columns: {sorted(missing)}")

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

    atp_download_report = download_from_urls(ATP_URLS, atp_path, refresh=args.refresh)
    wta_download_report = download_from_urls(WTA_URLS, wta_path, refresh=args.refresh)

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
            "atp_download": atp_download_report,
            "wta_download": wta_download_report,
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
            "Correct Sackmann filenames are atp_players.csv and wta_players.csv.",
        ],
    }

    write_json(args.out, metadata)
    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

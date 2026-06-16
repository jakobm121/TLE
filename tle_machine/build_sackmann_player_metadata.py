from __future__ import annotations

import argparse
import csv
import gzip
import json
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RAW_DIR = Path("data/raw/sackmann")
OUT_JSON = Path("data/metadata/sackmann/players.json")
REPORT_JSON = Path("data/reports/sackmann/player_metadata_report.json")

ATP_REPO = "https://github.com/JeffSackmann/tennis_atp.git"
WTA_REPO = "https://github.com/JeffSackmann/tennis_wta.git"
ATP_FILE = "atp_players.csv"
WTA_FILE = "wta_players.csv"

# Keep raw URLs as first attempt, but fall back to sparse git clone.
ATP_URLS = [
    "https://github.com/JeffSackmann/tennis_atp/raw/refs/heads/master/atp_players.csv",
    "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/refs/heads/master/atp_players.csv",
    "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_players.csv",
]
WTA_URLS = [
    "https://github.com/JeffSackmann/tennis_wta/raw/refs/heads/master/wta_players.csv",
    "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/refs/heads/master/wta_players.csv",
    "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_players.csv",
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


def looks_like_player_csv(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 20:
        return False
    head = path.read_bytes()[:500].decode("utf-8", errors="replace").lower()
    return "player_id" in head and "first_name" in head and "last_name" in head


def download_from_urls(urls: list[str], path: Path, refresh: bool = False) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)

    report = {
        "method": "url",
        "path": str(path),
        "downloaded": False,
        "used_url": "",
        "attempts": [],
    }

    if path.exists() and not refresh and looks_like_player_csv(path):
        report["status"] = "cached"
        return report

    last_error = ""
    for url in urls:
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "TLE-Sackmann-Metadata/1.0",
                    "Accept": "text/csv,text/plain,*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                data = r.read()

            tmp.write_bytes(data)
            if not looks_like_player_csv(tmp):
                head = data[:200].decode("utf-8", errors="replace")
                raise RuntimeError(f"Downloaded content is not player CSV. First bytes: {head!r}")

            tmp.replace(path)
            report["downloaded"] = True
            report["used_url"] = url
            report["status"] = "downloaded"
            report["attempts"].append({"url": url, "status": "ok", "bytes": len(data)})
            return report

        except Exception as e:
            if tmp.exists():
                tmp.unlink()
            last_error = f"{type(e).__name__}: {e}"
            status_code = getattr(e, "code", "")
            report["attempts"].append({"url": url, "status": "error", "code": status_code, "error": last_error})

    report["status"] = "error"
    report["error"] = last_error
    return report


def run_cmd(cmd: list[str], cwd: Path | None = None) -> str:
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            f"cmd={' '.join(cmd)}\n"
            f"stdout={p.stdout[-2000:]}\n"
            f"stderr={p.stderr[-2000:]}"
        )
    return p.stdout.strip()


def fetch_by_sparse_git(repo_url: str, filename: str, path: Path, refresh: bool = False) -> dict[str, Any]:
    report = {
        "method": "git_sparse_checkout",
        "repo_url": repo_url,
        "filename": filename,
        "path": str(path),
        "downloaded": False,
        "attempts": [],
    }

    if path.exists() and not refresh and looks_like_player_csv(path):
        report["status"] = "cached"
        return report

    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sackmann_players_") as td:
        tmp_root = Path(td)
        repo_dir = tmp_root / "repo"
        try:
            run_cmd(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", repo_url, str(repo_dir)])
            run_cmd(["git", "-C", str(repo_dir), "sparse-checkout", "set", filename])
            src = repo_dir / filename
            if not looks_like_player_csv(src):
                raise RuntimeError(f"{filename} not found or not a player CSV after sparse checkout.")
            shutil.copyfile(src, path)
            report["status"] = "downloaded"
            report["downloaded"] = True
            report["bytes"] = path.stat().st_size
            report["attempts"].append({"status": "ok"})
            return report
        except Exception as e:
            report["status"] = "error"
            report["error"] = f"{type(e).__name__}: {e}"
            report["attempts"].append({"status": "error", "error": report["error"]})
            raise


def fetch_player_file(
    *,
    urls: list[str],
    repo_url: str,
    filename: str,
    path: Path,
    refresh: bool,
) -> dict[str, Any]:
    url_report = download_from_urls(urls, path, refresh=refresh)
    if url_report.get("status") in {"cached", "downloaded"} and looks_like_player_csv(path):
        return url_report

    git_report = fetch_by_sparse_git(repo_url, filename, path, refresh=True)
    git_report["url_fallback_report"] = url_report
    return git_report


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

    atp_path = args.raw_dir / ATP_FILE
    wta_path = args.raw_dir / WTA_FILE

    atp_download_report = fetch_player_file(
        urls=ATP_URLS,
        repo_url=ATP_REPO,
        filename=ATP_FILE,
        path=atp_path,
        refresh=args.refresh,
    )
    wta_download_report = fetch_player_file(
        urls=WTA_URLS,
        repo_url=WTA_REPO,
        filename=WTA_FILE,
        path=wta_path,
        refresh=args.refresh,
    )

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
            "The script first tries raw GitHub URLs and falls back to sparse git checkout if raw download fails.",
        ],
    }

    write_json(args.out, metadata)
    write_json(REPORT_JSON, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

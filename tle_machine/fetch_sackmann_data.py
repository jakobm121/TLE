from __future__ import annotations

import argparse
from datetime import datetime, timezone

import requests

from .config import RAW_ATP_DIR, RAW_WTA_DIR, SACKMANN_FILES, START_YEAR
from .utils import ensure_dirs, now_utc_iso, write_json


def fetch_file(url: str, out_path, timeout: int = 30) -> tuple[bool, int, str | None]:
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 404:
            return False, 0, "404"
        r.raise_for_status()
        text = r.text
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        return True, len(text.splitlines()), None
    except Exception as exc:
        return False, 0, str(exc)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--end-year", type=int, default=datetime.now(timezone.utc).year)
    args = parser.parse_args(argv)

    ensure_dirs(RAW_ATP_DIR, RAW_WTA_DIR)
    files = []
    for year in range(args.start_year, args.end_year + 1):
        for source_name, cfg in SACKMANN_FILES.items():
            filename = cfg["template"].format(year=year)
            url = f"{cfg['base_url']}/{filename}"
            out_path = cfg["raw_dir"] / filename
            ok, lines, error = fetch_file(url, out_path)
            files.append({
                "source_name": source_name,
                "year": year,
                "url": url,
                "path": str(out_path.relative_to(out_path.parents[3])),
                "downloaded": ok,
                "lines": lines,
                "error": error,
            })

    report = {
        "generated_at": now_utc_iso(),
        "start_year": args.start_year,
        "end_year": args.end_year,
        "files_total": len(files),
        "files_downloaded": sum(1 for f in files if f["downloaded"]),
        "files": files,
    }
    write_json(RAW_ATP_DIR.parent / "fetch_report.json", report)
    print(report)


if __name__ == "__main__":
    main()

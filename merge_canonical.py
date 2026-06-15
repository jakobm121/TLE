from __future__ import annotations

from collections import Counter, defaultdict

from .config import CANONICAL_DIR, SOURCE_SACKMANN_DIR
from .utils import ensure_dirs, iter_jsonl_gz, now_utc_iso, read_json, write_json, write_jsonl_gz


def main() -> None:
    ensure_dirs(CANONICAL_DIR)
    manifest = read_json(SOURCE_SACKMANN_DIR / "manifest.json", {})
    by_year = defaultdict(list)
    counters = Counter()

    for yf in manifest.get("year_files", []):
        path = SOURCE_SACKMANN_DIR / str(yf["path"]).split("data/source/sackmann/")[-1]
        if not path.exists():
            path = __import__("pathlib").Path(yf["path"])
        for row in iter_jsonl_gz(path):
            by_year[int(row["year"])].append(row)
            counters["canonical_matches"] += 1
            counters[f"source_{row.get('source','unknown')}"] += 1
            counters[f"level_{row.get('level','unknown')}"] += 1
            counters[f"surface_{row.get('surface','unknown')}"] += 1
            counters[f"gender_{row.get('gender','unknown')}"] += 1

    year_files = []
    for year, rows in sorted(by_year.items()):
        rows.sort(key=lambda r: (r["date"], r["match_id"]))
        path = CANONICAL_DIR / f"tle_matches_{year}.jsonl.gz"
        count = write_jsonl_gz(path, rows)
        year_files.append({"year": year, "path": str(path), "matches": count, "created_at": now_utc_iso()})

    out = {
        "generated_at": now_utc_iso(),
        "canonical_matches": counters["canonical_matches"],
        "year_files": year_files,
        "counters": dict(counters),
        "sources": {"sackmann": counters.get("source_sackmann", 0)},
    }
    write_json(CANONICAL_DIR / "manifest.json", out)
    print(out)


if __name__ == "__main__":
    main()

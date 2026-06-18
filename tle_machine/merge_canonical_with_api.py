from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


VALID_GENDERS = {"men", "women"}
VALID_LEVELS = {"grand_slam", "atp_wta", "challenger", "itf", "qualifying"}
VALID_SURFACES = {"hard", "clay", "grass", "carpet", "unknown"}
MAPPED_STATUSES = {"auto_mapped", "manual_mapped"}

DEFAULT_SACKMANN_MANIFEST = Path("data/source/sackmann/manifest.json")
DEFAULT_API_MANIFEST = Path("data/source/api_tennis/manifest.json")
DEFAULT_MAPPING = Path("data/metadata/api_tennis/player_mapping.json")
DEFAULT_PLAYER_ALIASES = Path("data/metadata/sackmann/player_aliases.json")
DEFAULT_OUTPUT_DIR = Path("data/canonical")
DEFAULT_REPORT_DIR = Path("data/reports/canonical")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def short_hash(value: str, n: int = 20) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_jsonl_gz(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                row = json.loads(line)
                if isinstance(row, dict):
                    yield row


def write_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
    tmp.replace(path)
    return len(rows)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def manifest_paths(manifest_path: Path) -> list[Path]:
    if not manifest_path.exists():
        return []
    manifest = read_json(manifest_path)
    files = manifest.get("year_files") or []
    out: list[Path] = []
    for item in files:
        p = item.get("path") if isinstance(item, dict) else None
        if not p:
            continue
        path = Path(p)
        if not path.is_absolute():
            path = Path.cwd() / path
        out.append(path)
    return out


def iter_manifest_matches(manifest_path: Path) -> Iterable[dict[str, Any]]:
    for path in manifest_paths(manifest_path):
        if not path.exists():
            raise FileNotFoundError(f"Missing source match file from manifest {manifest_path}: {path}")
        yield from read_jsonl_gz(path)


def load_player_aliases(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = read_json(path)
    if not isinstance(data, dict):
        return {}

    aliases: dict[str, str] = {}
    for k, v in data.items():
        kk = clean(k)
        vv = clean(v)
        if not kk or not vv or kk == vv:
            continue
        if not kk.startswith(("men:sackmann:", "women:sackmann:")):
            continue
        if not vv.startswith(("men:sackmann:", "women:sackmann:")):
            continue
        if kk.split(":", 1)[0] != vv.split(":", 1)[0]:
            continue
        aliases[kk] = vv
    return aliases


def resolve_alias(player_key_value: str, aliases: dict[str, str], counters: Counter[str], prefix: str) -> str:
    original = clean(player_key_value)
    if not original:
        return original

    current = original
    seen: set[str] = set()
    while current in aliases:
        if current in seen:
            counters[f"{prefix}_alias_cycle_detected"] += 1
            return current
        seen.add(current)
        current = aliases[current]

    if current != original:
        counters[f"{prefix}_alias_resolved_player_keys"] += 1
    return current


def player_key(match: dict[str, Any], side: str) -> str:
    player = match.get(side) or {}
    if not isinstance(player, dict):
        return ""
    return clean(player.get("player_key"))


def player_name(match: dict[str, Any], side: str) -> str:
    player = match.get(side) or {}
    if not isinstance(player, dict):
        return ""
    return clean(player.get("name"))


def set_player_key(match: dict[str, Any], side: str, value: str) -> None:
    player = match.get(side)
    if not isinstance(player, dict):
        player = {}
        match[side] = player
    player["player_key"] = value


def apply_aliases_to_match(match: dict[str, Any], aliases: dict[str, str], counters: Counter[str], prefix: str) -> dict[str, Any]:
    out = dict(match)
    if isinstance(match.get("winner"), dict):
        out["winner"] = dict(match["winner"])
    if isinstance(match.get("loser"), dict):
        out["loser"] = dict(match["loser"])

    w_old = player_key(out, "winner")
    l_old = player_key(out, "loser")

    w_new = resolve_alias(w_old, aliases, counters, prefix)
    l_new = resolve_alias(l_old, aliases, counters, prefix)

    changed = False
    if w_old and w_new != w_old:
        out["winner_player_key_original"] = w_old
        set_player_key(out, "winner", w_new)
        changed = True
    if l_old and l_new != l_old:
        out["loser_player_key_original"] = l_old
        set_player_key(out, "loser", l_new)
        changed = True

    if changed:
        counters[f"{prefix}_alias_resolved_matches"] += 1

    return out


def base_match_valid(match: dict[str, Any]) -> tuple[bool, str]:
    gender = clean(match.get("gender"))
    level = clean(match.get("level"))
    surface = clean(match.get("surface"))
    date = clean(match.get("date"))

    if gender not in VALID_GENDERS:
        return False, "invalid_gender"
    if level not in VALID_LEVELS:
        return False, "invalid_level"
    if surface not in VALID_SURFACES:
        return False, "invalid_surface"
    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return False, "invalid_date"
    if not player_key(match, "winner") or not player_key(match, "loser"):
        return False, "missing_player_key"
    if player_key(match, "winner") == player_key(match, "loser"):
        return False, "winner_loser_same_player_key"
    return True, ""


def unordered_players_key(winner_key: str, loser_key: str) -> str:
    a, b = sorted([winner_key, loser_key])
    return f"{a}|{b}"


def strict_duplicate_key(match: dict[str, Any]) -> str:
    players = unordered_players_key(player_key(match, "winner"), player_key(match, "loser"))
    return "|".join([
        clean(match.get("date")),
        clean(match.get("gender")),
        clean(match.get("level")),
        clean(match.get("surface")),
        norm(match.get("round")),
        players,
    ])


def date_players_key(match: dict[str, Any]) -> str:
    players = unordered_players_key(player_key(match, "winner"), player_key(match, "loser"))
    return "|".join([
        clean(match.get("date")),
        clean(match.get("gender")),
        players,
    ])


def canonical_id(match: dict[str, Any]) -> str:
    raw = "|".join([
        clean(match.get("source")),
        clean(match.get("match_id")),
        clean(match.get("date")),
        clean(match.get("gender")),
        player_key(match, "winner"),
        player_key(match, "loser"),
        clean(match.get("score")),
    ])
    return f"tle:{short_hash(raw, 24)}"


def load_mapping(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing API player mapping: {path}")
    data = read_json(path)
    mapping = data.get("mapping") if isinstance(data, dict) else None
    if not isinstance(mapping, dict):
        raise ValueError(f"Invalid mapping JSON, missing object 'mapping': {path}")
    return {str(k): v for k, v in mapping.items() if isinstance(v, dict)}


def mapped_player(
    api_player: dict[str, Any],
    mapping: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    counters: Counter[str],
) -> tuple[bool, str, str, str]:
    api_key = clean(api_player.get("player_key"))
    if not api_key:
        return False, "", "", "missing_api_player_key"

    item = mapping.get(api_key)
    if not item:
        return False, "", "", "missing_mapping_entry"

    status = clean(item.get("status"))
    target_original = clean(item.get("sackmann_player_key"))
    if status not in MAPPED_STATUSES or not target_original:
        return False, "", target_original, status or "not_mapped"

    target_resolved = resolve_alias(target_original, aliases, counters, "api_mapping")
    if target_resolved != target_original:
        counters["api_mapping_alias_targets_resolved"] += 1

    return True, target_resolved, target_original, status


def convert_api_match(
    match: dict[str, Any],
    mapping: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    counters: Counter[str],
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    winner = match.get("winner") if isinstance(match.get("winner"), dict) else {}
    loser = match.get("loser") if isinstance(match.get("loser"), dict) else {}

    w_ok, w_target, w_target_original, w_status = mapped_player(winner, mapping, aliases, counters)
    l_ok, l_target, l_target_original, l_status = mapped_player(loser, mapping, aliases, counters)

    detail = {
        "api_match_id": clean(match.get("match_id")),
        "api_event_key": clean(match.get("api_event_key")),
        "date": clean(match.get("date")),
        "gender": clean(match.get("gender")),
        "level": clean(match.get("level")),
        "surface": clean(match.get("surface")),
        "tourney_name": clean(match.get("tourney_name")),
        "round": clean(match.get("round")),
        "winner_api_key": clean(winner.get("player_key")),
        "winner_api_name": clean(winner.get("name")),
        "winner_mapping_status": w_status,
        "winner_sackmann_key": w_target,
        "winner_sackmann_key_original": w_target_original,
        "loser_api_key": clean(loser.get("player_key")),
        "loser_api_name": clean(loser.get("name")),
        "loser_mapping_status": l_status,
        "loser_sackmann_key": l_target,
        "loser_sackmann_key_original": l_target_original,
    }

    if not w_ok and not l_ok:
        return None, "none_mapped", detail
    if not w_ok or not l_ok:
        return None, "one_mapped", detail
    if w_target == l_target:
        return None, "mapped_winner_loser_same_player", detail

    converted = {
        "match_id": f"api_tennis_mapped:{clean(match.get('api_event_key')) or short_hash(clean(match.get('match_id')), 16)}",
        "source": "api_tennis",
        "source_file": clean(match.get("source_file")),
        "api_event_key": clean(match.get("api_event_key")),
        "date": clean(match.get("date")),
        "year": int(clean(match.get("date"))[:4]),
        "gender": clean(match.get("gender")),
        "level": clean(match.get("level")),
        "surface": clean(match.get("surface")),
        "surface_raw": clean(match.get("surface_raw")),
        "tourney_id": clean(match.get("tourney_id")),
        "tourney_name": clean(match.get("tourney_name")),
        "round": clean(match.get("round")),
        "score": clean(match.get("score")),
        "winner": {
            "name": clean(winner.get("name")),
            "player_key": w_target,
            "api_player_key": clean(winner.get("player_key")),
            "api_player_id": clean(winner.get("api_player_id")),
        },
        "loser": {
            "name": clean(loser.get("name")),
            "player_key": l_target,
            "api_player_key": clean(loser.get("player_key")),
            "api_player_id": clean(loser.get("api_player_id")),
        },
        "mapping": {
            "winner_status": w_status,
            "loser_status": l_status,
            "winner_sackmann_key_original": w_target_original,
            "loser_sackmann_key_original": l_target_original,
        },
    }

    ok, reason = base_match_valid(converted)
    if not ok:
        return None, reason, detail

    return converted, "both_mapped", detail


def same_winner(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return player_key(a, "winner") == player_key(b, "winner") and player_key(a, "loser") == player_key(b, "loser")


def find_duplicate(
    api_match: dict[str, Any],
    strict_idx: dict[str, list[dict[str, Any]]],
    date_players_idx: dict[str, list[dict[str, Any]]],
) -> tuple[str, dict[str, Any] | None]:
    strict_candidates = strict_idx.get(strict_duplicate_key(api_match), [])
    if strict_candidates:
        return "strict_date_level_surface_round_players", strict_candidates[0]

    date_candidates = date_players_idx.get(date_players_key(api_match), [])
    if len(date_candidates) == 1:
        return "date_gender_players", date_candidates[0]

    if len(date_candidates) > 1:
        api_round = norm(api_match.get("round"))
        refined = [m for m in date_candidates if norm(m.get("round")) == api_round]
        if len(refined) == 1:
            return "date_gender_players_round_refined", refined[0]

        refined = [
            m
            for m in date_candidates
            if clean(m.get("level")) == clean(api_match.get("level"))
            and clean(m.get("surface")) == clean(api_match.get("surface"))
        ]
        if len(refined) == 1:
            return "date_gender_players_level_surface_refined", refined[0]

        return "ambiguous_date_players_duplicate", None

    return "", None


def add_to_indices(
    match: dict[str, Any],
    strict_idx: dict[str, list[dict[str, Any]]],
    date_players_idx: dict[str, list[dict[str, Any]]],
) -> None:
    strict_idx[strict_duplicate_key(match)].append(match)
    date_players_idx[date_players_key(match)].append(match)


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge Sackmann canonical/source with API source using robust API player mapping.")
    parser.add_argument("--sackmann-manifest", type=Path, default=DEFAULT_SACKMANN_MANIFEST)
    parser.add_argument("--api-manifest", type=Path, default=DEFAULT_API_MANIFEST)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--player-aliases", type=Path, default=DEFAULT_PLAYER_ALIASES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args()

    counters: Counter[str] = Counter()
    skipped_rows: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []
    added_rows: list[dict[str, Any]] = []
    conflict_rows: list[dict[str, Any]] = []

    aliases = load_player_aliases(args.player_aliases)
    counters["player_aliases_loaded"] = len(aliases)

    mapping = load_mapping(args.mapping)

    selected: list[dict[str, Any]] = []
    strict_idx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    date_players_idx: dict[str, list[dict[str, Any]]] = defaultdict(list)

    if not args.sackmann_manifest.exists():
        raise FileNotFoundError(f"Missing Sackmann source manifest: {args.sackmann_manifest}")

    for match in iter_manifest_matches(args.sackmann_manifest):
        counters["sackmann_input"] += 1
        match = dict(match)
        match["source"] = "sackmann"
        match = apply_aliases_to_match(match, aliases, counters, "sackmann")

        ok, reason = base_match_valid(match)
        if not ok:
            counters[f"sackmann_skipped_{reason}"] += 1
            skipped_rows.append({
                "source": "sackmann",
                "reason": reason,
                "match_id": clean(match.get("match_id")),
                "date": clean(match.get("date")),
                "winner_sackmann_key": player_key(match, "winner"),
                "loser_sackmann_key": player_key(match, "loser"),
            })
            continue

        sk = strict_duplicate_key(match)
        dk = date_players_key(match)
        if sk in strict_idx:
            counters["sackmann_duplicate_after_alias_skipped"] += 1
            skipped_rows.append({
                "source": "sackmann",
                "reason": "duplicate_after_alias",
                "match_id": clean(match.get("match_id")),
                "date": clean(match.get("date")),
                "winner_sackmann_key": player_key(match, "winner"),
                "loser_sackmann_key": player_key(match, "loser"),
            })
            continue

        match["canonical_match_id"] = canonical_id(match)
        selected.append(match)
        add_to_indices(match, strict_idx, date_players_idx)
        counters["sackmann_kept"] += 1

    if args.api_manifest.exists():
        for raw_api in iter_manifest_matches(args.api_manifest):
            counters["api_input"] += 1
            api_match, mapping_status, detail = convert_api_match(raw_api, mapping, aliases, counters)
            counters[f"api_mapping_{mapping_status}"] += 1

            if api_match is None:
                skipped_rows.append({"source": "api_tennis", "reason": mapping_status, **detail})
                continue

            duplicate_strategy, duplicate = find_duplicate(api_match, strict_idx, date_players_idx)
            if duplicate_strategy == "ambiguous_date_players_duplicate":
                counters["api_skipped_ambiguous_duplicate"] += 1
                skipped_rows.append({"source": "api_tennis", "reason": "ambiguous_duplicate", "duplicate_strategy": duplicate_strategy, **detail})
                continue

            if duplicate is not None:
                if same_winner(api_match, duplicate):
                    counters["api_duplicate_sackmann"] += 1
                    counters[f"api_duplicate_strategy_{duplicate_strategy}"] += 1
                    duplicate_rows.append({
                        **detail,
                        "duplicate_strategy": duplicate_strategy,
                        "sackmann_match_id": clean(duplicate.get("match_id")),
                        "sackmann_winner_key": player_key(duplicate, "winner"),
                        "sackmann_loser_key": player_key(duplicate, "loser"),
                    })
                    continue

                counters["api_conflict_opposite_winner"] += 1
                conflict_rows.append({
                    **detail,
                    "reason": "duplicate_players_date_but_opposite_winner",
                    "duplicate_strategy": duplicate_strategy,
                    "sackmann_match_id": clean(duplicate.get("match_id")),
                    "sackmann_winner_key": player_key(duplicate, "winner"),
                    "sackmann_loser_key": player_key(duplicate, "loser"),
                })
                skipped_rows.append({"source": "api_tennis", "reason": "duplicate_conflicting_winner", "duplicate_strategy": duplicate_strategy, **detail})
                continue

            api_match["canonical_match_id"] = canonical_id(api_match)
            selected.append(api_match)
            add_to_indices(api_match, strict_idx, date_players_idx)
            counters["api_added"] += 1
            counters[f"api_added_level_{api_match['level']}"] += 1
            counters[f"api_added_surface_{api_match['surface']}"] += 1
            added_rows.append({
                **detail,
                "canonical_match_id": api_match["canonical_match_id"],
                "mapped_winner_key": player_key(api_match, "winner"),
                "mapped_loser_key": player_key(api_match, "loser"),
            })
    else:
        counters["api_manifest_missing"] += 1

    selected.sort(key=lambda m: (
        clean(m.get("date")),
        clean(m.get("gender")),
        clean(m.get("level")),
        clean(m.get("surface")),
        norm(m.get("tourney_name")),
        norm(m.get("round")),
        clean(m.get("canonical_match_id")),
    ))

    by_year: dict[int, list[dict[str, Any]]] = defaultdict(list)
    levels: Counter[str] = Counter()
    surfaces: Counter[str] = Counter()
    genders: Counter[str] = Counter()
    sources: Counter[str] = Counter()

    for match in selected:
        year = int(clean(match.get("date"))[:4])
        by_year[year].append(match)
        levels[clean(match.get("level"))] += 1
        surfaces[clean(match.get("surface"))] += 1
        genders[clean(match.get("gender"))] += 1
        sources[clean(match.get("source"))] += 1

    year_files = []
    for year, rows in sorted(by_year.items()):
        out = args.output_dir / f"tle_matches_{year}.jsonl.gz"
        count = write_jsonl_gz(out, rows)
        year_files.append({"year": year, "path": str(out), "matches": count, "created_at": now_utc_iso()})

    generated_at = now_utc_iso()
    manifest = {
        "generated_at": generated_at,
        "source": "canonical_combined",
        "policy": {
            "primary_source": "sackmann",
            "api_source_policy": "add only API matches where both players are auto_mapped/manual_mapped and no Sackmann duplicate/conflict exists",
            "duplicate_detection": [
                "date+gender+level+surface+round+mapped_unordered_players",
                "date+gender+mapped_unordered_players",
                "safe refinement for rare multi-candidate duplicates",
            ],
            "player_alias_policy": "Sackmann player aliases are resolved before validation, dedupe, and API merge.",
        },
        "inputs": {
            "sackmann_manifest": str(args.sackmann_manifest),
            "api_manifest": str(args.api_manifest),
            "player_mapping": str(args.mapping),
            "player_aliases": str(args.player_aliases),
        },
        "player_aliases": {
            "loaded": len(aliases),
            "path": str(args.player_aliases),
        },
        "matches": len(selected),
        "year_files": year_files,
        "counters": dict(counters),
        "sources": dict(sorted(sources.items())),
        "levels": dict(sorted(levels.items())),
        "surfaces": dict(sorted(surfaces.items())),
        "genders": dict(sorted(genders.items())),
    }

    args.report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        **manifest,
        "outputs": {
            "manifest": str(args.output_dir / "manifest.json"),
            "compat_manifest": str(args.output_dir / "tle_matches_manifest.json"),
            "merge_report": str(args.report_dir / "merge_report.json"),
            "api_merge_added_csv": str(args.report_dir / "api_merge_added.csv"),
            "api_merge_skipped_csv": str(args.report_dir / "api_merge_skipped.csv"),
            "api_merge_duplicates_csv": str(args.report_dir / "api_merge_duplicates.csv"),
            "api_merge_conflicts_csv": str(args.report_dir / "api_merge_conflicts.csv"),
        },
        "samples": {
            "api_added": added_rows[:50],
            "api_skipped": skipped_rows[:50],
            "api_duplicates": duplicate_rows[:50],
            "api_conflicts": conflict_rows[:50],
        },
    }

    write_json(args.output_dir / "manifest.json", manifest)
    write_json(args.output_dir / "tle_matches_manifest.json", manifest)
    write_json(args.report_dir / "merge_report.json", report)

    common_fields = [
        "source",
        "reason",
        "duplicate_strategy",
        "date",
        "gender",
        "level",
        "surface",
        "tourney_name",
        "round",
        "api_match_id",
        "api_event_key",
        "winner_api_key",
        "winner_api_name",
        "winner_mapping_status",
        "winner_sackmann_key",
        "winner_sackmann_key_original",
        "loser_api_key",
        "loser_api_name",
        "loser_mapping_status",
        "loser_sackmann_key",
        "loser_sackmann_key_original",
        "sackmann_match_id",
    ]

    write_csv(args.report_dir / "api_merge_skipped.csv", skipped_rows, common_fields + ["winner_sackmann_key", "loser_sackmann_key"])
    write_csv(args.report_dir / "api_merge_duplicates.csv", duplicate_rows, common_fields + ["sackmann_winner_key", "sackmann_loser_key"])
    write_csv(args.report_dir / "api_merge_conflicts.csv", conflict_rows, common_fields + ["sackmann_winner_key", "sackmann_loser_key"])
    write_csv(args.report_dir / "api_merge_added.csv", added_rows, common_fields + ["canonical_match_id", "mapped_winner_key", "mapped_loser_key"])

    print(json.dumps({
        "status": "ok",
        "generated_at": generated_at,
        "canonical_matches": len(selected),
        "counters": dict(counters),
        "sources": dict(sorted(sources.items())),
        "outputs": report["outputs"],
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

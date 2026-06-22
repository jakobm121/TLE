from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

DEFAULT_SACKMANN_MANIFEST = Path("data/source/sackmann/manifest.json")
DEFAULT_TODAY_REVIEW = Path("data/reports/api_tennis/today_mapping_review.csv")
DEFAULT_NO_CANDIDATES = Path("data/reports/api_tennis/today_mapping_no_candidates.csv")
DEFAULT_OUTPUT_CSV = Path("data/reports/api_tennis/today_mapping_sackmann_candidate_search.csv")
DEFAULT_OUTPUT_JSON = Path("data/reports/api_tennis/today_mapping_sackmann_candidate_search.json")
DEFAULT_SUGGESTED_OVERRIDES_JSON = Path("data/reports/api_tennis/today_mapping_suggested_overrides.json")

RAW_SACKMANN_DIR = Path("data/raw/sackmann")
SOURCE_SACKMANN_DIR = Path("data/source/sackmann")
CANONICAL_DIR = Path("data/canonical")

MAPPED_STATUSES = {"auto_mapped", "manual_mapped", "mapped", "override_mapped"}
SACKMANN_KEY_RE = re.compile(r"^(men|women):sackmann:(.+)$")

# Conservative spelling / transliteration hints. These expand the query only; they do not auto-apply mappings.
TRANSLITERATION_HINTS = {
    "nikita": ["mykyta", "nikyta"],
    "mykyta": ["nikita", "nikyta"],
    "faris": ["fares"],
    "fares": ["faris"],
    "zakaria": ["zakaryia", "zakariya", "zakaria"],
    "zakaryia": ["zakaria", "zakariya"],
    "alexander": ["alexandr", "aleksandar", "aleksandr"],
    "alexandr": ["alexander", "aleksandar", "aleksandr"],
    "andrey": ["andrei", "andriy"],
    "andrei": ["andrey", "andriy"],
    "sergey": ["sergei"],
    "sergei": ["sergey"],
    "ilya": ["ilia"],
    "ilia": ["ilya"],
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def strip_accents(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def norm_text(value: Any) -> str:
    text = strip_accents(clean(value)).lower()
    text = text.replace("Ã¢ÂÂ", "'").replace("`", "'").replace("â", "'")
    text = re.sub(r"[^a-z0-9']+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm_text(value).replace("'", "")).strip("_")


def tokens(value: Any) -> list[str]:
    return [t for t in norm_text(value).replace("'", "").split() if t]


def token_key(ts: list[str]) -> str:
    return " ".join(sorted(ts))


def initials(ts: list[str]) -> str:
    return "".join(t[0] for t in ts if t)


def last_non_initial(ts: list[str]) -> str:
    non_initial = [t for t in ts if len(t) > 1]
    if non_initial:
        return non_initial[-1]
    return ts[-1] if ts else ""


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in {None, ""}:
            return default
        return int(float(value))
    except Exception:
        return default


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json_safe(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return read_json(path)
    except Exception:
        return default


def read_jsonl_gz(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                yield row


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def infer_gender(value: Any, path: Path | None = None) -> str:
    raw = clean(value).lower()
    if raw in {"men", "m", "atp", "male"}:
        return "men"
    if raw in {"women", "w", "wta", "female"}:
        return "women"
    text = str(path or "").lower()
    if "wta" in text or "women" in text or "female" in text:
        return "women"
    if "atp" in text or "men" in text or "male" in text:
        return "men"
    return ""


def normalize_sackmann_key(gender: str, raw_id: Any) -> str:
    rid = clean(raw_id)
    if not rid:
        return ""
    m = SACKMANN_KEY_RE.match(rid)
    if m:
        return rid
    if rid.startswith("sackmann:") and gender in {"men", "women"}:
        return f"{gender}:{rid}"
    if gender in {"men", "women"}:
        return f"{gender}:sackmann:{rid}"
    return rid


@dataclass
class RawSackPlayer:
    key: str
    gender: str
    names: Counter[str] = field(default_factory=Counter)
    matches: int = 0
    sources: Counter[str] = field(default_factory=Counter)
    first_seen_date: str = ""
    last_seen_date: str = ""
    example_tournaments: Counter[str] = field(default_factory=Counter)

    @property
    def name(self) -> str:
        if self.names:
            return self.names.most_common(1)[0][0]
        return ""

    @property
    def toks(self) -> list[str]:
        return tokens(self.name)

    @property
    def norm(self) -> str:
        return norm_key(self.name)

    @property
    def token_key(self) -> str:
        return token_key(self.toks)

    @property
    def last(self) -> str:
        return last_non_initial(self.toks)

    @property
    def first_initial(self) -> str:
        ts = self.toks
        return ts[0][0] if ts and ts[0] else ""

    @property
    def all_initials(self) -> str:
        return initials(self.toks)


def add_player(
    players: dict[str, RawSackPlayer],
    *,
    gender: str,
    raw_id: Any,
    name: Any,
    source: str,
    date: Any = "",
    tournament: Any = "",
) -> None:
    gender = infer_gender(gender)
    pname = clean(name)
    key = normalize_sackmann_key(gender, raw_id)
    if not key or not pname or gender not in {"men", "women"}:
        return
    if "/" in pname:
        return
    if key not in players:
        players[key] = RawSackPlayer(key=key, gender=gender)
    p = players[key]
    p.names[pname] += 1
    p.matches += 1
    p.sources[source] += 1
    d = clean(date)
    if d:
        if not p.first_seen_date or d < p.first_seen_date:
            p.first_seen_date = d
        if not p.last_seen_date or d > p.last_seen_date:
            p.last_seen_date = d
    t = clean(tournament)
    if t:
        p.example_tournaments[t] += 1


def get_nested_player(match: dict[str, Any], side: str) -> tuple[Any, Any]:
    obj = match.get(side)
    if isinstance(obj, dict):
        key = obj.get("player_key") or obj.get("sackmann_player_key") or obj.get("id") or obj.get("key")
        name = obj.get("name") or obj.get("player_name")
        if key or name:
            return key, name
    # Flat canonical / raw variants.
    prefixes = [side, side[:1]]
    key_names = [
        f"{side}_id", f"{side}_player_id", f"{side}_player_key", f"{side}_key",
        f"{side[:1]}_id", f"{side[:1]}_player_id", f"{side[:1]}_key",
    ]
    name_names = [
        f"{side}_name", f"{side}_player_name", f"{side}_full_name",
        f"{side[:1]}_name", f"{side[:1]}_player_name",
    ]
    key = next((match.get(k) for k in key_names if clean(match.get(k))), "")
    name = next((match.get(k) for k in name_names if clean(match.get(k))), "")
    return key, name


def add_match_players(players: dict[str, RawSackPlayer], match: dict[str, Any], source: str, path: Path | None = None) -> None:
    gender = infer_gender(match.get("gender") or match.get("tour") or match.get("event_type_type"), path)
    date = match.get("date") or match.get("tourney_date") or match.get("event_date") or match.get("match_date")
    tournament = match.get("tourney_name") or match.get("tournament_name") or match.get("event_name")
    for side in ("winner", "loser"):
        key, name = get_nested_player(match, side)
        add_player(players, gender=gender, raw_id=key, name=name, source=source, date=date, tournament=tournament)


def manifest_paths(manifest_path: Path) -> list[Path]:
    if not manifest_path.exists():
        return []
    manifest = read_json_safe(manifest_path, {})
    files = manifest.get("year_files") or [] if isinstance(manifest, dict) else []
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


def load_from_manifest(players: dict[str, RawSackPlayer], manifest_path: Path, counters: Counter[str]) -> None:
    for path in manifest_paths(manifest_path):
        if not path.exists():
            counters["manifest_missing_files"] += 1
            continue
        counters["manifest_files"] += 1
        for m in read_jsonl_gz(path):
            if clean(m.get("source")) and clean(m.get("source")) != "sackmann":
                continue
            add_match_players(players, m, "source_manifest", path)
            counters["manifest_matches_scanned"] += 1


def load_from_jsonl_globs(players: dict[str, RawSackPlayer], counters: Counter[str]) -> None:
    patterns = [
        (SOURCE_SACKMANN_DIR / "tle_sackmann_matches_*.jsonl.gz", "source_sackmann_jsonl"),
        (CANONICAL_DIR / "tle_matches_*.jsonl.gz", "canonical_jsonl"),
    ]
    seen_paths: set[Path] = set()
    for pattern, source in patterns:
        for path in sorted(pattern.parent.glob(pattern.name)):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            counters[f"{source}_files"] += 1
            for m in read_jsonl_gz(path):
                if source == "canonical_jsonl" and clean(m.get("source")) and clean(m.get("source")) != "sackmann":
                    continue
                add_match_players(players, m, source, path)
                counters[f"{source}_matches_scanned"] += 1


def load_from_raw_csv(players: dict[str, RawSackPlayer], counters: Counter[str]) -> None:
    if not RAW_SACKMANN_DIR.exists():
        return
    for path in sorted(RAW_SACKMANN_DIR.glob("**/*.csv")):
        counters["raw_csv_files"] += 1
        gender_from_path = infer_gender("", path)
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    gender = infer_gender(row.get("gender") or row.get("tour"), path) or gender_from_path
                    date = row.get("tourney_date") or row.get("date") or row.get("match_date")
                    tournament = row.get("tourney_name") or row.get("tournament_name")
                    for side in ("winner", "loser"):
                        key = row.get(f"{side}_id") or row.get(f"{side}_player_id") or row.get(f"{side}_key")
                        name = row.get(f"{side}_name") or row.get(f"{side}_player_name")
                        add_player(players, gender=gender, raw_id=key, name=name, source="raw_sackmann_csv", date=date, tournament=tournament)
                    counters["raw_csv_rows_scanned"] += 1
        except UnicodeDecodeError:
            with path.open("r", encoding="latin-1", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    gender = infer_gender(row.get("gender") or row.get("tour"), path) or gender_from_path
                    date = row.get("tourney_date") or row.get("date") or row.get("match_date")
                    tournament = row.get("tourney_name") or row.get("tournament_name")
                    for side in ("winner", "loser"):
                        key = row.get(f"{side}_id") or row.get(f"{side}_player_id") or row.get(f"{side}_key")
                        name = row.get(f"{side}_name") or row.get(f"{side}_player_name")
                        add_player(players, gender=gender, raw_id=key, name=name, source="raw_sackmann_csv_latin1", date=date, tournament=tournament)
                    counters["raw_csv_rows_scanned"] += 1
        except Exception:
            counters["raw_csv_read_errors"] += 1


def build_sackmann_players(manifest_path: Path) -> tuple[dict[str, RawSackPlayer], dict[str, list[str]], dict[str, Any]]:
    players: dict[str, RawSackPlayer] = {}
    counters: Counter[str] = Counter()
    load_from_manifest(players, manifest_path, counters)
    load_from_jsonl_globs(players, counters)
    load_from_raw_csv(players, counters)
    by_gender: dict[str, list[str]] = defaultdict(list)
    for k, p in players.items():
        if p.gender in {"men", "women"} and p.name:
            by_gender[p.gender].append(k)
    report = {
        "sackmann_players": len(players),
        "by_gender": {g: len(v) for g, v in by_gender.items()},
        "source_counters": dict(counters),
    }
    return players, by_gender, report


def api_name_parts(name: str) -> dict[str, Any]:
    raw = clean(name)
    nts = tokens(raw)
    visible_initials = []
    for part in raw.replace("Ã¢ÂÂ", "'").replace("â", "'").split():
        p = strip_accents(part).strip().strip(",")
        if re.fullmatch(r"[A-Za-z]\.", p):
            visible_initials.append(p[0].lower())
    non_initial = [t for t in nts if len(t) > 1]
    last = non_initial[-1] if non_initial else (nts[-1] if nts else "")
    if len(nts) >= 2 and len(nts[-1]) == 1 and len(nts[-2]) > 1:
        last = nts[-2]
    first_initial = visible_initials[0] if visible_initials else (nts[0][0] if nts else "")
    return {
        "raw": raw,
        "tokens": nts,
        "initials": visible_initials,
        "last": last,
        "first_initial": first_initial,
        "norm": norm_key(raw),
        "token_key": token_key(nts),
        "all_initials": initials(nts),
    }


def expanded_query_variants(api_tokens: list[str]) -> set[str]:
    variants = {" ".join(api_tokens)} if api_tokens else set()
    for i, tok in enumerate(api_tokens):
        for repl in TRANSLITERATION_HINTS.get(tok, []):
            alt = list(api_tokens)
            alt[i] = repl
            variants.add(" ".join(alt))
    # Reversed order candidate: John Fancutt Thomas -> Thomas Fancutt John / Thomas Fancutt.
    if len(api_tokens) >= 2:
        variants.add(" ".join(reversed(api_tokens)))
    if len(api_tokens) >= 3:
        variants.add(" ".join([api_tokens[-1], *api_tokens[:-1]]))
        variants.add(" ".join([api_tokens[-1], api_tokens[-2], *api_tokens[:-2]]))
    return {v for v in variants if v.strip()}


def ordered_initials_contained(short_initials: list[str], full_tokens: list[str]) -> bool:
    if not short_initials:
        return False
    full_initials = [t[0] for t in full_tokens if t]
    pos = -1
    for ini in short_initials:
        try:
            pos = full_initials.index(ini, pos + 1)
        except ValueError:
            return False
    return True


def score_candidate(api_name: str, candidate: RawSackPlayer) -> tuple[float, str]:
    api = api_name_parts(api_name)
    c_tokens = candidate.toks
    c_norm = candidate.norm
    c_last = candidate.last
    c_first_initial = candidate.first_initial
    c_token_key = candidate.token_key

    if not api["norm"] or not c_norm:
        return 0.0, "empty"

    if api["norm"] == c_norm:
        return 1.0, "exact_normalized_raw"

    if api["token_key"] and api["token_key"] == c_token_key:
        return 0.995, "exact_token_set_raw"

    # Name moved from suffix to prefix: John Fancutt Thomas -> Thomas Fancutt.
    if len(api["tokens"]) >= 3 and len(c_tokens) >= 2:
        api_set = set(api["tokens"])
        c_set = set(c_tokens)
        if c_set.issubset(api_set) and api["last"] in c_set:
            return 0.965, "subset_reordered_name"

    # Explicit transliteration variants; kept below hard auto territory unless ratio is also very strong.
    for variant in expanded_query_variants(api["tokens"]):
        vn = norm_key(variant)
        if vn == c_norm:
            return 0.960, "transliteration_exact_variant"
        if token_key(tokens(variant)) == c_token_key:
            return 0.955, "transliteration_token_variant"

    ratio = SequenceMatcher(None, api["norm"], c_norm).ratio()
    token_overlap = 0.0
    if api["tokens"] and c_tokens:
        aa, bb = set(api["tokens"]), set(c_tokens)
        token_overlap = len(aa & bb) / len(aa | bb)

    method = "fuzzy_raw"
    score = ratio * 0.66 + token_overlap * 0.12

    if api["last"] and api["last"] == c_last:
        score += 0.15
        method = "same_last_raw"
    elif api["last"] and api["last"] in c_tokens:
        score += 0.11
        method = "last_inside_name_raw"

    if api["first_initial"] and api["first_initial"] == c_first_initial:
        score += 0.10
        method = "initial_surname_raw" if method in {"same_last_raw", "last_inside_name_raw"} else "same_initial_raw"

    if api["last"] and api["last"] == c_last and api["first_initial"] and api["first_initial"] == c_first_initial:
        score = max(score, 0.975)
        method = "exact_initial_form_raw"

    if len(api["initials"]) >= 2 and api["last"] and api["last"] == c_last:
        if ordered_initials_contained(api["initials"], c_tokens[:-1]):
            score = max(score, 0.955)
            method = "multi_initial_surname_raw"

    # API full name vs Sackmann initials/name abbreviation.
    c_initial_tokens = [t for t in c_tokens if len(t) == 1 and t.isalpha()]
    if c_initial_tokens and c_last and c_last in api["tokens"]:
        api_name_tokens = [t for t in api["tokens"] if t != c_last and len(t) > 1]
        if ordered_initials_contained(c_initial_tokens, api_name_tokens):
            score = max(score, 0.925)
            method = "expanded_vs_sackmann_initials_raw"

    return round(min(score, 0.999), 6), method


def load_review(path: Path, no_candidates_path: Path | None = None) -> list[dict[str, str]]:
    rows = read_csv_rows(path)
    # Prefer today_mapping_no_candidates.csv if present; append rows not already in review.
    if no_candidates_path and no_candidates_path.exists():
        seen = {clean(r.get("api_player_key")) or clean(r.get("api_raw_player_key")) for r in rows}
        for r in read_csv_rows(no_candidates_path):
            key = clean(r.get("api_player_key")) or clean(r.get("api_raw_player_key"))
            if key and key not in seen:
                rows.append(r)
                seen.add(key)
    if not rows:
        raise FileNotFoundError(f"Missing or empty today mapping review CSV: {path}")
    return rows


def is_relevant_row(row: dict[str, str]) -> bool:
    name = clean(row.get("api_name") or row.get("player_name") or row.get("name"))
    if not name or "/" in name:
        return False
    lower = f"{clean(row.get('event_type_types'))} {clean(row.get('event_names'))}".lower()
    if any(x in lower for x in ("doubles", "teams", "davis cup", "billie jean king", "fed cup")):
        return False
    status = clean(row.get("mapping_status") or row.get("status"))
    issue = clean(row.get("issue") or row.get("reason"))
    if status in MAPPED_STATUSES:
        return False
    # 10d is for problematic today players. Keep all unmapped rows, but especially no-candidate ones.
    return status not in MAPPED_STATUSES or "no" in issue.lower()


def choose_action(best_score: float, margin: float, method: str, candidates_count: int) -> str:
    # This script is a review/search layer, not auto-mapping. "safe_override" means safe to review quickly.
    if best_score >= 0.985 and margin >= 0.030:
        return "safe_override"
    if best_score >= 0.970 and margin >= 0.050 and method in {"exact_initial_form_raw", "exact_normalized_raw", "exact_token_set_raw"}:
        return "safe_override"
    if best_score >= 0.955 and margin >= 0.030:
        return "review_likely"
    if best_score >= 0.930:
        return "manual_review"
    if best_score >= 0.850:
        return "weak_candidate"
    return "very_weak_candidate"


def main() -> int:
    ap = argparse.ArgumentParser(description="Deep-search raw Sackmann source players for candidates for today's unmapped API players.")
    ap.add_argument("--sackmann-manifest", default=str(DEFAULT_SACKMANN_MANIFEST))
    ap.add_argument("--today-review", default=str(DEFAULT_TODAY_REVIEW))
    ap.add_argument("--today-no-candidates", default=str(DEFAULT_NO_CANDIDATES))
    ap.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    ap.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    ap.add_argument("--suggested-overrides-json", default=str(DEFAULT_SUGGESTED_OVERRIDES_JSON))
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--min-score", type=float, default=0.70)
    args = ap.parse_args()

    players, by_gender, source_report = build_sackmann_players(Path(args.sackmann_manifest))
    review = [r for r in load_review(Path(args.today_review), Path(args.today_no_candidates)) if is_relevant_row(r)]

    out_rows: list[dict[str, Any]] = []
    suggested_overrides: dict[str, str] = {}
    counters: Counter[str] = Counter()

    for r in review:
        api_key = clean(r.get("api_player_key") or r.get("api_raw_player_key"))
        api_name = clean(r.get("api_name") or r.get("player_name") or r.get("name"))
        gender = infer_gender(r.get("gender") or r.get("tour"))
        if not api_key or not api_name or gender not in {"men", "women"}:
            counters["skipped_bad_review_row"] += 1
            continue

        counters["review_players"] += 1
        scored: list[tuple[float, str, RawSackPlayer]] = []
        api = api_name_parts(api_name)

        # Candidate prefilter: gender + same last / surname-inside / same first initial / token overlap / known transliteration.
        query_variants = {norm_key(v) for v in expanded_query_variants(api["tokens"])}
        for key in by_gender.get(gender, []):
            p = players[key]
            c_tokens = p.toks
            if not c_tokens:
                continue
            possible = False
            if api["last"] and (api["last"] == p.last or api["last"] in c_tokens):
                possible = True
            if api["first_initial"] and api["first_initial"] == p.first_initial and api["last"]:
                possible = True
            if set(api["tokens"]) & set(c_tokens):
                possible = True
            if p.norm in query_variants or p.token_key in {token_key(tokens(v)) for v in query_variants}:
                possible = True
            # Very small gender pools are fine, but avoid full brute force unless name is short.
            if not possible and len(api["tokens"]) > 1:
                continue

            score, method = score_candidate(api_name, p)
            if score >= float(args.min_score):
                scored.append((score, method, p))

        scored.sort(key=lambda x: (x[0], int(x[2].matches or 0)), reverse=True)
        top = scored[: max(1, args.top)]

        if not top:
            counters["no_candidate"] += 1
            out_rows.append({
                **r,
                "api_player_key": api_key,
                "api_name": api_name,
                "gender": gender,
                "rank": "",
                "sackmann_player_key": "",
                "sackmann_name": "",
                "sackmann_matches": "",
                "score": "",
                "margin": "",
                "method": "",
                "candidate_sources": "",
                "first_seen_date": "",
                "last_seen_date": "",
                "example_tournament": "",
                "suggested_action": "no_candidate",
                "suggested_override_json": "",
            })
            continue

        best_score = top[0][0]
        second_score = top[1][0] if len(top) > 1 else 0.0
        margin = round(best_score - second_score, 6)
        action = choose_action(best_score, margin, top[0][1], len(top))
        counters[action] += 1

        for rank, (score, method, p) in enumerate(top, start=1):
            override = ""
            if rank == 1 and action in {"safe_override", "review_likely"}:
                suggested_overrides[api_key] = p.key
                override = json.dumps({api_key: p.key}, ensure_ascii=False)
            out_rows.append({
                **r,
                "api_player_key": api_key,
                "api_name": api_name,
                "gender": gender,
                "rank": rank,
                "sackmann_player_key": p.key,
                "sackmann_name": p.name,
                "sackmann_matches": p.matches,
                "score": score,
                "margin": margin if rank == 1 else "",
                "method": method,
                "candidate_sources": ";".join(f"{k}:{v}" for k, v in p.sources.most_common()),
                "first_seen_date": p.first_seen_date,
                "last_seen_date": p.last_seen_date,
                "example_tournament": p.example_tournaments.most_common(1)[0][0] if p.example_tournaments else "",
                "suggested_action": action if rank == 1 else "candidate",
                "suggested_override_json": override,
            })

    fieldnames = [
        "api_player_key",
        "api_raw_player_key",
        "api_name",
        "gender",
        "mapping_status",
        "issue",
        "reason",
        "today_match_count",
        "opponents_today",
        "event_names",
        "event_type_types",
        "rank",
        "sackmann_player_key",
        "sackmann_name",
        "sackmann_matches",
        "score",
        "margin",
        "method",
        "candidate_sources",
        "first_seen_date",
        "last_seen_date",
        "example_tournament",
        "suggested_action",
        "suggested_override_json",
    ]
    write_csv(Path(args.output_csv), out_rows, fieldnames)
    write_json(Path(args.suggested_overrides_json), suggested_overrides)

    report = {
        "status": "ok",
        "generated_at": now_utc_iso(),
        "today_review": str(args.today_review),
        "today_no_candidates": str(args.today_no_candidates),
        "sackmann_manifest": str(args.sackmann_manifest),
        "players_in_review": len(review),
        "sackmann_index": source_report,
        "rows_written": len(out_rows),
        "suggested_overrides": len(suggested_overrides),
        "counters": dict(counters),
        "outputs": {
            "csv": str(args.output_csv),
            "json": str(args.output_json),
            "suggested_overrides_json": str(args.suggested_overrides_json),
        },
        "safety_note": "This script only suggests candidates/overrides. It does not modify player_mapping.json or player_mapping_overrides.json.",
    }
    write_json(Path(args.output_json), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

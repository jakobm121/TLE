# TLE Machine - Phase 1 Sackmann Core

This repo starts with a clean Sackmann-only TLE pipeline.

## Phase 1 goal

Build final Elo ratings from Sackmann history only:

```text
Sackmann CSV -> source/sackmann -> canonical -> ratings
```

## First run

1. Enable GitHub Actions.
2. Set Workflow permissions to **Read and write permissions**.
3. Run workflow: **TLE Sackmann Refresh**.

No API key is needed for Phase 1.

## Outputs

```text
data/source/sackmann/manifest.json
data/canonical/manifest.json
data/ratings/tle_player_ratings.json
data/ratings/manifest.json
data/reports/ratings/rating_build_report.json
```

## Commands locally

```bash
python -m pip install -r requirements.txt
python -m tle_machine.fetch_sackmann_data
python -m tle_machine.import_sackmann_history
python -m tle_machine.merge_canonical
python -m tle_machine.build_ratings
python -m tle_machine.reports
```

## Next phases

Phase 2 adds API results and tournament metadata.
Phase 3 adds player mapping.
Phase 4 adds scanner and settlement.

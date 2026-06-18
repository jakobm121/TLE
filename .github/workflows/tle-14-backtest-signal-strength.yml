name: TLE 14 Backtest Signal Strength

on:
  workflow_dispatch:
    inputs:
      min_level_matches:
        description: "Minimum pre-match level matches for both players"
        required: false
        default: "10"
      min_surface_matches:
        description: "Minimum pre-match surface matches for both players"
        required: false
        default: "5"

permissions:
  contents: write

jobs:
  backtest-signal-strength:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Backtest signal strength
        run: |
          python -m tle_machine.backtest_signal_strength \
            --min-level-matches "${{ github.event.inputs.min_level_matches }}" \
            --min-surface-matches "${{ github.event.inputs.min_surface_matches }}"

      - name: Commit signal strength outputs
        run: |
          git config user.name "github-actions"
          git config user.email "github-actions@github.com"

          git add data/backtest/signal_strength_blend_80_20.csv || true
          git add data/backtest/signal_strength_blend_80_20_by_level.csv || true
          git add data/backtest/signal_strength_blend_80_20_by_surface.csv || true
          git add data/reports/backtest/signal_strength_blend_80_20_report.json || true

          if git diff --cached --quiet; then
            echo "No signal strength backtest changes to commit."
          else
            git commit -m "Backtest Elo signal strength"
            git push
          fi

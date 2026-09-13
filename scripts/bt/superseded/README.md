# Superseded research scripts

Kept for provenance, out of the reproduction path. Nothing in `scripts/bt/`,
`portfolio.py`, `make_report.py`, or the docs reads their output.

| Script | Writes | Superseded by | Why |
|---|---|---|---|
| `replay_spotlag2.py` | *nothing* (prints a table) | `replay_spotlag3.py` | Intermediate parameter sweep. Uses the pre-correction fill logic, so its table reproduces the optimistic fills that `BACKTEST_JAN_SEP_2026.md` retracts ("filling at quotes that did not exist"). Running it will mislead. |
| `measure_lag.py` | `lag_samples.jsonl` | `measure_lag2.py` | Sparse sampler. `analyze_lag.py` reads `lag_dense.jsonl` from `measure_lag2.py`; nothing reads `lag_samples.jsonl`. |

**Not** superseded, despite the naming — both are live in the pipeline:

- `replay_spotlag.py` → `spotlag_trades.jsonl`, read by `make_report.py` as the **naive** baseline.
- `replay_spotlag3.py` → `spotlag_trades_final.jsonl`, read by `make_report.py` and `portfolio.py` as the **corrected** result.

The report compares the two, so deleting either breaks reproduction.

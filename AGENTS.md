# Repository Guidelines

`xtquant-big-convert` bridges Big QMT's built-in Python to external programs over Redis/ZMQ, and exposes a MiniQMT (`xtquant`)-compatible API on top. Python >=3.8; QMT-embedded code must stay 3.6-safe.

## Project Structure & Module Organization

- `src/bigqmt_signal_trader/` — core bridge package (`adapters/`, `transports/`, RPC, order/trade handling).
- `src/bigqmt_backtest/` — ZMQ backtest bridge; `src/xtquant/` — MiniQMT-compatible shim (`xtdata`, `xttrader`, `xtconstant`).
- `src/BIGQMT_*.py`, `src/bigqmt_signal_trader_*.py` — top-level strategy/entry modules loaded directly by QMT.
- `tests/bigqmt_signal_trader/`, `tests/bigqmt_backtest/` — offline suites; `test_all_apis.py` — live tests; `run_all_tests.py` — aggregate runner.
- `tools/` — single-file builders; `deploy/` — PowerShell deployment; `docs/`, `examples/`, `bigqmt_no_redis/`, `qmt-trader/`.

## Build, Test, and Development Commands

```bash
pip install -e ".[dev]"        # editable install with pytest
python run_all_tests.py         # offline suite; add -v, --group signal_trader|backtest, --live
python -m pytest tests/bigqmt_signal_trader -q
python tools/build_single_file.py                 # writes src/BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py (gitignored)
python tools/build_no_redis_single_file_flat.py   # no-redis flat build
python test_all_apis.py         # live RPC tests; needs QMT + Redis running
```

## Coding Style & Naming Conventions

- 4-space indent; `snake_case` modules/functions/variables, `PascalCase` classes, `UPPER_SNAKE_CASE` constants.
- Keep the `# coding: utf-8` header on modules. Docstrings explain *why*, and cite issue numbers for behavioral decisions.
- No linter/formatter is configured; match surrounding style. Avoid 3.8+-only syntax in code shared with QMT's python36.
- Tests use `unittest` cases run under pytest: `tests/**/test_*.py`, methods `test_*`.

## Testing Guidelines

Offline tests must pass without QMT or Redis; anything needing a live bridge belongs behind `--live`. Tests are behavior contracts — each bug fix should add or extend a regression test, referencing the issue (e.g. `#387`). Run the full offline suite before opening a PR.

## Commit & Pull Request Guidelines

Commit messages follow Conventional Commits with Chinese summaries, e.g. `fix: 公式族进注入捕获名单（#374 收尾） (#381)` or `release: 0.3.61`. Reference issue numbers as `(#123)`.

PRs should describe the behavior change, list verification performed, and link issues. For releases, keep `pyproject.toml`, `src/bigqmt_signal_trader/version.py`, and `CHANGELOG.md` in sync (enforced by `tests/test_version_stamp.py`), and note when a strategy-file change requires a QMT strategy restart.

## Security & Configuration Tips

Never commit account ids, Redis passwords, or QMT paths: keep `*_local_config.py` / `*_client_config.py` untracked (`*.local.py` is ignored). Remote order methods stay off unless `rpc_allow_order_methods=True` is set deliberately. The Redis upper bound in `pyproject.toml` (`<8.0.0`) is intentional and pinned by `tests/test_dependency_constraints.py`.

Local secrets live in `.env.<profile>` (`.env.dev`, `.env.prod`), both gitignored; only `.env.template` is committed. `bigqmt_signal_trader/env_config.py` parses them, `bigqmt-env` regenerates the two config files, and `BIGQMT_ENV_DISABLE=1` switches discovery off for harnesses. Keep `init_config` out of `env_config`: the single-file builders refuse to embed `init_config.py`, and their check is a plain text scan.

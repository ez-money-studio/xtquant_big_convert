# QMT 2.1.33 / Python 3.12 compatibility audit

Audit date: 2026-10-25.

## Tested terminal

| Item | Value |
|---|---|
| QMT | 2.1.33.0 |
| QMT runtime | CPython 3.12.10, 64-bit |
| Runtime directory | `bin.x64\python312` |
| xtquant | 250807.1.2 (`xtquant_250807`) |
| pandas | 3.0.2 |
| numpy | 2.4.4 |
| Bridge transport in this deployment | named pipe |

Deployment policy: this terminal uses **named pipe only**. Redis and ZMQ are
not acceptable alternatives for this environment and must not be proposed as
fallbacks. The pipe DACL explicitly grants the current logged-in user
read/write so the normal, non-elevated trading client can connect.

The old `python36.dll` remains beside the new directory. Its presence does not
mean QMT is still using Python 3.6; `get_deployment_info()` reports the
interpreter actually executing the bridge.

## API comparison

- The bridge's `src/xtquant/xtconstant.py` has the same 538 definitions and
  values as the new native QMT constants. No trading enum was renamed or
  renumbered.
- New native `XtOrder`, `XtTrade`, `XtPosition` and `XtAsset` models add
  `direction`, `offset_flag`, `secu_account`, `instrument_name`,
  `commission`, position analytics and `fetch_balance`. The bridge already
  returns these fields, so the two contracts now align more closely.
- Native `xtdata.py` grew from 62 to 124 public top-level definitions. It adds
  formula, tabular, ETF/history-ST, Level-2, market/sector and quote-server
  APIs. Twelve old top-level helpers disappeared, notably `init_data_dir`,
  `load_global_config`, `get_industry`, `get_trade_times` and the old
  `merge_data*` helpers.
- The 2025 native SDK renamed/removed `get_trade_times`; trading-session data
  is now available through `get_trading_period`,
  `get_kline_trading_period`, `get_all_trading_periods` and
  `get_all_kline_trading_periods`. `get_industry` is no longer a native
  `xtdata` method either. The bridge's existing MiniQMT aliases remain, but a
  QMT 3.12 deployment must verify those aliases against the live strategy
  context before relying on them.
- The new `ContextInfo` stub exposes the key bridge methods, but
  `get_market_data_ex_ori` is absent. The bridge already treats that method as
  optional and falls back to `get_market_data_ex`.
- The bridge does not import native `xtdata` on the server path: it talks to
  the QMT-injected `ContextInfo`/global functions and exposes its own
  MiniQMT-compatible shim. Therefore the removed native helpers do not by
  themselves break the bridge, but external code importing native xtdata must
  be audited separately.
- Native `xttrader.py` keeps the same four public classes and the core
  `XtQuantTrader` query/order/cancel/callback family is present. It adds bank
  transfer, fund/security transfer, smart-algorithm and SMT methods, and removes
  five older appointment aliases. The bridge's `BigQmtXtTrader` keeps those old
  appointment aliases, but does not claim support for the new advanced trading
  methods until each one has a tested server-side route.

## Runtime verification

After loading the bridge strategy, call:

```python
from bigqmt_signal_trader.xtquant_compat import configure, xtdata

configure()
print(xtdata.get_deployment_info())
print(xtdata.call_method("probe_capabilities"))
```

Required result:

- `runtime.python_version == "3.12.x"`
- `runtime.profile == "python312"`
- `runtime.abi` ends in `-cp312-win_amd64.pyd`
- `qmt_globals.passorder`, `get_trade_detail_data` and the download functions
  are bound
- `thread_routing` still routes trade-context calls to `adjust_thread`

This online portion requires the QMT strategy to be restarted and loaded by
the QMT GUI. Static package imports and the offline bridge suite do not prove
that QMT injected its strategy globals.

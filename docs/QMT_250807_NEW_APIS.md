# QMT 2.1.33 / xtquant 250807 新接口审计

审计日期：2026-10-25。对象是本机 `bin.x64\python312` 的
`xtquant 250807.1.2`，不是外部安装的另一个 xtquant 版本。

状态定义：

- **已支持**：桥已有 MiniQMT 方法或 RPC 路由，并有离线测试。
- **已捕获**：QMT 注入的全局函数已进入统一捕获表，但尚不绕过 RPC 白名单。
- **原生接口**：只存在于原生 `xtdata`/`xttrader`，大 QMT 内置进程不保证可调用。
- **未支持**：桥没有服务端实现，不应假装兼容。

## 1. 全局函数注入覆盖

捕获名单的权威定义是
[`src/bigqmt_signal_trader/qmt_globals.py`](../src/bigqmt_signal_trader/qmt_globals.py)。
当前共 **55 个**候选名称：3 个核心入口 + 52 个扩展入口。

| 分组 | 函数 |
|---|---|
| 核心 | `passorder`, `cancel`, `get_trade_detail_data` |
| 下载 | `download_history_data`, `download_history_data2`, `down_history_data`, `download_sector_data`, `download_sector_weight` |
| 公式 | `call_formula`, `call_formula_batch`, `subscribe_formula`, `unsubscribe_formula`, `get_formula_result`, `gen_factor_index` |
| 交易查询 | `get_history_trade_detail_data`, `get_value_by_order_id`, `get_last_order_id`, `get_ipo_data`, `get_new_purchase_limit` |
| 信用 | `query_credit_account`, `query_credit_opvolume`, `get_assure_contract`, `get_enable_short_contract`, `get_unclosed_compacts`, `get_closed_compacts`, `get_debt_contract` |
| 期权/港股通 | `get_option_subject_position`, `get_comb_option`, `get_hkt_exchange_rate` |
| 算法/任务 | `algo_passorder`, `smart_algo_passorder`, `get_smart_algo_param`, `cancel_task`, `pause_task`, `resume_task` |
| 篮子 | `get_basket`, `set_basket` |
| 引用数据 | `get_etf_info`, `get_etf_iopv`, `get_st_status`, `ext_data`, `ext_data_rank`, `ext_data_rank_range`, `ext_data_range`, `get_factor_value`, `get_factor_rank`, `call_vba`, `get_vba_func_result`, `is_suspended_stock`, `get_his_st_data`, `get_backtest_index`, `get_group_result`, `create_extend_data`, `reset_extend_data_stock_list`, `set_extend_data_value` |

捕获不等于开放。`probe_capabilities.qmt_globals` 会逐个报告本轮终端是否真的注入，
并提供 `qmt_global_counts.expected/bound`。算法下单、任务操作和篮子写入即使已捕获，
仍没有 RPC 路由；以后开放时必须进入 `ORDER_METHODS` 并受
`rpc_allow_order_methods` 控制。

## 2. `XtQuantTrader` 新增接口

| 分组 | 新增或变化 | 桥状态 |
|---|---|---|
| 查询 | `query_position_statistics`, `query_secu_account` | 已支持 |
| 通用数据 | `export_data`, `query_data` | 未支持 |
| 划拨查询 | `query_com_fund`, `query_com_position` | 未支持 |
| 银证转账 | `bank_transfer_in`, `bank_transfer_in_async`, `bank_transfer_out`, `bank_transfer_out_async`, `query_bank_info`, `query_bank_amount`, `query_bank_transfer_stream` | 未支持 |
| 资金/证券划转 | `fund_transfer`, `secu_transfer` | 未支持 |
| CTP 内转 | `ctp_transfer_option_to_future`, `ctp_transfer_option_to_future_async`, `ctp_transfer_future_to_option`, `ctp_transfer_future_to_option_async` | 未支持 |
| 智能算法 | `get_smart_algo_param`, `smart_algo_order_async`, `query_smart_algo_task`, `cancel_smart_algo_task_async` | 全局参数查询已捕获；其余未支持 |
| 约券/SMT | `smt_query_quoter`, `smt_negotiate_order_async`, `smt_query_compact`, `smt_query_order`, `smt_appointment_order_async`, `smt_appointment_cancel_async`, `smt_compact_renewal_async`, `smt_compact_return_async` | 未支持 |
| 外部数据 | `sync_transaction_from_external`, `set_timeout` | 未支持 |

新增回调：
`on_bank_transfer_async_response`, `on_ctp_internal_transfer_async_response`,
`on_smart_algo_order_async_response`, `on_operate_smart_task_async_response`。
桥当前不转发这四类新回调。

原生 SDK 删除了五个旧预约接口：
`query_appointment_info`, `query_appointment_info_async`,
`query_smt_secu_info`, `query_smt_secu_rate`, `smt_appointment_async`。
桥为了旧代码兼容仍保留同名包装，但新调用应优先使用上表的新接口。

新增数据结构字段：

- 委托：`direction`, `offset_flag`, `secu_account`, `instrument_name`
- 成交：上述字段 + `commission`
- 持仓：`avg_price`, `direction`, `last_price`, `profit_rate`,
  `secu_account`, `instrument_name`, `float_profit`, `open_date`,
  `position_profit`
- 资产：`fetch_balance`

桥已支持委托/成交的主要新增字段和资产 `fetch_balance`。高层的
`query_stock_positions()` 仍以旧 `PositionSnapshot` 字段为准；需要完整原生持仓字段时，
必须通过原始交易明细接口或后续新增兼容字段，不能假设已有同名属性。

## 3. 原生 `xtdata` 新增接口

以下 74 个顶层定义由 250807 新增；它们只代表原生包暴露，不等于大 QMT 内置进程可达。
当前 pipe 部署关闭 native xtdata，避免连接 `127.0.0.1:58610` 触发券商沙箱。

| 分组 | 接口 |
|---|---|
| 连接/状态 | `connect`, `disconnect`, `hello`, `fetch_quote_server_from_config`, `get_quote_server_config`, `get_quote_server_status`, `show_quote_server_status`, `watch_quote_server_status`, `watch_xtquant_status`, `get_current_connect_sub_info`, `get_all_sub_info`, `get_authorized_market_list`, `get_server_list`, `get_available_quote_key` |
| 公式 | `bind_formula`, `create_formula`, `del_formula`, `get_formulas`, `import_formula`, `call_formula`, `subscribe_formula`, `unsubscribe_formula`, `get_formula_result`, `gen_factor_index`, `get_tabular_formula` |
| 交易时段 | `get_trading_period`, `get_kline_trading_period`, `get_all_trading_periods`, `get_all_kline_trading_periods` |
| 行情/基础资料 | `download_etf_info`, `download_his_st_data`, `download_holiday_data`, `get_his_option_list`, `get_his_option_list_batch`, `get_his_st_data`, `get_ipo_info`, `get_market_last_trade_date`, `get_markets`, `get_sector_info`, `get_instrument_detail_list`, `get_full_kline`, `get_field_list`, `get_period_list`, `get_data_dir`, `get_sec_main_contract`, `compute_coming_trading_calendar`, `generate_index_data`, `get_financial_data_ori`, `get_trading_contract_list` |
| Level2/微观结构 | `get_broker_queue_data`, `get_fullspeed_orderbook`, `get_l2thousand_queue`, `subscribe_l2thousand_queue`, `get_order_rank`, `get_hk_broker_dict`, `get_transactioncount` |
| 板块写入/刷新 | `create_sector`, `create_sector_folder`, `remove_stock_from_sector`, `reset_sector`, `reset_market_stock_list`, `reset_market_trading_day_list` |
| 可转债 | `bnd_get_amount_change`, `bnd_get_call_info`, `bnd_get_conversion_price`, `bnd_get_put_info` |
| 表格/文件 | `download_tabular_data`, `get_tabular_data`, `read_feather`, `write_feather`, `push_custom_data` |
| 其他 | `QuoteServer`, `get_wp_market_list`, `subscribe_quote2` |

原生 `xtdata` 删除的旧接口：
`init_data_dir`, `load_global_config`, `get_industry`, `get_trade_times`,
`merge_data`, `merge_data_first`, `merge_data_last`, `merge_data_max`,
`merge_data_min`, `merge_data_sum`, `MergeData`, `TimeListBuilder`。

桥保留 `get_industry` / `get_trade_times` 的旧包装以便迁移，但 QMT 2.1.33 的
`ContextInfo.get_trade_times` 实测不存在；交易时段应迁移到
`get_trading_period` 系列。大 QMT 内置进程无法连接 native quote service 时，
这组新接口会明确报不可用，而不是返回伪造结果。

## 4. 验证方法

重启策略后执行：

```python
from bigqmt_signal_trader.xtquant_compat import configure, xtdata
configure()
report = xtdata.call_method("probe_capabilities")
print(report["qmt_global_counts"])
print(report["qmt_globals"])
```

`bound` 小于 `expected` 是正常的：不同券商/权限会裁剪全局函数。关键是探针必须列出全部
55 个候选，不能因为名单遗漏把“桥没捕获”误报成“终端没有”。

## 5. 2026-10-25 实机结果

QMT 2.1.33.0 / Python 3.12.10 / xtquant 250807.1.2 / 桥 0.3.70，
pipe 部署，普通用户权限：

- 候选名单：55
- 已绑定：37
- 未绑定：18

已绑定且值得注意的新接口：
`algo_passorder`, `smart_algo_passorder`, `get_smart_algo_param`,
`cancel_task`, `pause_task`, `resume_task`, `get_basket`, `set_basket`,
`query_credit_opvolume`, `call_vba`, `ext_data*`, `get_factor_*`。

新增六项复核：

- 已绑定：`get_etf_info`, `get_etf_iopv`, `get_st_status`
- 未注入：`create_extend_data`, `reset_extend_data_stock_list`,
  `set_extend_data_value`（投研版扩展数据写权限未开放）

未绑定：
`call_formula`, `call_formula_batch`, `subscribe_formula`,
`unsubscribe_formula`, `get_formula_result`, `gen_factor_index`,
`download_history_data2`, `download_sector_data`, `download_sector_weight`,
`get_history_trade_detail_data`, `get_his_st_data`,
`is_suspended_stock`, `get_backtest_index`, `get_group_result`,
`get_vba_func_result`, `create_extend_data`,
`reset_extend_data_stock_list`, `set_extend_data_value`。

回测专用的 `order_lots` / `order_shares` / `order_target_*` / 期货开平仓函数
不在实盘捕获目标内；`set_auto_trade_callback` 等 qmtools 内部桥接函数也不作为
RPC 候选。

可用的替代通道：

- `download_history_data` 已绑定；批量下载可逐票回退。
- `ContextInfo.get_his_st_data` 和 `ContextInfo.is_suspended_stock` 存在。
- `get_backtest_index` / `get_group_result` 是回测上下文接口，实盘未注入符合预期。
- 公式族未注入，表示当前终端/权限下不能把公式 RPC 当作可用能力。

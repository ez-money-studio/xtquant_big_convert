# coding: utf-8
"""Canonical names of functions injected into a mounted QMT strategy.

QMT 2.1.33 / xtquant 250807 exposes more trading, task, formula and reference
globals than earlier terminals. Capture only identifies callable globals; it
does not expose them over RPC or bypass ``rpc_allow_order_methods``.

Keep this module dependency-free: both the mounted strategy and ``redis_rpc``
import it, including inside the single-file sandbox loader.
"""

QMT_CORE_GLOBAL_FUNCS = (
    "passorder",
    "cancel",
    "get_trade_detail_data",
)

QMT_GLOBAL_GROUPS = {
    "download": (
        "download_history_data",
        "download_history_data2",
        "down_history_data",
        "download_sector_data",
        "download_sector_weight",
    ),
    "formula": (
        "call_formula",
        "call_formula_batch",
        "subscribe_formula",
        "unsubscribe_formula",
        "get_formula_result",
        "gen_factor_index",
    ),
    "trade_query": (
        "get_history_trade_detail_data",
        "get_value_by_order_id",
        "get_last_order_id",
        "get_ipo_data",
        "get_new_purchase_limit",
    ),
    "credit": (
        "query_credit_account",
        "query_credit_opvolume",
        "get_assure_contract",
        "get_enable_short_contract",
        "get_unclosed_compacts",
        "get_closed_compacts",
        "get_debt_contract",
    ),
    "option_hk": (
        "get_option_subject_position",
        "get_comb_option",
        "get_hkt_exchange_rate",
    ),
    "algorithm_task": (
        "algo_passorder",
        "smart_algo_passorder",
        "get_smart_algo_param",
        "cancel_task",
        "pause_task",
        "resume_task",
    ),
    "basket": (
        "get_basket",
        "set_basket",
    ),
    "reference": (
        "get_etf_info",
        "get_etf_iopv",
        "get_st_status",
        "ext_data",
        "ext_data_rank",
        "ext_data_rank_range",
        "ext_data_range",
        "get_factor_value",
        "get_factor_rank",
        "call_vba",
        "get_vba_func_result",
        "is_suspended_stock",
        "get_his_st_data",
        "get_backtest_index",
        "get_group_result",
        "create_extend_data",
        "reset_extend_data_stock_list",
        "set_extend_data_value",
    ),
}


def _flatten_groups(groups):
    seen = set()
    result = []
    for names in groups.values():
        for name in names:
            if name not in seen:
                seen.add(name)
                result.append(name)
    return tuple(result)


QMT_EXTRA_GLOBAL_FUNCS = _flatten_groups(QMT_GLOBAL_GROUPS)
QMT_INJECTED_GLOBAL_FUNCS = QMT_CORE_GLOBAL_FUNCS + QMT_EXTRA_GLOBAL_FUNCS

__all__ = [
    "QMT_CORE_GLOBAL_FUNCS",
    "QMT_EXTRA_GLOBAL_FUNCS",
    "QMT_GLOBAL_GROUPS",
    "QMT_INJECTED_GLOBAL_FUNCS",
]

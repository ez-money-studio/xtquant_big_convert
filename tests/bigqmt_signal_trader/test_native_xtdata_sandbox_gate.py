"""沙箱传输下 native xtdata SDK 必须关：它一调用就拨 58610（#2026-10-14）。

实盘经过：终端的安全模块看到本机 127.0.0.1:58610 的连接就弹「非法IP」，**弹完直接
把策略停掉**。#407 把 ``get_instrument`` 改成 native-first 之后，这条路就成了一踩
就死的雷 —— 而策略自己 ``new`` provider 时没把沙箱开关传进去（默认 True），
``adapter_factory`` 那份正确的判断在单文件/策略路径上被绕过去了。

这组用例钉住两件事：判定函数说对话，以及 provider 真的不去碰 SDK。
"""

import io
import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters import market_bigqmt
from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider


def _strategy_module():
    import importlib.util

    path = os.path.join(ROOT, "src", "bigqmt_signal_trader_strategy.py")
    spec = importlib.util.spec_from_file_location("_strategy_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GateDecisionTest(unittest.TestCase):
    def setUp(self):
        self.strategy = _strategy_module()

    def test_sandbox_transports_default_to_off(self):
        for transport in ("pipe", "shm", "PIPE"):
            self.assertFalse(
                self.strategy._native_xtdata_enabled({"rpc": {"transport": transport}}),
                transport)

    def test_other_transports_default_to_on(self):
        for transport in ("redis", "zmq", "", None):
            config = {"rpc": {"transport": transport}} if transport is not None else {"rpc": {}}
            self.assertTrue(self.strategy._native_xtdata_enabled(config), transport)

    def test_explicit_setting_wins_both_ways(self):
        self.assertTrue(self.strategy._native_xtdata_enabled(
            {"rpc": {"transport": "pipe", "native_xtdata_enabled": True}}))
        self.assertFalse(self.strategy._native_xtdata_enabled(
            {"rpc": {"transport": "redis", "native_xtdata_enabled": False}}))

    def test_transport_may_also_sit_at_the_top_level(self):
        """单文件 shell 的 config 里 transport 也可能在顶层。"""
        self.assertFalse(self.strategy._native_xtdata_enabled({"transport": "pipe"}))
        self.assertTrue(self.strategy._native_xtdata_enabled({"transport": "zmq"}))


class ProviderDoesNotDialTest(unittest.TestCase):
    def test_get_instrument_skips_the_sdk_when_disabled(self):
        called = []
        saved = market_bigqmt._load_native_xtdata
        market_bigqmt._load_native_xtdata = lambda: called.append("dial") or None

        context = types.SimpleNamespace(
            get_instrumentdetail=lambda code: {"InstrumentName": "stub", "Code": code})
        try:
            provider = BigQmtMarketDataProvider(
                context, qmt_api={}, native_xtdata_enabled=False)
            data = provider.get_instrument("600519.SH")
        finally:
            market_bigqmt._load_native_xtdata = saved

        self.assertEqual(called, [], "native SDK must not be loaded on a sandbox transport")
        self.assertEqual(data.get("InstrumentName"), "stub")

    def test_strategy_wiring_passes_the_gate_everywhere(self):
        """文本级绊线：策略里每一处 provider 构造都要带上这个开关。"""
        path = os.path.join(ROOT, "src", "bigqmt_signal_trader_strategy.py")
        text = io.open(path, encoding="utf-8").read()
        constructions = text.count("BigQmtMarketDataProvider(")
        gated = text.count("native_xtdata_enabled=_native_xtdata_enabled(config)")
        self.assertGreaterEqual(constructions, 2, "expected the rpc + download-jobs call sites")
        self.assertEqual(constructions, gated)


if __name__ == "__main__":
    unittest.main()

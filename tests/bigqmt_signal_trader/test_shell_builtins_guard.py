"""单文件 shell 给内嵌模块的 builtins 必须完整（2026-10-14 实盘崩溃）。

QMT 给策略文件的 builtins 是好的，但同一环境里 ``import builtins`` 可能是精简
stub（实测那次的 stub 里没有 ``Exception``）。旧实现
``module_builtins = dict(_builtins.__dict__)`` 把这个残缺表塞给每个内嵌模块，
于是内嵌模块里第一个被触发的 ``except Exception`` 抛
``NameError: name 'Exception' is not defined`` —— 策略在 init 阶段直接死掉
（日志表象：``bridge preload failed: name 'Exception' is not defined``）。

两条用例：把 shell 的两份 builtins 合并起来（模板级），以及让策略里那个
「查全局函数名」的 helper 不再依赖 ``Exception`` 可解析（函数级）。
"""

import ast
import builtins
import io
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

STRATEGY = os.path.join(ROOT, "src", "bigqmt_signal_trader_strategy.py")
FLAT_TEMPLATE = os.path.join(ROOT, "tools", "build_no_redis_single_file_flat.py")


class StubbedBuiltinsTest(unittest.TestCase):
    """``_resolve_runtime_name`` 在精简 builtins 下也必须只是返回 None。"""

    def _function(self, builtins_ns):
        source = io.open(STRATEGY, encoding="utf-8").read()
        tree = ast.parse(source)
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_resolve_runtime_name")
        snippet = ast.get_source_segment(source, node)
        namespace = {"__builtins__": builtins_ns, "_qmt_api": {}}
        exec(compile(snippet + "\n", "<_resolve_runtime_name>", "exec"), namespace)
        return namespace["_resolve_runtime_name"]

    def test_missing_name_returns_none_without_exception(self):
        stub = dict(vars(builtins))
        for name in ("Exception", "BaseException"):
            stub.pop(name, None)

        resolve = self._function(stub)

        self.assertIsNone(resolve("get_history_trade_detail_data"))

    def test_known_name_still_resolves_from_builtins(self):
        stub = dict(vars(builtins))

        resolve = self._function(stub)

        self.assertIs(resolve("print"), print)


class TemplateMergesBuiltinsTest(unittest.TestCase):
    def test_template_merges_the_shell_builtins_with_the_imported_module(self):
        text = io.open(FLAT_TEMPLATE, encoding="utf-8").read()

        self.assertIn("def _complete_builtins():", text)
        self.assertIn("module_builtins = _complete_builtins()", text)
        self.assertNotIn("module_builtins = dict(_builtins.__dict__)", text)


if __name__ == "__main__":
    unittest.main()

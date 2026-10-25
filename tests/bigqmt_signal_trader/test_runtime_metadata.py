# coding: utf-8
"""The deployment probe must identify the QMT interpreter, not the client's."""

import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers, _runtime_metadata


class RuntimeMetadataTest(unittest.TestCase):
    def test_reports_the_running_interpreter_and_abi(self):
        info = _runtime_metadata()

        self.assertEqual(
            info["python_version"],
            ".".join(str(part) for part in sys.version_info[:3]),
        )
        self.assertEqual(
            info["profile"],
            "python%d%d" % (sys.version_info[0], sys.version_info[1]),
        )
        self.assertEqual(info["implementation"], "CPython")
        self.assertIn(info["bits"], (32, 64))
        self.assertIn("packages", info)
        self.assertIn("xtquant", info["packages"])

    def test_is_json_serializable(self):
        json.dumps(_runtime_metadata())

    def test_deployment_and_capability_responses_include_runtime(self):
        handlers = BigQmtRpcHandlers("acct", None, None)

        deployment = handlers.handle("get_deployment_info", {})
        capabilities = handlers.handle("probe_capabilities", {})

        self.assertEqual(deployment["runtime"]["python_version"],
                         _runtime_metadata()["python_version"])
        self.assertEqual(capabilities["runtime"]["profile"],
                         _runtime_metadata()["profile"])


if __name__ == "__main__":
    unittest.main()

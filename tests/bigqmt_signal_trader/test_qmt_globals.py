# coding: utf-8
"""QMT global registry stays complete and duplicate-free."""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.qmt_globals import (  # noqa: E402
    QMT_CORE_GLOBAL_FUNCS,
    QMT_EXTRA_GLOBAL_FUNCS,
    QMT_GLOBAL_GROUPS,
    QMT_INJECTED_GLOBAL_FUNCS,
)


class QmtGlobalRegistryTest(unittest.TestCase):
    def test_core_and_extra_make_the_full_capture_list(self):
        self.assertEqual(
            QMT_INJECTED_GLOBAL_FUNCS,
            QMT_CORE_GLOBAL_FUNCS + QMT_EXTRA_GLOBAL_FUNCS,
        )

    def test_groups_do_not_duplicate_names(self):
        all_names = []
        for group in QMT_GLOBAL_GROUPS.values():
            all_names.extend(group)
        self.assertEqual(len(all_names), len(set(all_names)))

    def test_250807_groups_are_present(self):
        expected = {
            "download", "formula", "trade_query", "credit", "option_hk",
            "algorithm_task", "basket", "reference",
        }
        self.assertEqual(set(QMT_GLOBAL_GROUPS), expected)


if __name__ == "__main__":
    unittest.main()

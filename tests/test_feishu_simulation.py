from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import patch

from workflows.feishu_simulation import (
    cleanup_simulation,
    simulated_work_order,
    simulation_table_environment,
    simulation_table_name,
)


class FeishuSimulationTest(unittest.TestCase):
    def test_simulation_names_and_rows_are_isolated_and_exportable(self) -> None:
        run_id = "abcd-1234-efgh-5678"
        self.assertEqual(simulation_table_name(run_id), "feishu_sim_abcd1234efgh5678")
        row = simulated_work_order(run_id, datetime(2026, 7, 23, 10, 20, 30))
        self.assertTrue(row["safetyCode"].startswith("SIM-ABCD1234"))
        self.assertEqual(row["createTime"], "2026-07-23 10:20:30")
        self.assertIn("theme", row)

    def test_cleanup_refuses_a_non_simulation_table_before_connecting(self) -> None:
        with patch("workflows.feishu_simulation.connect") as connect:
            with self.assertRaisesRegex(ValueError, "拒绝操作非飞书联调"):
                cleanup_simulation({"schema": "public", "table": "work_orders"})
        connect.assert_not_called()

    def test_cleanup_refuses_a_different_simulation_run(self) -> None:
        config = {"schema": "public", "table": "feishu_sim_abcd1234efgh5678"}
        with patch("workflows.feishu_simulation.connect") as connect:
            with self.assertRaisesRegex(ValueError, "非本次运行"):
                cleanup_simulation(config, expected_table="feishu_sim_1234567890abcdef")
        connect.assert_not_called()

    def test_simulation_table_environment_restores_existing_value_after_failure(self) -> None:
        with patch.dict(os.environ, {"PGTABLE": "formal_table"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with simulation_table_environment("feishu_sim_abcd1234efgh5678"):
                    self.assertEqual(os.environ["PGTABLE"], "feishu_sim_abcd1234efgh5678")
                    raise RuntimeError("stop")
            self.assertEqual(os.environ["PGTABLE"], "formal_table")


if __name__ == "__main__":
    unittest.main()

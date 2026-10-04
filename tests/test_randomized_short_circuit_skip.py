import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from pf_adaptive_distance_dataset.core.config import Config
from pf_adaptive_distance_dataset.core.models import (
    DatasetIntegrityError,
    DatasetStatistics,
    ExportPayload,
    ShortCircuitCalculationError,
    ShortCircuitCurrentError,
    ShortCircuitExecutionError,
)
from pf_adaptive_distance_dataset.domain import infeed
from pf_adaptive_distance_dataset.pipeline import dataset_generator as generator


class RandomizedShortCircuitSkipTest(unittest.TestCase):
    def test_short_circuit_execute_failure_is_reported_to_caller(self):
        for o_result in (2, RuntimeError("PowerFactory failed")):
            with self.subTest(result=o_result):
                o_command = SimpleNamespace()
                if isinstance(o_result, Exception):
                    o_command.Execute = lambda: (_ for _ in ()).throw(o_result)
                else:
                    o_command.Execute = lambda: o_result
                with ExitStack() as o_stack:
                    o_stack.enter_context(patch.object(
                        infeed, "get_short_circuit_command", return_value=o_command,
                    ))
                    o_stack.enter_context(patch.object(infeed, "configure_short_circuit_command"))
                    o_stack.enter_context(patch.object(
                        infeed, "get_pf_attribute",
                        side_effect=lambda _obj, s_attr, _default=None: 0 if s_attr == "ppro" else None,
                    ))
                    o_stack.enter_context(patch.object(
                        infeed, "set_pf_attribute_verified", return_value=True,
                    ))
                    self.assertFalse(infeed.execute_short_circuit_at_line_fault(
                        None, None, object(), 50.0, None,
                    ))

    def test_scenario_result_failures_are_skipped_without_partial_rows(self):
        df_states = pd.DataFrame([{"ConfigID": "config-1", "switch_1": 1}])
        o_session = SimpleNamespace(app=object(), project=object())
        d_failure = {"kind": "randomized_execution"}

        def fake_process(**d_kwargs):
            if d_failure["kind"] == "base_execution" and d_kwargs["is_base"]:
                raise ShortCircuitExecutionError("base ComShc failed")
            if (
                    (
                        d_failure["kind"] == "all_extreme_zone2"
                        and not d_kwargs["is_base"]
                    )
                    or (
                        d_failure["kind"] == "base_extreme_zone2"
                        and d_kwargs["is_base"]
                    )
            ):
                d_kwargs["scenario_yields"].extend([
                    ExportPayload(kind="flat_row", data={
                        "relay_id": "Relay_A",
                        "protected_corridor_id": "Line_A_B",
                        "base_zone2_r_reach_ohm": 3.0,
                        "base_zone2_x_reach_ohm": 4.0,
                        "target_zone2_r_reach_ohm": 6.0,
                        "target_zone2_x_reach_ohm": 8.0,
                    }),
                    ExportPayload(kind="graph_row", data={
                        "scenario_uid": d_kwargs["scenario_uid"],
                    }),
                ])
                return
            if d_kwargs["scid"] == "rand_0001":
                if d_failure["kind"] == "extreme_zone2":
                    d_kwargs["scenario_yields"].extend([
                        ExportPayload(kind="flat_row", data={
                            "relay_id": "Relay_A",
                            "protected_corridor_id": "Line_A_B",
                            "base_zone2_r_reach_ohm": 3.0,
                            "base_zone2_x_reach_ohm": 4.0,
                            "target_zone2_r_reach_ohm": 6.0,
                            "target_zone2_x_reach_ohm": 8.0,
                        }),
                        ExportPayload(kind="graph_row", data={
                            "scenario_uid": d_kwargs["scenario_uid"],
                        }),
                    ])
                    return
                d_kwargs["scenario_yields"].append(
                    ExportPayload(kind="flat_row", data={"partial": True})
                )
                if d_failure["kind"] == "randomized_execution":
                    raise ShortCircuitExecutionError("randomized ComShc failed")
                if d_failure["kind"] == "randomized_current":
                    raise ShortCircuitCurrentError("missing relay current")
                if d_failure["kind"] == "structural_calculation":
                    raise ShortCircuitCalculationError("unsupported fault location")
            d_kwargs["scenario_yields"].append(
                ExportPayload(kind="graph_row", data={"scenario_uid": d_kwargs["scenario_uid"]})
            )

        with ExitStack() as o_stack:
            o_stack.enter_context(patch.multiple(
                Config,
                INCLUDE_ORIGINAL_BASE_CASE=True,
                RANDOMIZED_SCENARIO_COUNT=2,
                PERSISTENT_ZONE2_FAILURE_MIN_RANDOMIZATIONS=2,
            ))
            o_stack.enter_context(patch.object(generator, "_get_master_case_pair", return_value=(object(), object())))
            o_stack.enter_context(patch.object(generator, "delete_existing_slave_cases"))
            o_stack.enter_context(patch.object(generator, "_validate_switch_columns", return_value=["switch_1"]))
            o_stack.enter_context(patch.object(generator, "_build_switch_state_context", return_value={
                "sid": "ss_0001", "cfg": "config-1", "sw_vec": [1],
                "closed_switch_count": 1, "open_switch_count": 0,
            }))
            o_stack.enter_context(patch.object(
                generator, "_create_and_activate_slave_for_switch_state",
                return_value=(object(), object(), object()),
            ))
            o_stack.enter_context(patch.object(
                generator, "capture_original_state_from_active_slave",
                return_value=({}, {}, {}, {}, {}, [], [], {}),
            ))
            o_stack.enter_context(patch.object(generator, "_process_single_scenario", side_effect=fake_process))
            o_cleanup = o_stack.enter_context(patch.object(generator, "_cleanup_after_scenario"))
            o_stack.enter_context(patch.object(generator, "delete_slave_case_pair"))
            o_stack.enter_context(patch.object(generator, "_reactivate_master_and_delete_slaves"))

            l_payloads = list(generator.generate_dataset_cases(
                o_session, DatasetStatistics(), df_states, ["switch_1"]
            ))
            self.assertEqual(
                [o_payload.kind for o_payload in l_payloads],
                ["graph_row", "skipped_scenario", "graph_row"],
            )
            self.assertEqual(l_payloads[1].data["scenario_uid"], "ss_0001_rand_0001")
            self.assertEqual(
                l_payloads[1].data["reason"], "short_circuit_execution_failed"
            )
            self.assertEqual(l_payloads[1].data["switch_state_row_index"], 1)
            self.assertEqual(o_cleanup.call_count, 3)

            d_failure["kind"] = "base_execution"
            l_payloads = list(generator.generate_dataset_cases(
                o_session, DatasetStatistics(), df_states, ["switch_1"]
            ))
            self.assertEqual(
                [o_payload.kind for o_payload in l_payloads],
                ["skipped_scenario", "flat_row", "graph_row", "graph_row"],
            )
            self.assertEqual(l_payloads[0].data["scenario_uid"], "ss_0001_base_0000")

            d_failure["kind"] = "randomized_current"
            l_payloads = list(generator.generate_dataset_cases(
                o_session, DatasetStatistics(), df_states, ["switch_1"]
            ))
            self.assertEqual(
                [o_payload.kind for o_payload in l_payloads],
                ["graph_row", "skipped_scenario", "graph_row"],
            )
            self.assertEqual(
                l_payloads[1].data["reason"], "short_circuit_current_invalid"
            )

            d_failure["kind"] = "extreme_zone2"
            l_payloads = list(generator.generate_dataset_cases(
                o_session, DatasetStatistics(), df_states, ["switch_1"]
            ))
            self.assertEqual(
                [o_payload.kind for o_payload in l_payloads],
                ["graph_row", "skipped_scenario", "graph_row"],
            )
            self.assertEqual(
                l_payloads[1].data["reason"],
                "extreme_zone2_target_to_base_ratio",
            )
            self.assertEqual(l_payloads[1].data["target_to_base_ratio"], 2.0)
            self.assertEqual(l_payloads[1].data["relay_id"], "Relay_A")

            d_failure["kind"] = "base_extreme_zone2"
            l_payloads = list(generator.generate_dataset_cases(
                o_session, DatasetStatistics(), df_states, ["switch_1"]
            ))
            self.assertEqual(
                [o_payload.kind for o_payload in l_payloads],
                ["skipped_scenario", "flat_row", "graph_row", "graph_row"],
            )
            self.assertEqual(
                l_payloads[0].data["scenario_uid"],
                "ss_0001_base_0000",
            )
            self.assertEqual(
                l_payloads[0].data["reason"],
                "extreme_zone2_target_to_base_ratio",
            )

            d_failure["kind"] = "all_extreme_zone2"
            l_payloads = list(generator.generate_dataset_cases(
                o_session, DatasetStatistics(), df_states, ["switch_1"]
            ))
            self.assertEqual(
                [o_payload.kind for o_payload in l_payloads],
                ["skipped_scenario", "skipped_scenario", "skipped_scenario"],
            )
            self.assertEqual(
                l_payloads[0].data["reason"],
                "persistent_switch_state_extreme_zone2",
            )
            self.assertEqual(l_payloads[0].data["evaluated_randomizations"], 2)
            self.assertEqual(l_payloads[0].data["failed_randomizations"], 2)

            d_failure["kind"] = "structural_calculation"
            with self.assertRaises(DatasetIntegrityError):
                list(generator.generate_dataset_cases(
                    o_session, DatasetStatistics(), df_states, ["switch_1"]
                ))


if __name__ == "__main__":
    unittest.main()

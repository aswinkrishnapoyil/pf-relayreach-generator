import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from pf_adaptive_distance_dataset.core.config import Config
from pf_adaptive_distance_dataset.core.models import DatasetMetadata, ExportPayload
from pf_adaptive_distance_dataset.exports.export import stream_export_and_audit
from pf_adaptive_distance_dataset.pipeline import batch_orchestrator as batch


class SingleGraphExportTest(unittest.TestCase):
    def test_recorded_base_current_skip_survives_export_and_validation(self):
        o_metadata = DatasetMetadata(
            dataset_version="test", grid_name="test", project_name="test",
            generation_timestamp="2026-09-17", random_seed_base=42,
            line_randomization_enabled=True, dg_randomization_enabled=True,
            line_length_scale_range=(0.8, 1.2), dg_capacity_scale_range=(0.8, 1.2),
            notes="PowerFactory-free skip regression test",
        )
        d_skipped = {
            "scenario_uid": "ss_0001_base_0000",
            "switch_state_row_index": 1,
            "switch_state_config_id": "config-1",
            "reason": "short_circuit_current_invalid",
            "detail": "relay and fault context",
        }
        l_payloads = [
            ExportPayload(kind="graph_row", data={
                "scenario_uid": "ss_0001_rand_0001", "switch_state_row_index": 1,
            }),
            ExportPayload(kind="skipped_scenario", data=d_skipped),
        ]
        with tempfile.TemporaryDirectory(prefix="pf_graph_skip_") as s_temp_dir:
            p_output = Path(s_temp_dir)
            with patch.multiple(Config, INCLUDE_ORIGINAL_BASE_CASE=True, RANDOMIZED_SCENARIO_COUNT=1):
                o_statistics = stream_export_and_audit(iter(l_payloads), o_metadata, p_output)
                self.assertEqual(
                    o_statistics.invalid_case_reasons["short_circuit_current_invalid"], 1
                )
                p_graph = next(p_output.glob("*.parquet"))
                self.assertEqual(pq.ParquetFile(p_graph).metadata.num_rows, 1)
                self.assertEqual(
                    json.loads((p_output / "skipped_scenarios.json").read_text(encoding="utf-8"))["scenarios"],
                    [d_skipped],
                )
                d_batch = batch._validate_batch_graph(batch.BatchRange(1, 1), p_graph)
                self.assertEqual(d_batch["skipped_scenario_count"], 1)
                self.assertEqual(
                    batch.validate_final_graph(p_graph, 1, [d_skipped])["skipped_scenario_count"], 1
                )
                with self.assertRaises(RuntimeError):
                    batch.validate_final_graph(p_graph, 1)

    def test_export_preserves_full_fields_and_terminal_nulls_across_checkpoint(self):
        o_metadata = DatasetMetadata(
            dataset_version="test", grid_name="test", project_name="test",
            generation_timestamp="2026-09-15", random_seed_base=42,
            line_randomization_enabled=True, dg_randomization_enabled=True,
            line_length_scale_range=(0.8, 1.2), dg_capacity_scale_range=(0.8, 1.2),
            notes="PowerFactory-free export regression test",
        )
        l_rows = [
            {
                "scenario_uid": f"ss_0001_rand_{i_index:04d}",
                "switch_state_row_index": 1,
                "directed_edge_relay_id": ["Relay_A", "Relay_B"],
                "zone3_applicable": [0, 1],
                "target_zone1_r_reach_ohm": [0.85, 0.85],
                "target_zone2_r_reach_ohm": [1.0, 1.5],
                "target_zone3_r_reach_ohm": [None, 2.2],
                "target_zone3_x_reach_ohm": [None, 4.4],
                "base_zone2_r_reach_ohm": [1.0, 1.4],
                "zone2_infeed_correction_r_ohm": [0.0, 0.1],
            }
            for i_index in range(51)
        ]
        with tempfile.TemporaryDirectory(prefix="pf_graph_export_") as s_temp_dir:
            p_output = Path(s_temp_dir)
            stream_export_and_audit(
                (ExportPayload(kind="graph_row", data=d_row) for d_row in l_rows),
                o_metadata,
                output_dir=p_output,
            )
            l_parquets = list(p_output.glob("*.parquet"))
            self.assertEqual(len(l_parquets), 1)
            self.assertEqual(pq.read_table(l_parquets[0]).to_pylist(), l_rows)
            self.assertEqual(len(list(p_output.glob("metadata_*.json"))), 1)
            self.assertEqual(len(list(p_output.glob("statistics_*.json"))), 1)

    def test_batches_merge_and_resume_with_only_full_graph_parquets(self):
        class FakeWorker:
            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        def fake_worker(l_command, **_d_kwargs):
            i_switch = int(l_command[l_command.index("--start-switch-state") + 1])
            p_batch_dir = Path(l_command[l_command.index("--worker-output-dir") + 1])
            (p_batch_dir / ".powerfactory_connected").touch()
            pq.write_table(
                pa.table({
                    "scenario_uid": [f"ss_{i_switch:04d}_base_0000", f"ss_{i_switch:04d}_rand_0001"],
                    "switch_state_row_index": [i_switch, i_switch],
                }),
                p_batch_dir / "distance_protection_graph_array_20260915_120000.parquet",
            )
            return FakeWorker()

        with tempfile.TemporaryDirectory(prefix="pf_graph_batches_") as s_temp_dir:
            p_output = Path(s_temp_dir)
            with patch.object(batch, "OUTPUT_DIR", p_output), patch.multiple(
                Config, INCLUDE_ORIGINAL_BASE_CASE=True, RANDOMIZED_SCENARIO_COUNT=1,
                RANDOM_SEED_BASE=42, DEBUG=True,
            ), patch.object(batch.subprocess, "Popen", side_effect=fake_worker) as o_worker:
                d_args = dict(
                    script_path=p_output / "unused_worker.py", total_switch_states=2,
                    batch_size=1, restart_pause_seconds=0, keep_batch_files=True,
                    random_seed_base=42, debug=False,
                )
                p_final = batch.run_batched_generation(**d_args)
                self.assertEqual(o_worker.call_count, 2)
                self.assertIn("--no-debug", o_worker.call_args.args[0])
                self.assertEqual(list(p_output.glob("*.parquet")), [p_final])
                self.assertEqual(pq.read_table(p_final)["switch_state_row_index"].to_pylist(), [1, 1, 2, 2])
                p_manifest = next((p_output / "_batch_tmp").glob("*/run_manifest.json"))
                d_manifest = json.loads(p_manifest.read_text(encoding="utf-8"))
                self.assertEqual(d_manifest["status"], "complete")
                self.assertEqual(d_manifest["final_outputs"]["merged_graph_rows"], 4)
                for d_entry in d_manifest["batches"].values():
                    p_batch_dir = Path(d_entry["graph_path"]).parent
                    self.assertEqual(len(list(p_batch_dir.glob("*.parquet"))), 1)
                    self.assertEqual(d_entry["graph_rows"], 2)

                o_worker.reset_mock()
                p_resumed = batch.run_batched_generation(
                    **d_args, resume_run_id=d_manifest["run_id"],
                )
                o_worker.assert_not_called()
                self.assertEqual(p_resumed, p_final)
                self.assertEqual(pq.ParquetFile(p_resumed).metadata.num_rows, 4)

    def test_batch_merge_accepts_recorded_extreme_zone2_skip(self):
        class FakeWorker:
            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        def fake_worker(l_command, **_d_kwargs):
            i_switch = int(l_command[l_command.index("--start-switch-state") + 1])
            p_batch_dir = Path(l_command[l_command.index("--worker-output-dir") + 1])
            (p_batch_dir / ".powerfactory_connected").touch()
            if i_switch == 1:
                l_uids = ["ss_0001_rand_0001"]
                (p_batch_dir / "skipped_scenarios.json").write_text(json.dumps({
                    "scenarios": [{
                        "scenario_uid": "ss_0001_base_0000",
                        "switch_state_row_index": 1,
                        "switch_state_config_id": "config-1",
                        "reason": "extreme_zone2_target_to_base_ratio",
                        "detail": "|Z2,target|/|Z2,base|=2.1 >= 2.0",
                    }],
                }), encoding="utf-8")
            else:
                l_uids = [
                    f"ss_{i_switch:04d}_base_0000",
                    f"ss_{i_switch:04d}_rand_0001",
                ]
            pq.write_table(pa.table({
                "scenario_uid": l_uids,
                "switch_state_row_index": [i_switch] * len(l_uids),
            }), p_batch_dir / "distance_protection_graph_array_20260915_120000.parquet")
            return FakeWorker()

        with tempfile.TemporaryDirectory(prefix="pf_graph_skip_batches_") as s_temp_dir:
            p_output = Path(s_temp_dir)
            with patch.object(batch, "OUTPUT_DIR", p_output), patch.multiple(
                Config, INCLUDE_ORIGINAL_BASE_CASE=True, RANDOMIZED_SCENARIO_COUNT=1,
                RANDOM_SEED_BASE=42,
            ), patch.object(batch.subprocess, "Popen", side_effect=fake_worker):
                p_final = batch.run_batched_generation(
                    script_path=p_output / "unused_worker.py", total_switch_states=2,
                    batch_size=1, restart_pause_seconds=0, keep_batch_files=False,
                    random_seed_base=42,
                )
                self.assertEqual(pq.ParquetFile(p_final).metadata.num_rows, 3)
                p_manifest = next(p_output.glob("run_manifest_*.json"))
                d_saved_manifest = json.loads(p_manifest.read_text(encoding="utf-8"))
                self.assertEqual(d_saved_manifest["status"], "complete")
                self.assertEqual(d_saved_manifest["final_outputs"]["graph_rows"], 3)
                self.assertEqual(d_saved_manifest["config_signature"]["zone2_target_to_base_ratio_limit"], 2.0)
                p_skips = next(p_output.glob("skipped_scenarios_*.json"))
                self.assertEqual(
                    json.loads(p_skips.read_text(encoding="utf-8"))["scenarios"][0]["scenario_uid"],
                    "ss_0001_base_0000",
                )
                self.assertFalse((p_output / "_batch_tmp" / p_final.stem.removeprefix(
                    "distance_protection_graph_array_"
                )).exists())

    def test_all_scenarios_of_one_state_may_be_recorded_as_skipped(self):
        l_skipped = [{
            "scenario_uid": "ss_0001_base_0000",
            "switch_state_row_index": 1,
            "switch_state_config_id": "config-1",
            "reason": "persistent_switch_state_extreme_zone2",
            "detail": "all randomized realizations exceeded the ratio limit",
        }] + [
            {
                "scenario_uid": s_uid,
                "switch_state_row_index": 1,
                "switch_state_config_id": "config-1",
                "reason": "extreme_zone2_target_to_base_ratio",
                "detail": "Zone-2 target-to-base ratio reached the limit",
            }
            for s_uid in ("ss_0001_rand_0001", "ss_0001_rand_0002")
        ]
        with tempfile.TemporaryDirectory(prefix="pf_graph_all_skipped_") as s_temp_dir:
            p_output = Path(s_temp_dir)
            o_metadata = DatasetMetadata(
                dataset_version="test", grid_name="test", project_name="test",
                generation_timestamp="2026-09-19", random_seed_base=42,
                line_randomization_enabled=True, dg_randomization_enabled=True,
                line_length_scale_range=(0.8, 1.2), dg_capacity_scale_range=(0.8, 1.2),
                notes="PowerFactory-free all-skipped regression test",
            )
            with patch.multiple(
                Config, INCLUDE_ORIGINAL_BASE_CASE=True, RANDOMIZED_SCENARIO_COUNT=2,
            ):
                stream_export_and_audit(
                    iter(ExportPayload(kind="skipped_scenario", data=d) for d in l_skipped),
                    o_metadata,
                    p_output,
                )
                p_graph = next(p_output.glob("*.parquet"))
                self.assertEqual(pq.ParquetFile(p_graph).metadata.num_rows, 0)
                d_batch = batch._validate_batch_graph(batch.BatchRange(1, 1), p_graph)
                self.assertEqual(d_batch["skipped_scenario_count"], 3)
                d_final = batch.validate_final_graph(p_graph, 1, l_skipped)
                self.assertEqual(d_final["switch_state_count"], 1)
                self.assertEqual(d_final["graph_switch_state_count"], 0)

    def test_single_graph_checks_reject_incomplete_duplicate_and_wrong_switch_rows(self):
        with tempfile.TemporaryDirectory(prefix="pf_graph_validation_") as s_temp_dir:
            p_graph = Path(s_temp_dir) / "distance_protection_graph_array_20260915_120000.parquet"
            with patch.multiple(Config, INCLUDE_ORIGINAL_BASE_CASE=True, RANDOMIZED_SCENARIO_COUNT=1):
                l_invalid_cases = [
                    (["a"], [1]),
                    (["a", "a"], [1, 1]),
                    (["a", "b"], [2, 2]),
                ]
                for l_uids, l_switches in l_invalid_cases:
                    with self.subTest(uids=l_uids, switches=l_switches):
                        pq.write_table(pa.table({
                            "scenario_uid": l_uids,
                            "switch_state_row_index": l_switches,
                        }), p_graph)
                        with self.assertRaises(RuntimeError):
                            batch._validate_batch_graph(batch.BatchRange(1, 1), p_graph)
                        with self.assertRaises(RuntimeError):
                            batch.validate_final_graph(p_graph, 1)


if __name__ == "__main__":
    unittest.main()

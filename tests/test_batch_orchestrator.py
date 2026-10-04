import os
import subprocess
import tempfile
import unittest
from inspect import signature
from pathlib import Path
from unittest.mock import patch

from main_script import main
from pf_adaptive_distance_dataset.core.config import Config
from pf_adaptive_distance_dataset.pipeline import batch_orchestrator as batch
from pf_adaptive_distance_dataset.pipeline.batch_orchestrator import (
    BatchRange,
    build_batch_ranges,
    _new_manifest,
    _validate_resume_manifest,
)


class BatchOrchestratorTest(unittest.TestCase):
    def test_resume_detects_protection_policy_changes(self):
        d_manifest = _new_manifest("test", 1, 1, 42)
        d_policy = {
            "TERMINAL_ZONE2_REACH_FACTOR": "terminal_zone2_reach_factor",
            "ZONE2_TARGET_TO_BASE_RATIO_LIMIT": "zone2_target_to_base_ratio_limit",
            "PERSISTENT_ZONE2_FAILURE_MIN_RANDOMIZATIONS": (
                "persistent_zone2_failure_min_randomizations"
            ),
        }
        for s_attribute, s_key in d_policy.items():
            with self.subTest(setting=s_attribute):
                self.assertEqual(d_manifest["config_signature"][s_key], getattr(Config, s_attribute))
                with patch.object(Config, s_attribute, getattr(Config, s_attribute) + 1):
                    with self.assertRaisesRegex(RuntimeError, "configuration changed"):
                        _validate_resume_manifest(d_manifest, 1, 42)

    def test_resume_rejects_paths_before_loading_any_manifest(self):
        for s_run_id in ("../outside", "..", "C:\\outside", "/outside", "20261004_171334/child"):
            with self.subTest(run_id=s_run_id), patch.object(batch, "_load_manifest") as o_load:
                with self.assertRaisesRegex(ValueError, "timestamp ID"):
                    batch.run_batched_generation(Path("unused.py"), 1, resume_run_id=s_run_id)
                o_load.assert_not_called()

    def test_recursive_cleanup_refuses_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as s_temp_dir:
            p_root = Path(s_temp_dir)
            p_expected = p_root / "expected"
            p_unrelated = p_root / "unrelated"
            p_expected.mkdir()
            p_unrelated.mkdir()
            with self.assertRaisesRegex(ValueError, "Refusing cleanup"):
                batch._remove_directory_within(p_unrelated, p_expected)
            self.assertTrue(p_unrelated.is_dir())

    def test_normal_run_defaults_come_from_config(self):
        d_config_names = {
            "debug": "DEBUG",
            "batch_size": "BATCH_SIZE",
            "restart_pause_seconds": "RESTART_PAUSE_SECONDS",
            "keep_batch_files": "KEEP_BATCH_FILES",
            "resume_run_id": "RESUME_RUN_ID",
            "worker_startup_timeout_seconds": "WORKER_STARTUP_TIMEOUT_SECONDS",
            "worker_startup_retries": "WORKER_STARTUP_RETRIES",
        }
        for o_function in (main, batch.run_batched_generation):
            for s_parameter, s_config_name in d_config_names.items():
                with self.subTest(function=o_function.__name__, setting=s_parameter):
                    self.assertEqual(
                        signature(o_function).parameters[s_parameter].default,
                        getattr(Config, s_config_name),
                    )

    def test_resume_rejects_batches_from_before_integrity_checks(self):
        d_manifest = _new_manifest("test", 1, 1, 42)
        _validate_resume_manifest(d_manifest, 1, 42)
        d_manifest["config_signature"].pop("integrity_contract_version")
        with self.assertRaisesRegex(RuntimeError, "configuration changed"):
            _validate_resume_manifest(d_manifest, 1, 42)
        d_manifest["config_signature"]["integrity_contract_version"] = 1
        with self.assertRaisesRegex(RuntimeError, "configuration changed"):
            _validate_resume_manifest(d_manifest, 1, 42)

    def test_build_batch_ranges_covers_all_states_once(self):
        ranges = build_batch_ranges(total_switch_states=86, batch_size=5)

        self.assertEqual(ranges[0], BatchRange(1, 5))
        self.assertEqual(ranges[-1], BatchRange(86, 86))
        self.assertEqual(len(ranges), 18)

        covered = [
            state
            for batch in ranges
            for state in range(batch.start, batch.end + 1)
        ]
        self.assertEqual(covered, list(range(1, 87)))

    def test_invalid_batch_size_is_rejected(self):
        with self.assertRaises(ValueError):
            build_batch_ranges(total_switch_states=86, batch_size=0)

    def test_batch_labels_are_stable(self):
        self.assertEqual(BatchRange(6, 10).label, "batch_0006_0010")

    def test_timed_out_startup_is_stopped_and_partial_batch_is_retried(self):
        class FakeWorker:
            pid = 12345

            def __init__(self, running):
                self.running = running

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return None if self.running else 0

        with tempfile.TemporaryDirectory() as s_temp_dir:
            p_batch_dir = Path(s_temp_dir) / "batch_0001_0001"
            p_batch_dir.mkdir()
            (p_batch_dir / "partial.txt").write_text("partial", encoding="utf-8")

            l_workers = [FakeWorker(True), FakeWorker(False)]

            def fake_launch(*_l_args, **_d_kwargs):
                o_worker = l_workers.pop(0)
                if not o_worker.running:
                    (p_batch_dir / ".powerfactory_connected").touch()
                return o_worker

            with patch.object(
                batch.subprocess, "Popen",
                side_effect=fake_launch,
            ) as o_launch, patch.object(
                batch, "_terminate_worker_tree"
            ) as o_stop, patch.object(
                batch.time, "monotonic", side_effect=[0.0, 1.0, 2.0]
            ), patch.object(batch.time, "sleep") as o_pause:
                batch._run_worker_with_retries(
                    ["python", "worker.py"], p_batch_dir, Path(s_temp_dir),
                    {}, 1.0, 1, 60.0,
                )
            self.assertEqual(o_launch.call_count, 2)
            o_stop.assert_called_once()
            o_pause.assert_called_once_with(60.0)
            self.assertFalse((p_batch_dir / "partial.txt").exists())
            self.assertTrue((p_batch_dir / ".powerfactory_start").exists())

    def test_connected_worker_is_not_force_stopped_during_generation(self):
        class FakeWorker:
            def wait(self, timeout=None):
                self.assert_no_timeout = timeout is None
                self.start_marker_seen = (p_batch_dir / ".powerfactory_start").exists()
                return 0

        o_worker = FakeWorker()
        with tempfile.TemporaryDirectory() as s_temp_dir:
            p_batch_dir = Path(s_temp_dir) / "batch_0001_0001"
            p_batch_dir.mkdir()

            def fake_launch(*_l_args, **_d_kwargs):
                (p_batch_dir / ".powerfactory_connected").touch()
                return o_worker

            with patch.object(
                batch.subprocess, "Popen", side_effect=fake_launch,
            ), patch.object(batch, "_terminate_worker_tree") as o_stop:
                batch._run_worker_with_retries(
                    ["python", "worker.py"], p_batch_dir, Path(s_temp_dir),
                    {}, 0.001, 0, 60.0,
                )
            self.assertTrue(o_worker.assert_no_timeout)
            self.assertTrue(o_worker.start_marker_seen)
            o_stop.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows taskkill is required")
    def test_timeout_cleanup_targets_entire_windows_worker_tree(self):
        class FakeWorker:
            pid = 12345

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 4294967295

        with patch.object(
            batch.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as o_taskkill:
            batch._terminate_worker_tree(FakeWorker())
        self.assertEqual(
            o_taskkill.call_args.args[0],
            ["taskkill", "/PID", "12345", "/T", "/F"],
        )


if __name__ == "__main__":
    unittest.main()

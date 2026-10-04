# batch_orchestrator.py
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time

import pyarrow as pa
import pyarrow.parquet as pq

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core.config import Config, OUTPUT_DIR, SWITCH_STATE_FILE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchRange:
    start: int
    end: int

    @property
    def label(self) -> str:
        return f"batch_{self.start:04d}_{self.end:04d}"

    @property
    def switch_state_count(self) -> int:
        return self.end - self.start + 1


def build_batch_ranges(total_switch_states: int, batch_size: int) -> list[BatchRange]:
    if total_switch_states <= 0:
        raise ValueError("total_switch_states must be greater than zero.")
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero.")

    return [
        BatchRange(start=i_start, end=min(i_start + batch_size - 1, total_switch_states))
        for i_start in range(1, total_switch_states + 1, batch_size)
    ]


def expected_scenarios_per_switch_state() -> int:
    return Config.RANDOMIZED_SCENARIO_COUNT + int(Config.INCLUDE_ORIGINAL_BASE_CASE)


def expected_graph_rows(batch_range: BatchRange) -> int:
    return batch_range.switch_state_count * expected_scenarios_per_switch_state()


def _sha256_file(path: Path) -> str:
    o_hash = hashlib.sha256()
    with path.open("rb") as o_file:
        for b_chunk in iter(lambda: o_file.read(1024 * 1024), b""):
            o_hash.update(b_chunk)
    return o_hash.hexdigest()


def _config_signature() -> dict:
    return {
        "dataset_version": Config.DATASET_VERSION,
        "integrity_contract_version": 3,
        "project_name": Config.PROJECT_NAME,
        "grid_name": Config.GRID_NAME,
        "master_study_case_name": Config.MASTER_STUDY_CASE_NAME,
        "master_operation_scenario_name": Config.MASTER_OPERATION_SCENARIO_NAME,
        "switch_state_scenarios_enabled": Config.ENABLE_SWITCH_STATE_SCENARIOS,
        "include_original_base_case": Config.INCLUDE_ORIGINAL_BASE_CASE,
        "randomized_scenario_count": Config.RANDOMIZED_SCENARIO_COUNT,
        "max_switch_state_config_count": Config.MAX_SWITCH_STATE_CONFIG_COUNT,
        "dg_capacity_random_seed_offset": Config.DG_CAPACITY_RANDOM_SEED_OFFSET,
        "dataset_export_type": Config.DATASET_EXPORT_TYPE,
        "line_randomization_enabled": Config.ENABLE_LINE_RANDOMIZATION,
        "dg_randomization_enabled": Config.ENABLE_DG_CAPACITY_RANDOMIZATION,
        "line_length_scale_min": Config.LINE_LENGTH_SCALE_MIN,
        "line_length_scale_max": Config.LINE_LENGTH_SCALE_MAX,
        "line_rx_ratio_randomization_enabled": (
            Config.ENABLE_LINE_RX_RATIO_RANDOMIZATION
        ),
        "line_rx_scale_min": Config.LINE_RX_SCALE_MIN,
        "line_rx_scale_max": Config.LINE_RX_SCALE_MAX,
        "line_rx_ratio_max": Config.LINE_RX_RATIO_MAX,
        "dg_capacity_scale_min": Config.DG_CAPACITY_SCALE_MIN,
        "dg_capacity_scale_max": Config.DG_CAPACITY_SCALE_MAX,
        "reach_gf": Config.REACH_GF,
        "terminal_zone2_reach_factor": Config.TERMINAL_ZONE2_REACH_FACTOR,
        "zone3_reach_factor": Config.ZONE3_REACH_FACTOR,
        "zone2_target_to_base_ratio_limit": Config.ZONE2_TARGET_TO_BASE_RATIO_LIMIT,
        "persistent_zone2_failure_min_randomizations": (
            Config.PERSISTENT_ZONE2_FAILURE_MIN_RANDOMIZATIONS
        ),
        "zero_tolerance": Config.ZERO_TOLERANCE,
    }


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    p_partial = path.with_suffix(path.suffix + ".partial")
    with p_partial.open("w", encoding="utf-8") as o_file:
        json.dump(data, o_file, indent=2, default=str)
    os.replace(p_partial, path)


def _load_manifest(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as o_file:
        return json.load(o_file)


def _find_batch_graph_parquet(batch_dir: Path) -> Path:
    l_graph = sorted(
        batch_dir.glob("distance_protection_graph_array_[0-9]*.parquet")
    )

    if len(l_graph) != 1:
        raise RuntimeError(
            f"Expected exactly one graph parquet in {batch_dir}; "
            f"found graph={len(l_graph)}."
        )

    return l_graph[0]


def _parquet_row_count(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def _expected_scenario_ids(start: int, end: int) -> dict[str, int]:
    d_expected = {}
    for i_switch_state in range(start, end + 1):
        s_prefix = f"ss_{i_switch_state:04d}_"
        if Config.INCLUDE_ORIGINAL_BASE_CASE:
            d_expected[s_prefix + "base_0000"] = i_switch_state
        for i_scenario in range(1, Config.RANDOMIZED_SCENARIO_COUNT + 1):
            d_expected[s_prefix + f"rand_{i_scenario:04d}"] = i_switch_state
    return d_expected


def _read_skipped_scenarios(batch_dir: Path) -> list[dict]:
    p_skipped = batch_dir / "skipped_scenarios.json"
    if not p_skipped.is_file():
        return []
    d_report = _load_manifest(p_skipped)
    l_skipped = d_report.get("scenarios")
    if not isinstance(l_skipped, list):
        raise RuntimeError(f"Invalid skipped-scenario report: {p_skipped}")
    return l_skipped


def _validate_scenario_coverage(
        expected: dict[str, int],
        graph_uids: list[str],
        graph_indices: list[int],
        skipped: list[dict],
) -> None:
    if len(set(graph_uids)) != len(graph_uids):
        raise RuntimeError("Duplicate scenario_uid values in graph parquet.")
    for s_uid, i_index in zip(graph_uids, graph_indices):
        if expected.get(s_uid) != i_index:
            raise RuntimeError(f"Wrong or unexpected graph scenario: {s_uid}")

    l_skipped_uids = []
    s_allowed_skip_reasons = {
        "short_circuit_execution_failed",
        "short_circuit_current_invalid",
        "extreme_zone2_target_to_base_ratio",
        "persistent_switch_state_extreme_zone2",
    }
    for d_skipped in skipped:
        if not isinstance(d_skipped, dict):
            raise RuntimeError(f"Invalid skipped-scenario record: {d_skipped}")
        s_uid = d_skipped.get("scenario_uid")
        if (
                not isinstance(s_uid, str)
                or s_uid not in expected
                or expected.get(s_uid) != d_skipped.get("switch_state_row_index")
                or d_skipped.get("reason") not in s_allowed_skip_reasons
                or not d_skipped.get("switch_state_config_id")
        ):
            raise RuntimeError(f"Invalid skipped-scenario record: {d_skipped}")
        l_skipped_uids.append(s_uid)

    if len(set(l_skipped_uids)) != len(l_skipped_uids):
        raise RuntimeError("Duplicate skipped-scenario records.")
    if set(graph_uids) & set(l_skipped_uids):
        raise RuntimeError("A scenario is both exported and marked skipped.")
    if set(graph_uids) | set(l_skipped_uids) != set(expected):
        raise RuntimeError("Graph and recorded skips do not cover the expected scenarios.")


def _validate_batch_graph(
        batch_range: BatchRange,
        graph_path: Path,
) -> dict:
    if not graph_path.exists() or graph_path.stat().st_size == 0:
        raise RuntimeError(f"Missing or empty graph parquet: {graph_path}")

    i_graph_rows = _parquet_row_count(graph_path)
    l_graph_uids, l_graph_switch_indices = _read_identity_columns(graph_path)
    l_skipped = _read_skipped_scenarios(graph_path.parent)
    _validate_scenario_coverage(
        _expected_scenario_ids(batch_range.start, batch_range.end),
        l_graph_uids,
        l_graph_switch_indices,
        l_skipped,
    )

    s_expected_switch_indices = set(range(batch_range.start, batch_range.end + 1))
    s_covered_switch_indices = set(l_graph_switch_indices) | {
        int(d_skipped["switch_state_row_index"])
        for d_skipped in l_skipped
    }
    if s_covered_switch_indices != s_expected_switch_indices:
        raise RuntimeError(
            f"{batch_range.label} contains incorrect global switch-state indices: "
            f"expected {sorted(s_expected_switch_indices)}, "
            f"got {sorted(s_covered_switch_indices)}."
        )
    return {
        "graph_path": str(graph_path),
        "graph_rows": i_graph_rows,
        "skipped_scenarios": l_skipped,
        "skipped_scenario_count": len(l_skipped),
    }


def _align_table_to_schema(table: Any, target_schema: Any) -> Any:

    d_columns = {s_name: table[s_name] for s_name in table.column_names}
    l_arrays = []

    for o_field in target_schema:
        if o_field.name not in d_columns:
            o_array = pa.nulls(table.num_rows, type=o_field.type)
        else:
            o_array = d_columns[o_field.name]
            if o_array.type != o_field.type:
                o_array = o_array.cast(o_field.type, safe=False)
        l_arrays.append(o_array)

    return pa.table(
        {o_field.name: l_arrays[i] for i, o_field in enumerate(target_schema)},
        schema=target_schema,
    )


def merge_parquet_files(paths: Iterable[Path], output_path: Path) -> int:
    l_paths = [Path(p_path) for p_path in paths]
    if not l_paths:
        raise RuntimeError("No parquet files were supplied for merging.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    p_partial = output_path.with_suffix(output_path.suffix + ".partial")
    if p_partial.exists():
        p_partial.unlink()

    l_schemas = [
        pq.ParquetFile(p_input).schema_arrow.remove_metadata()
        for p_input in l_paths
    ]

    try:
        o_unified_schema = pa.unify_schemas(
            l_schemas,
            promote_options="permissive",
        )
    except Exception as o_error:
        raise RuntimeError(
            "Could not derive a common parquet schema for batch merge."
        ) from o_error

    o_writer: Optional[Any] = None
    i_total_rows = 0

    try:
        o_writer = pq.ParquetWriter(
            p_partial,
            o_unified_schema,
            compression="snappy",
        )

        for p_input in l_paths:
            o_parquet = pq.ParquetFile(p_input)
            for i_row_group in range(o_parquet.num_row_groups):
                o_table = o_parquet.read_row_group(i_row_group)
                o_aligned_table = _align_table_to_schema(
                    o_table,
                    o_unified_schema,
                )
                o_writer.write_table(o_aligned_table)
                i_total_rows += o_aligned_table.num_rows
    except Exception:
        if o_writer is not None:
            try:
                o_writer.close()
            except Exception:
                pass
        if p_partial.exists():
            p_partial.unlink()
        raise
    else:
        if o_writer is not None:
            o_writer.close()
        os.replace(p_partial, output_path)

    return i_total_rows


def _read_identity_columns(path: Path) -> tuple[list[str], list[int]]:

    o_parquet = pq.ParquetFile(path)
    l_available = set(o_parquet.schema_arrow.names)
    l_required = ["scenario_uid", "switch_state_row_index"]
    l_missing = [s_name for s_name in l_required if s_name not in l_available]
    if l_missing:
        raise RuntimeError(
            f"Final parquet is missing validation columns {l_missing}: {path}"
        )

    l_scenario_uids: list[str] = []
    l_switch_indices: list[int] = []
    for i_row_group in range(o_parquet.num_row_groups):
        o_table = o_parquet.read_row_group(i_row_group, columns=l_required)
        l_scenario_uids.extend(str(v) for v in o_table["scenario_uid"].to_pylist())
        l_switch_indices.extend(int(v) for v in o_table["switch_state_row_index"].to_pylist())

    return l_scenario_uids, l_switch_indices


def validate_final_graph(
        graph_path: Path,
        total_switch_states: int,
        skipped_scenarios: Optional[list[dict]] = None,
) -> dict:
    i_expected_rows = total_switch_states * expected_scenarios_per_switch_state()
    i_graph_rows = _parquet_row_count(graph_path)
    l_skipped = skipped_scenarios or []

    if i_graph_rows + len(l_skipped) != i_expected_rows:
        raise RuntimeError(
            "Final parquet row-count validation failed: "
            f"expected={i_expected_rows}, graph={i_graph_rows}, "
            f"recorded_skips={len(l_skipped)}."
        )

    l_uids, l_switch_indices = _read_identity_columns(graph_path)
    _validate_scenario_coverage(
        _expected_scenario_ids(1, total_switch_states),
        l_uids,
        l_switch_indices,
        l_skipped,
    )

    s_expected_indices = set(range(1, total_switch_states + 1))
    s_graph_indices = set(l_switch_indices)
    s_actual_indices = s_graph_indices | {
        int(d_skipped["switch_state_row_index"])
        for d_skipped in l_skipped
    }
    if s_actual_indices != s_expected_indices:
        l_missing = sorted(s_expected_indices - s_actual_indices)
        l_extra = sorted(s_actual_indices - s_expected_indices)
        raise RuntimeError(
            "Switch-state coverage validation failed. "
            f"Missing indices={l_missing[:10]}, extra indices={l_extra[:10]}."
        )

    return {
        "expected_rows": i_expected_rows,
        "graph_rows": i_graph_rows,
        "unique_scenario_uids": len(set(l_uids)),
        "switch_state_count": len(s_actual_indices),
        "graph_switch_state_count": len(s_graph_indices),
        "skipped_scenario_count": len(l_skipped),
    }


def _new_manifest(
        run_id: str,
        batch_size: int,
        total_switch_states: int,
        random_seed_base: int,
) -> dict:
    return {
        "manifest_version": 1,
        "run_id": run_id,
        "status": "running",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "batch_size": batch_size,
        "total_switch_states": total_switch_states,
        "expected_scenario_rows": total_switch_states * expected_scenarios_per_switch_state(),
        "random_seed_base": random_seed_base,
        "switch_state_file": str(SWITCH_STATE_FILE),
        "switch_state_sha256": _sha256_file(SWITCH_STATE_FILE),
        "config_signature": _config_signature(),
        "batches": {},
        "final_outputs": {},
    }


def _validate_resume_manifest(
        manifest: dict,
        total_switch_states: int,
        random_seed_base: int,
) -> None:
    if manifest.get("total_switch_states") != total_switch_states:
        raise RuntimeError("Cannot resume: switch-state count changed.")
    if manifest.get("random_seed_base") != random_seed_base:
        raise RuntimeError("Cannot resume: random seed base changed.")
    if manifest.get("switch_state_sha256") != _sha256_file(SWITCH_STATE_FILE):
        raise RuntimeError("Cannot resume: switch_states.csv changed.")
    if manifest.get("config_signature") != _config_signature():
        raise RuntimeError("Cannot resume: generation configuration changed.")


def _terminate_worker_tree(o_process: subprocess.Popen) -> None:
    if o_process.poll() is not None:
        return

    if os.name == "nt":
        o_result = subprocess.run(
            ["taskkill", "/PID", str(o_process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if o_result.returncode != 0 and o_process.poll() is None:
            raise RuntimeError(
                f"Could not stop worker process tree {o_process.pid}: "
                f"{o_result.stderr.strip()}"
            )
    else:
        os.killpg(o_process.pid, signal.SIGKILL)

    try:
        o_process.wait(timeout=30)
    except subprocess.TimeoutExpired as o_error:
        raise RuntimeError(
            f"Worker process tree {o_process.pid} did not stop."
        ) from o_error


def _remove_directory_within(p_directory: Path, p_parent: Path) -> None:
    """Refuse recursive cleanup outside the intended immediate parent."""
    if p_directory.resolve().parent != p_parent.resolve():
        raise ValueError(f"Refusing cleanup outside {p_parent}: {p_directory}")
    shutil.rmtree(p_directory)


def _run_worker_with_retries(
        l_command: list[str],
        p_batch_dir: Path,
        p_workdir: Path,
        d_environment: dict[str, str],
        f_startup_timeout_seconds: float,
        i_retries: int,
        f_retry_pause_seconds: float,
) -> None:
    for i_attempt in range(i_retries + 1):
        if i_attempt:
            _remove_directory_within(p_batch_dir, p_batch_dir.parent)
            p_batch_dir.mkdir(parents=True, exist_ok=True)
            logger.warning(
                "Retrying %s (%d/%d).",
                p_batch_dir.name,
                i_attempt + 1,
                i_retries + 1,
            )

        o_process = subprocess.Popen(
            l_command,
            cwd=str(p_workdir),
            env=d_environment,
            start_new_session=os.name != "nt",
        )
        p_connected_marker = p_batch_dir / ".powerfactory_connected"
        f_deadline = time.monotonic() + f_startup_timeout_seconds
        while (
                not p_connected_marker.exists()
                and o_process.poll() is None
                and time.monotonic() < f_deadline
        ):
            time.sleep(0.25)

        if p_connected_marker.exists():
            # Never force-stop a worker after it starts modifying PowerFactory.
            (p_batch_dir / ".powerfactory_start").touch()
            i_exit_code = o_process.wait()
        else:
            i_exit_code = o_process.poll()
            if i_exit_code is None:
                logger.error(
                    "%s did not connect to PowerFactory within %.0f seconds; "
                    "stopping process tree %d.",
                    p_batch_dir.name,
                    f_startup_timeout_seconds,
                    o_process.pid,
                )
                _terminate_worker_tree(o_process)
                if i_attempt == i_retries:
                    raise RuntimeError(
                        f"{p_batch_dir.name} startup timed out on all "
                        f"{i_retries + 1} attempts."
                    )
                time.sleep(f_retry_pause_seconds)
                continue

        if i_exit_code != 0:
            raise RuntimeError(
                f"{p_batch_dir.name} worker failed with exit code {i_exit_code}."
            )
        if not p_connected_marker.exists():
            raise RuntimeError(
                f"{p_batch_dir.name} exited without a PowerFactory connection marker."
            )
        return


def run_batched_generation(
        script_path: Path,
        total_switch_states: int,
        batch_size: int = Config.BATCH_SIZE,
        restart_pause_seconds: float = Config.RESTART_PAUSE_SECONDS,
        debug: bool = Config.DEBUG,
        keep_batch_files: bool = Config.KEEP_BATCH_FILES,
        resume_run_id: Optional[str] = Config.RESUME_RUN_ID,
        random_seed_base: Optional[int] = None,
        worker_startup_timeout_seconds: float = Config.WORKER_STARTUP_TIMEOUT_SECONDS,
        worker_startup_retries: int = Config.WORKER_STARTUP_RETRIES,
) -> Path:
    if worker_startup_timeout_seconds <= 0:
        raise ValueError("worker_startup_timeout_seconds must be greater than zero.")
    if worker_startup_retries < 0:
        raise ValueError("worker_startup_retries cannot be negative.")

    if resume_run_id:
        if re.fullmatch(r"[0-9]{8}_[0-9]{6}(?:_[0-9]{2,})?", resume_run_id) is None:
            raise ValueError("resume_run_id must be a generated timestamp ID, not a path.")
        s_run_id = resume_run_id
        p_run_dir = OUTPUT_DIR / "_batch_tmp" / s_run_id
        if p_run_dir.resolve().parent != (OUTPUT_DIR / "_batch_tmp").resolve():
            raise ValueError("Resume directory is outside Results/_batch_tmp.")
        p_manifest_path = p_run_dir / "run_manifest.json"
        if not p_manifest_path.exists():
            raise RuntimeError(f"Resume manifest not found: {p_manifest_path}")

        d_manifest = _load_manifest(p_manifest_path)
        i_manifest_seed = int(d_manifest["random_seed_base"])
        if random_seed_base is not None and int(random_seed_base) != i_manifest_seed:
            raise RuntimeError(
                "Cannot resume: requested random seed differs from the manifest seed."
            )
        random_seed_base = i_manifest_seed
        Config.RANDOM_SEED_BASE = random_seed_base
        _validate_resume_manifest(
            d_manifest,
            total_switch_states=total_switch_states,
            random_seed_base=random_seed_base,
        )
        batch_size = int(d_manifest["batch_size"])
        logger.info("Resuming batched run: %s", s_run_id)
    else:
        if random_seed_base is None:
            random_seed_base = Config.get_random_seed_base()
        Config.RANDOM_SEED_BASE = int(random_seed_base)

        s_base_run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        s_run_id = s_base_run_id
        i_suffix = 1
        while (OUTPUT_DIR / "_batch_tmp" / s_run_id).exists():
            s_run_id = f"{s_base_run_id}_{i_suffix:02d}"
            i_suffix += 1

        p_run_dir = OUTPUT_DIR / "_batch_tmp" / s_run_id
        p_manifest_path = p_run_dir / "run_manifest.json"
        p_run_dir.mkdir(parents=True, exist_ok=True)
        d_manifest = _new_manifest(
            run_id=s_run_id,
            batch_size=batch_size,
            total_switch_states=total_switch_states,
            random_seed_base=int(random_seed_base),
        )
        _atomic_write_json(p_manifest_path, d_manifest)

    assert random_seed_base is not None

    l_batch_ranges = build_batch_ranges(total_switch_states, batch_size)
    l_graph_paths: list[Path] = []
    l_skipped_scenarios: list[dict] = []

    logger.info(
        "Starting batched generation: %d switch states, batch size=%d, batches=%d, run_id=%s",
        total_switch_states,
        batch_size,
        len(l_batch_ranges),
        s_run_id,
    )

    try:
        for i_batch_number, o_batch in enumerate(l_batch_ranges, start=1):
            p_batch_dir = p_run_dir / o_batch.label
            d_existing = d_manifest.get("batches", {}).get(o_batch.label, {})

            if d_existing.get("status") == "complete":
                try:
                    p_batch_graph = Path(d_existing["graph_path"])
                    d_batch_validation = _validate_batch_graph(o_batch, p_batch_graph)
                    d_existing.update(d_batch_validation)
                    logger.info(
                        "Skipping completed %s (%d/%d).",
                        o_batch.label,
                        i_batch_number,
                        len(l_batch_ranges),
                    )
                    l_graph_paths.append(p_batch_graph)
                    l_skipped_scenarios.extend(d_batch_validation["skipped_scenarios"])
                    continue
                except Exception:
                    logger.warning(
                        "Completed manifest entry for %s is invalid; rerunning batch.",
                        o_batch.label,
                        exc_info=True,
                    )

            if p_batch_dir.exists():
                _remove_directory_within(p_batch_dir, p_run_dir)
            p_batch_dir.mkdir(parents=True, exist_ok=True)

            d_manifest["batches"][o_batch.label] = {
                "start": o_batch.start,
                "end": o_batch.end,
                "status": "running",
                "started_at": datetime.now().isoformat(),
            }
            d_manifest["status"] = "running"
            d_manifest["updated_at"] = datetime.now().isoformat()
            _atomic_write_json(p_manifest_path, d_manifest)

            l_command = [
                sys.executable,
                str(script_path),
                "--worker",
                "--start-switch-state",
                str(o_batch.start),
                "--end-switch-state",
                str(o_batch.end),
                "--worker-output-dir",
                str(p_batch_dir),
                "--random-seed-base",
                str(random_seed_base),
            ]
            l_command.append("--debug" if debug else "--no-debug")

            logger.info(
                "Launching %s (%d/%d): switch states %d-%d",
                o_batch.label,
                i_batch_number,
                len(l_batch_ranges),
                o_batch.start,
                o_batch.end,
            )

            d_environment = os.environ.copy()
            d_environment["PYTHONUNBUFFERED"] = "1"

            _run_worker_with_retries(
                l_command,
                p_batch_dir=p_batch_dir,
                p_workdir=script_path.parent,
                d_environment=d_environment,
                f_startup_timeout_seconds=worker_startup_timeout_seconds,
                i_retries=worker_startup_retries,
                f_retry_pause_seconds=max(
                    Config.WORKER_STARTUP_RETRY_PAUSE_SECONDS,
                    restart_pause_seconds,
                ),
            )

            p_batch_graph = _find_batch_graph_parquet(p_batch_dir)
            d_batch_validation = _validate_batch_graph(o_batch, p_batch_graph)
            l_graph_paths.append(p_batch_graph)
            l_skipped_scenarios.extend(d_batch_validation["skipped_scenarios"])

            d_manifest["batches"][o_batch.label].update(
                {
                    "status": "complete",
                    "completed_at": datetime.now().isoformat(),
                    **d_batch_validation,
                }
            )
            d_manifest["updated_at"] = datetime.now().isoformat()
            _atomic_write_json(p_manifest_path, d_manifest)

            logger.info(
                "%s completed and validated. Worker exited; PowerFactory memory can be reclaimed.",
                o_batch.label,
            )

            if i_batch_number < len(l_batch_ranges) and restart_pause_seconds > 0:
                logger.info(
                    "Pausing %.1f seconds before starting a fresh PowerFactory worker.",
                    restart_pause_seconds,
                )
                time.sleep(restart_pause_seconds)

        p_final_graph = OUTPUT_DIR / f"distance_protection_graph_array_{s_run_id}.parquet"

        i_graph_rows = merge_parquet_files(l_graph_paths, p_final_graph)
        d_final_validation = validate_final_graph(
            graph_path=p_final_graph,
            total_switch_states=total_switch_states,
            skipped_scenarios=l_skipped_scenarios,
        )

        if l_skipped_scenarios:
            p_final_skips = OUTPUT_DIR / f"skipped_scenarios_{s_run_id}.json"
            _atomic_write_json(p_final_skips, {"scenarios": l_skipped_scenarios})
            d_final_validation["skipped_scenarios_path"] = str(p_final_skips)
            logger.warning(
                "Skipped %d scenarios under the configured omission policies; "
                "review %s before using the dataset.",
                len(l_skipped_scenarios),
                p_final_skips,
            )

        d_manifest["status"] = "complete"
        d_manifest["completed_at"] = datetime.now().isoformat()
        d_manifest["updated_at"] = datetime.now().isoformat()
        d_manifest["final_outputs"] = {
            "graph_path": str(p_final_graph),
            "merged_graph_rows": i_graph_rows,
            **d_final_validation,
        }
        _atomic_write_json(p_manifest_path, d_manifest)

        # Keep the completed provenance record even when temporary batches go.
        _atomic_write_json(OUTPUT_DIR / f"run_manifest_{s_run_id}.json", d_manifest)
        if not keep_batch_files:
            _remove_directory_within(p_run_dir, OUTPUT_DIR / "_batch_tmp")

        logger.info("Batched generation complete.")
        logger.info("Final graph parquet: %s", p_final_graph)
        return p_final_graph

    except Exception as o_error:
        d_manifest["status"] = "failed"
        d_manifest["updated_at"] = datetime.now().isoformat()
        d_manifest["last_error"] = str(o_error)
        _atomic_write_json(p_manifest_path, d_manifest)
        logger.error(
            "Batched run failed. Temporary files were retained for resume: %s",
            p_run_dir,
        )
        logger.error(
            "Resume with: python main_script.py --resume-run %s",
            s_run_id,
        )
        raise

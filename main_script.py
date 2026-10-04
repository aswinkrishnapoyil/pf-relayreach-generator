# main_script.py
from __future__ import annotations

import argparse
import logging
import sys
import time
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from pf_adaptive_distance_dataset.core.config import Config, LOGS_DIR
from pf_adaptive_distance_dataset.core.models import DatasetMetadata, DatasetStatistics
from pf_adaptive_distance_dataset.exports.export import stream_export_and_audit
from pf_adaptive_distance_dataset.pf_api.pf_session import PowerFactorySession
from pf_adaptive_distance_dataset.pipeline.batch_orchestrator import run_batched_generation
from pf_adaptive_distance_dataset.pipeline.dataset_generator import generate_dataset_cases
from pf_adaptive_distance_dataset.pipeline.switch_states import load_switch_state_dataframe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(
            LOGS_DIR / "pipeline.log",
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)


def _run_with_terminal_transcript() -> int:
    """
    Re-launch main_script.py once and tee the complete stdout/stderr
    stream from the main process and all inherited worker processes
    to both the terminal and a timestamped text file.
    """

    s_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    p_log_file = (
            LOGS_DIR
            / f"dataset_generator_terminal_{s_timestamp}.txt"
    )

    d_environment = os.environ.copy()

    # Prevent recursive re-launching.
    d_environment["DATASET_TRANSCRIPT_ACTIVE"] = "1"

    # Ensure immediate, consistently encoded worker output.
    d_environment["PYTHONUNBUFFERED"] = "1"
    d_environment["PYTHONIOENCODING"] = "utf-8"

    l_command = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        *sys.argv[1:],
    ]

    print(f"Complete terminal log: {p_log_file}", flush=True)

    with p_log_file.open(
            "w",
            encoding="utf-8",
            buffering=1,
    ) as o_log:

        o_process = subprocess.Popen(
            l_command,
            cwd=str(Path(__file__).resolve().parent),
            env=d_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        assert o_process.stdout is not None

        try:
            for s_line in o_process.stdout:
                sys.stdout.write(s_line)
                sys.stdout.flush()

                o_log.write(s_line)
                o_log.flush()

        except KeyboardInterrupt:
            o_process.terminate()
            o_process.wait()

            s_message = (
                "\nDataset generation interrupted by user.\n"
            )

            sys.stdout.write(s_message)
            o_log.write(s_message)

            return 130

        i_exit_code = o_process.wait()

        s_summary = (
                "\n"
                + "=" * 80
                + "\n"
                + f"Dataset-generator exit code: {i_exit_code}\n"
                + f"Complete terminal log: {p_log_file}\n"
                + "=" * 80
                + "\n"
        )

        sys.stdout.write(s_summary)
        sys.stdout.flush()

        o_log.write(s_summary)
        o_log.flush()

    return i_exit_code


# ======================================================================================================================
# ------------------------------------------------ Pipeline Execution --------------------------------------------------
# ======================================================================================================================
def _validate_randomization_configuration() -> None:
    if Config.RANDOMIZED_SCENARIO_COUNT > 0 and (
            not Config.ENABLE_LINE_RANDOMIZATION
            and not Config.ENABLE_DG_CAPACITY_RANDOMIZATION
    ):
        logger.warning(
            "RANDOMIZED_SCENARIO_COUNT=%d but both randomization switches are disabled. "
            "Aborting in 10 seconds; fix config.py.",
            Config.RANDOMIZED_SCENARIO_COUNT,
        )
        time.sleep(10)
        raise RuntimeError(
            "Aborted: randomized scenarios are enabled but no randomization is active."
        )


def run_generation_worker(
        start_switch_state: int,
        end_switch_state: int,
        output_dir: Path,
        random_seed_base: int,
        debug: bool = Config.DEBUG,
) -> None:
    """Runs one isolated switch-state batch in one Python/PowerFactory process."""

    Config.RANDOM_SEED_BASE = int(random_seed_base)
    Config.get_random_seed_base()
    _validate_randomization_configuration()

    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Worker debug mode enabled: PowerFactory GUI will be shown.")

    df_switch_states, l_switch_columns = load_switch_state_dataframe()
    i_total_switch_state_count = len(df_switch_states)

    if i_total_switch_state_count == 0:
        raise RuntimeError("No switch-state configurations were loaded.")
    if start_switch_state < 1:
        raise ValueError("start-switch-state must be at least 1.")
    if end_switch_state < start_switch_state:
        raise ValueError("end-switch-state must be greater than or equal to start-switch-state.")
    if end_switch_state > i_total_switch_state_count:
        raise ValueError(
            f"end-switch-state {end_switch_state} exceeds loaded count {i_total_switch_state_count}."
        )

    df_batch = df_switch_states.iloc[start_switch_state - 1:end_switch_state].copy()
    i_row_offset = start_switch_state - 1

    logger.info(
        "Worker loaded %d switch states; processing global range %d-%d (%d rows).",
        i_total_switch_state_count,
        start_switch_state,
        end_switch_state,
        len(df_batch),
    )
    logger.info("Opening PowerFactory session for isolated worker batch...")

    o_generation_statistics = DatasetStatistics()

    with PowerFactorySession(
            Config.PROJECT_NAME,
            Config.GRID_NAME,
            debug=debug,
    ) as o_session:
        (output_dir / ".powerfactory_connected").touch()
        p_start_marker = output_dir / ".powerfactory_start"
        f_start_deadline = time.monotonic() + Config.WORKER_START_MARKER_TIMEOUT_SECONDS
        while not p_start_marker.exists():
            if time.monotonic() >= f_start_deadline:
                raise RuntimeError("Timed out waiting for the batch supervisor to start generation.")
            time.sleep(0.1)
        g_data_generator = generate_dataset_cases(
            session=o_session,
            stats=o_generation_statistics,
            df_chunk=df_batch,
            sw_cols=l_switch_columns,
            row_offset=i_row_offset,
        )

        o_dataset_metadata = DatasetMetadata(
            dataset_version=Config.DATASET_VERSION,
            grid_name=Config.GRID_NAME,
            project_name=Config.PROJECT_NAME,
            generation_timestamp=datetime.now().isoformat(),
            random_seed_base=Config.get_random_seed_base(),
            line_randomization_enabled=Config.ENABLE_LINE_RANDOMIZATION,
            dg_randomization_enabled=Config.ENABLE_DG_CAPACITY_RANDOMIZATION,
            line_length_scale_range=(
                Config.LINE_LENGTH_SCALE_MIN,
                Config.LINE_LENGTH_SCALE_MAX,
            ),
            dg_capacity_scale_range=(
                Config.DG_CAPACITY_SCALE_MIN,
                Config.DG_CAPACITY_SCALE_MAX,
            ),
            notes=(
                f"Isolated batch worker for switch states {start_switch_state}-{end_switch_state}. "
                "Global switch indices and deterministic seeds are preserved via row_offset."
            ),
        )

        o_export_statistics = stream_export_and_audit(
            payloads=g_data_generator,
            metadata=o_dataset_metadata,
            output_dir=output_dir,
        )

    for s_invalid_reason, i_reason_count in o_generation_statistics.invalid_case_reasons.items():
        o_export_statistics.invalid_case_reasons[s_invalid_reason] = (
                o_export_statistics.invalid_case_reasons.get(s_invalid_reason, 0)
                + i_reason_count
        )

    logger.info(
        "Worker batch complete. Valid rows: %d/%d",
        o_export_statistics.total_cases_valid,
        o_export_statistics.total_cases_generated,
    )
    if o_export_statistics.invalid_case_reasons:
        logger.warning(
            "Worker invalid/generation issue summary: %s",
            o_export_statistics.invalid_case_reasons,
        )


def main(
        debug: bool = Config.DEBUG,
        batch_size: int = Config.BATCH_SIZE,
        restart_pause_seconds: float = Config.RESTART_PAUSE_SECONDS,
        keep_batch_files: bool = Config.KEEP_BATCH_FILES,
        resume_run_id: Optional[str] = Config.RESUME_RUN_ID,
        worker_startup_timeout_seconds: float = Config.WORKER_STARTUP_TIMEOUT_SECONDS,
        worker_startup_retries: int = Config.WORKER_STARTUP_RETRIES,
) -> None:
    """Runs the full dataset in isolated PowerFactory subprocess batches."""

    _validate_randomization_configuration()

    if resume_run_id is None:
        i_random_seed_base: Optional[int] = Config.get_random_seed_base()
    else:
        # The original seed is loaded from the retained run manifest. This is
        # required when Config.RANDOM_SEED_BASE=None and the initial run failed.
        i_random_seed_base = None

    df_switch_states, _ = load_switch_state_dataframe()
    i_total_switch_state_count = len(df_switch_states)
    if i_total_switch_state_count == 0:
        raise RuntimeError("No switch-state configurations were loaded.")

    logger.info(
        "Loaded %d switch-state configurations. PowerFactory will restart every %d states.",
        i_total_switch_state_count,
        batch_size,
    )

    run_batched_generation(
        script_path=Path(__file__).resolve(),
        total_switch_states=i_total_switch_state_count,
        batch_size=batch_size,
        restart_pause_seconds=restart_pause_seconds,
        debug=debug,
        keep_batch_files=keep_batch_files,
        resume_run_id=resume_run_id,
        random_seed_base=i_random_seed_base,
        worker_startup_timeout_seconds=worker_startup_timeout_seconds,
        worker_startup_retries=worker_startup_retries,
    )


if __name__ == "__main__":

    if os.environ.get("DATASET_TRANSCRIPT_ACTIVE") != "1":
        raise SystemExit(
            _run_with_terminal_transcript()
        )

    o_parser = argparse.ArgumentParser()
    o_parser.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=Config.DEBUG,
        help=f"Show PowerFactory GUI and verbose logs (config default: {Config.DEBUG}).",
    )
    o_parser.add_argument(
        "--batch-size",
        type=int,
        default=Config.BATCH_SIZE,
        help=f"Switch states per fresh PowerFactory worker (config default: {Config.BATCH_SIZE}).",
    )
    o_parser.add_argument(
        "--restart-pause-seconds",
        type=float,
        default=Config.RESTART_PAUSE_SECONDS,
        help=f"Pause between workers in seconds (config default: {Config.RESTART_PAUSE_SECONDS}).",
    )
    o_parser.add_argument(
        "--worker-startup-timeout-seconds",
        type=float,
        default=Config.WORKER_STARTUP_TIMEOUT_SECONDS,
        help=("Maximum seconds to connect PowerFactory before retrying "
              f"(config default: {Config.WORKER_STARTUP_TIMEOUT_SECONDS})."),
    )
    o_parser.add_argument(
        "--worker-startup-retries",
        type=int,
        default=Config.WORKER_STARTUP_RETRIES,
        help=("Additional attempts after a PowerFactory startup timeout "
              f"(config default: {Config.WORKER_STARTUP_RETRIES})."),
    )
    o_parser.add_argument(
        "--resume-run",
        type=str,
        default=Config.RESUME_RUN_ID,
        help=("Resume an incomplete run using its run ID from Results/_batch_tmp/ "
              f"(config default: {Config.RESUME_RUN_ID})."),
    )
    o_parser.add_argument(
        "--keep-batch-files",
        action=argparse.BooleanOptionalAction,
        default=Config.KEEP_BATCH_FILES,
        help=(
            "Keep temporary batch outputs after a successful final merge "
            f"(default from Config.KEEP_BATCH_FILES={Config.KEEP_BATCH_FILES})."
        ),
    )

    # Internal worker arguments. They are intentionally available for diagnostics,
    # but normal full runs should not set --worker directly.
    o_parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    o_parser.add_argument("--start-switch-state", type=int, default=None, help=argparse.SUPPRESS)
    o_parser.add_argument("--end-switch-state", type=int, default=None, help=argparse.SUPPRESS)
    o_parser.add_argument("--worker-output-dir", type=Path, default=None, help=argparse.SUPPRESS)
    o_parser.add_argument("--random-seed-base", type=int, default=None, help=argparse.SUPPRESS)

    o_args = o_parser.parse_args()

    if o_args.worker:
        if o_args.start_switch_state is None or o_args.end_switch_state is None:
            o_parser.error("--worker requires --start-switch-state and --end-switch-state.")
        if o_args.worker_output_dir is None:
            o_parser.error("--worker requires --worker-output-dir.")
        if o_args.random_seed_base is None:
            o_parser.error("--worker requires --random-seed-base.")

        run_generation_worker(
            start_switch_state=o_args.start_switch_state,
            end_switch_state=o_args.end_switch_state,
            output_dir=o_args.worker_output_dir,
            random_seed_base=o_args.random_seed_base,
            debug=o_args.debug,
        )
    else:
        main(
            debug=o_args.debug,
            batch_size=o_args.batch_size,
            restart_pause_seconds=o_args.restart_pause_seconds,
            keep_batch_files=o_args.keep_batch_files,
            resume_run_id=o_args.resume_run,
            worker_startup_timeout_seconds=o_args.worker_startup_timeout_seconds,
            worker_startup_retries=o_args.worker_startup_retries,
        )

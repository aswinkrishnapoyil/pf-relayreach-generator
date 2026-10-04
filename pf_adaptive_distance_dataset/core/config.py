# config.py
from __future__ import annotations

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = SCRIPT_DIR

RESULTS_DIR = PROJECT_ROOT / "Results"
OUTPUT_DIR = RESULTS_DIR
SWITCH_STATE_DIR = SCRIPT_DIR / "switch_state"
SWITCH_STATE_FILE = SWITCH_STATE_DIR / "switch_states.csv"
LOGS_DIR = SCRIPT_DIR / "logs"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SWITCH_STATE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


class Config:
    # Local connection settings may override these in powerfactory.local.json.
    PF_PYTHON_PATH = r"C:\Program Files\DIgSILENT\PowerFactory 2023 SP3\Python\3.9"
    PROJECT_NAME = r"\your_user\YourProject"
    GRID_NAME = "YourGrid.ElmNet"

    MASTER_STUDY_CASE_NAME = "SC_Master"
    MASTER_OPERATION_SCENARIO_NAME = "OS_Master"

    SLAVE_STUDY_CASE_PREFIX = "SC_Slave"
    SLAVE_OPERATION_SCENARIO_PREFIX = "OS_Slave"

    DATASET_VERSION = "v3.3.2"
    DATASET_EXPORT_TYPE = "streaming_graph_array_with_metadata"

    # --- Normal Run Controls (used when main_script.py is launched directly) ---
    DEBUG = False  # True shows the PowerFactory GUI and enables verbose logs.
    BATCH_SIZE = 1  # Switch states per fresh PowerFactory worker.
    RESTART_PAUSE_SECONDS = 15.0
    WORKER_STARTUP_TIMEOUT_SECONDS = 60.0
    WORKER_STARTUP_RETRIES = 2
    WORKER_STARTUP_RETRY_PAUSE_SECONDS = 60.0
    WORKER_START_MARKER_TIMEOUT_SECONDS = 60.0
    KEEP_BATCH_FILES = True
    RESUME_RUN_ID: Optional[str] = None  # Reset to None after resuming a run.

    # --- Grading Factor for Zone Reach ---
    REACH_GF = 0.85
    TERMINAL_ZONE2_REACH_FACTOR = 1.20
    ZONE3_REACH_FACTOR = 1.20
    ZONE2_TARGET_TO_BASE_RATIO_LIMIT = 2.0
    PERSISTENT_ZONE2_FAILURE_MIN_RANDOMIZATIONS = 25
    ZERO_TOLERANCE = 1e-12

    # --- Switch State Controls ---
    ENABLE_SWITCH_STATE_SCENARIOS = True
    MAX_SWITCH_STATE_CONFIG_COUNT = None

    # --- Base Case & Randomization Volume ---
    INCLUDE_ORIGINAL_BASE_CASE = True
    RANDOMIZED_SCENARIO_COUNT = 25

    # --- Line Parameter Randomization ---
    ENABLE_LINE_RANDOMIZATION = True
    LINE_LENGTH_SCALE_MIN = 0.8
    LINE_LENGTH_SCALE_MAX = 1.2
    ENABLE_LINE_RX_RATIO_RANDOMIZATION = True
    LINE_RX_SCALE_MIN = 0.8
    LINE_RX_SCALE_MAX = 1.2
    LINE_RX_RATIO_MAX = 0.60

    # --- Distributed Generation (DG) Randomization ---
    ENABLE_DG_CAPACITY_RANDOMIZATION = True
    DG_CAPACITY_SCALE_MIN = 0.8
    DG_CAPACITY_SCALE_MAX = 1.2

    RANDOM_SEED_BASE: Optional[int] = 20260915  # Set as None to get auto generated seed base
    DG_CAPACITY_RANDOM_SEED_OFFSET = 100000

    @classmethod
    def get_random_seed_base(cls) -> int:
        """Auto-generates a reproducible unique seed if one is not hardcoded."""
        if cls.RANDOM_SEED_BASE is None:
            timestamp = int(datetime.now().timestamp() * 1000) % 1000000
            seed = timestamp + (os.getpid() % 100000)
            cls.RANDOM_SEED_BASE = seed
            logging.getLogger(__name__).info(
                f"Auto-generated RANDOM_SEED_BASE: {cls.RANDOM_SEED_BASE}"
            )
        else:
            logging.getLogger(__name__).info(
                f"Using hardcoded RANDOM_SEED_BASE: {cls.RANDOM_SEED_BASE}"
            )
        return cls.RANDOM_SEED_BASE


def _load_local_powerfactory_settings(
        p_settings_file: Path = PROJECT_ROOT / "powerfactory.local.json",
) -> None:
    """Load only local connection settings; generation policy stays in config.py."""
    if not p_settings_file.is_file():
        return

    with p_settings_file.open("r", encoding="utf-8-sig") as o_file:
        d_settings = json.load(o_file)
    set_allowed = {
        "PF_PYTHON_PATH", "PROJECT_NAME", "GRID_NAME",
        "MASTER_STUDY_CASE_NAME", "MASTER_OPERATION_SCENARIO_NAME",
    }
    if not isinstance(d_settings, dict) or set(d_settings) - set_allowed:
        raise ValueError(f"Unknown local PowerFactory settings in {p_settings_file}")
    if any(not isinstance(s_value, str) or not s_value.strip()
           for s_value in d_settings.values()):
        raise ValueError(f"Local PowerFactory settings must be nonempty strings: {p_settings_file}")
    for s_name, s_value in d_settings.items():
        setattr(Config, s_name, s_value)


_load_local_powerfactory_settings()


class PFAttr:
    BUS1 = "bus1"
    BUS2 = "bus2"
    BUS3 = "bus3"

    BUSHV = "bushv"
    BUSMV = "busmv"
    BUSLV = "buslv"

    CTERM = "cterm"
    OUTSERV = "outserv"

    LINE_R = "R1"
    LINE_X = "X1"
    LINE_LENGTH = "dline"

    LINE_TYPE = "typ_id"
    LINE_TYPE_R_CANDIDATES = ["rline", "R1", "r1"]
    LINE_TYPE_X_CANDIDATES = ["xline", "X1", "x1"]

    SWITCH_STATE = "on_off"
    TERMINAL_USAGE = "iUsage"

    IKSS = "m:Ikss"
    IKSS_BUS1 = "m:Ikss:bus1"
    IKSS_BUS2 = "m:Ikss:bus2"

    DG_CAPACITY_CANDIDATES = ["sgn", "Sn", "snom", "Srated"]

    CUBICLE_ATTR_CANDIDATES = [
        BUS1,
        BUS2,
        BUS3,
        BUSHV,
        BUSMV,
        BUSLV,
    ]

    IKSS_CANDIDATES = [
        IKSS,
        IKSS_BUS1,
        IKSS_BUS2,
    ]

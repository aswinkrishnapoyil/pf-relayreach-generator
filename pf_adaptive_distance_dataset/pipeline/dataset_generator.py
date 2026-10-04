# dataset_generator.py
from __future__ import annotations

import hashlib
import logging
from typing import Any, Generator

import pandas as pd

from ..core.config import Config
from ..core.models import (
    DatasetStatistics,
    ExportPayload,
    DatasetIntegrityError,
    PowerFactoryStateError,
    ShortCircuitScenarioError,
    ShortCircuitExecutionError,
)

from ..pf_api.pf_utils import get_safe_name

from ..pf_api.pf_session import (
    PowerFactorySession,
    get_required_project_object_by_loc_name,
)

from ..pf_api.slave_cases import (
    delete_existing_slave_cases,
    create_slave_case_pair,
    activate_slave_case_pair,
    delete_slave_case_pair,
)

from .randomization import (
    apply_random_line_parameter_scenario,
    apply_random_dg_capacity_scenario,
)

from ..pf_api.grid_state import restore_grid_state

from ..graph.graph_arrays import convert_scenario_to_graph_row

from .switch_states import (
    apply_switch_state,
    apply_outserv_for_components_behind_open_switches,
)

from .case_features import get_corridor_scenario_rows

from ..pf_api.state_capture import (
    capture_original_state_from_active_slave,
    capture_bus_attributes,
)

logger = logging.getLogger(__name__)


def _increment_invalid_reason(stats: DatasetStatistics, reason: str) -> None:
    """
    Increments one invalid/generation issue reason in DatasetStatistics.
    """

    o_statistics = stats
    s_reason = reason

    o_statistics.invalid_case_reasons[s_reason] = (
            o_statistics.invalid_case_reasons.get(s_reason, 0) + 1
    )


def _find_extreme_zone2_target(
        scenario_payloads: list[ExportPayload],
) -> dict | None:
    """Returns the first relay whose corrected Zone 2 reaches the limit."""

    for o_payload in scenario_payloads:
        if o_payload.kind != "flat_row":
            continue

        d_row = o_payload.data
        c_base = complex(
            float(d_row.get("base_zone2_r_reach_ohm", 0.0)),
            float(d_row.get("base_zone2_x_reach_ohm", 0.0)),
        )
        f_base_magnitude = abs(c_base)
        if f_base_magnitude <= Config.ZERO_TOLERANCE:
            continue

        c_target = complex(
            float(d_row.get("target_zone2_r_reach_ohm", 0.0)),
            float(d_row.get("target_zone2_x_reach_ohm", 0.0)),
        )
        f_target_magnitude = abs(c_target)
        f_ratio = f_target_magnitude / f_base_magnitude

        if f_ratio >= Config.ZONE2_TARGET_TO_BASE_RATIO_LIMIT:
            return {
                "relay_id": d_row.get("relay_id", ""),
                "protected_corridor_id": d_row.get(
                    "protected_corridor_id",
                    "",
                ),
                "base_zone2_magnitude_ohm": f_base_magnitude,
                "target_zone2_magnitude_ohm": f_target_magnitude,
                "target_to_base_ratio": f_ratio,
            }

    return None


def _get_master_case_pair(project):
    """
    Gets the configured master study case and master operation scenario.
    """

    o_project = project

    o_master_study_case = get_required_project_object_by_loc_name(
        project=o_project,
        loc_name=Config.MASTER_STUDY_CASE_NAME,
        class_name="IntCase",
        label="master study case",
    )

    o_master_operation_scenario = get_required_project_object_by_loc_name(
        project=o_project,
        loc_name=Config.MASTER_OPERATION_SCENARIO_NAME,
        class_name="IntScenario",
        label="master operation scenario",
    )

    logger.info(
        f"Using master study case: {get_safe_name(o_master_study_case)}"
    )

    logger.info(
        "Using master operation scenario: "
        f"{get_safe_name(o_master_operation_scenario)}"
    )

    return o_master_study_case, o_master_operation_scenario


def _validate_switch_columns(df_chunk: pd.DataFrame, sw_cols: list) -> list:
    """
    Validates that all switch-state columns exist in the DataFrame chunk.
    """

    df_switch_state_chunk = df_chunk
    l_switch_columns = list(sw_cols)

    l_missing_switch_columns = [
        s_column
        for s_column in l_switch_columns
        if s_column not in df_switch_state_chunk.columns
    ]

    if l_missing_switch_columns:
        raise ValueError(
            f"df_chunk is missing {len(l_missing_switch_columns)} switch columns. "
            f"First missing columns: {l_missing_switch_columns[:5]}"
        )

    return l_switch_columns


def _build_switch_state_context(
        row: Any,
        sw_cols: list,
        global_idx: int,
        local_pos: int,
        total_rows: int,
) -> dict:
    """
    Builds switch-state identifiers, switch vector, and switch counts.
    """

    d_switch_state_row = row
    l_switch_columns = sw_cols
    i_global_row_index = global_idx
    i_local_row_position = local_pos
    i_total_rows = total_rows

    s_switch_state_short_id = f"ss_{i_global_row_index + 1:04d}"

    raw_config_id = d_switch_state_row.get(
        "ConfigID",
        s_switch_state_short_id,
    )

    s_switch_state_config_id = (
        s_switch_state_short_id
        if pd.isna(raw_config_id)
        else str(raw_config_id)
    )

    logger.info(
        f">>> Switch State {i_global_row_index + 1} "
        f"(row {i_local_row_position + 1}/{i_total_rows}): "
        f"{s_switch_state_config_id}"
    )

    l_switch_status_vector = [
        int(d_switch_state_row[s_column])
        for s_column in l_switch_columns
    ] if l_switch_columns else []

    i_closed_switch_count = (
        sum(l_switch_status_vector)
        if l_switch_status_vector
        else 0
    )

    i_open_switch_count = (
        len(l_switch_status_vector) - i_closed_switch_count
        if l_switch_status_vector
        else 0
    )

    return {
        "sid": s_switch_state_short_id,
        "cfg": s_switch_state_config_id,
        "sw_vec": l_switch_status_vector,
        "closed_switch_count": i_closed_switch_count,
        "open_switch_count": i_open_switch_count,
    }


def _create_and_activate_slave_for_switch_state(
        session: PowerFactorySession,
        project,
        master_study_case,
        master_operation_scenario,
        sid: str,
):
    """
    Creates and activates one slave study-case/scenario pair for a switch state.
    """

    o_session = session
    o_project = project
    o_master_study_case = master_study_case
    o_master_operation_scenario = master_operation_scenario
    s_switch_state_short_id = sid

    o_slave_study_case, o_slave_operation_scenario = create_slave_case_pair(
        master_study_case=o_master_study_case,
        master_operation_scenario=o_master_operation_scenario,
        slave_suffix=s_switch_state_short_id,
    )

    activate_slave_case_pair(
        o_slave_study_case,
        o_slave_operation_scenario,
    )

    l_grid_objects = o_project.GetContents(
        Config.GRID_NAME,
        1,
    )

    if not l_grid_objects:
        raise RuntimeError(
            f"Grid not found after slave activation: {Config.GRID_NAME}"
        )

    o_grid = l_grid_objects[0]
    o_session.grid = o_grid

    logger.info(
        "Activated slave case/scenario for switch state "
        f"{s_switch_state_short_id}."
    )

    return o_slave_study_case, o_slave_operation_scenario, o_grid


def _iter_scenarios():
    """
    Iterates over the base scenario and configured randomized scenarios.
    """

    for i_randomized_scenario_index in range(
            -1,
            Config.RANDOMIZED_SCENARIO_COUNT,
    ):
        b_is_base_case = i_randomized_scenario_index == -1

        if b_is_base_case and not Config.INCLUDE_ORIGINAL_BASE_CASE:
            continue

        s_scenario_id = (
            "base_0000"
            if b_is_base_case
            else f"rand_{i_randomized_scenario_index + 1:04d}"
        )

        yield (
            i_randomized_scenario_index,
            b_is_base_case,
            s_scenario_id,
        )


def _restore_and_apply_switch_state(
        row: Any,
        cfg: str,
        cub_lookup: dict,
        out_objs: list,
        orig_lines: dict,
        orig_dgs: dict,
        orig_out: dict,
        orig_sw: dict,
) -> None:
    """
    Restores the slave grid baseline and applies the selected switch state.
    """

    d_switch_state_row = row
    s_switch_state_config_id = cfg
    d_cubicle_lookup = cub_lookup
    l_outserv_controlled_objects = out_objs

    d_original_line_states = orig_lines
    d_original_dg_states = orig_dgs
    d_original_outserv_states = orig_out
    d_original_switch_states = orig_sw

    restore_grid_state(
        orig_lines=d_original_line_states,
        orig_dgs=d_original_dg_states,
        orig_out=d_original_outserv_states,
        orig_sw=d_original_switch_states,
    )

    apply_switch_state(
        row=d_switch_state_row,
        lookup=d_cubicle_lookup,
        cfg=s_switch_state_config_id,
    )

    apply_outserv_for_components_behind_open_switches(
        objs=l_outserv_controlled_objects,
        orig_states=d_original_outserv_states,
    )


def _get_scenario_seeds(
        is_base: bool,
        sc: int,
        global_idx: int,
) -> tuple[int, int]:
    """
    Returns line and DG randomization seeds for one scenario.
    """

    b_is_base_case = is_base
    i_randomized_scenario_index = sc
    i_global_row_index = global_idx

    if b_is_base_case:
        return -1, -1

    _s_seed_key = f"{Config.get_random_seed_base()}_{i_global_row_index}_{i_randomized_scenario_index}".encode()
    i_line_random_seed = int(hashlib.md5(_s_seed_key).hexdigest(), 16) % (2 ** 31)

    i_dg_random_seed = (
            i_line_random_seed
            + Config.DG_CAPACITY_RANDOM_SEED_OFFSET
    )

    return i_line_random_seed, i_dg_random_seed


def _tag_randomization_logs(
        logs: list[dict],
        sid: str,
        cfg: str,
        scenario_uid: str,
) -> None:
    """
    Adds switch-state and scenario identifiers to randomization log rows.
    """

    l_randomization_logs = logs
    s_switch_state_short_id = sid
    s_switch_state_config_id = cfg
    s_scenario_uid = scenario_uid

    for d_log_row in l_randomization_logs:
        d_log_row.update(
            {
                "switch_state_short_id": s_switch_state_short_id,
                "switch_state_config_id": s_switch_state_config_id,
                "scenario_uid": s_scenario_uid,
            }
        )


def _apply_randomization_for_scenario(
        is_base: bool,
        scid: str,
        sid: str,
        cfg: str,
        scenario_uid: str,
        line_seed: int,
        dg_seed: int,
        orig_lines: dict,
        orig_dgs: dict,
) -> tuple[list[dict], list[dict]]:
    """
    Applies line-length-based line randomization and DG-capacity randomization for one non-base scenario.
    """

    b_is_base_case = is_base
    s_scenario_id = scid
    s_switch_state_short_id = sid
    s_switch_state_config_id = cfg
    s_scenario_uid = scenario_uid

    i_line_random_seed = line_seed
    i_dg_random_seed = dg_seed

    d_original_line_states = orig_lines
    d_original_dg_states = orig_dgs

    l_line_randomization_logs = []
    l_dg_randomization_logs = []

    if not b_is_base_case and Config.ENABLE_LINE_RANDOMIZATION:
        l_line_randomization_logs = apply_random_line_parameter_scenario(
            d_original_line_states,
            s_scenario_id,
            i_line_random_seed,
            Config.LINE_LENGTH_SCALE_MIN,
            Config.LINE_LENGTH_SCALE_MAX,
        )

        _tag_randomization_logs(
            logs=l_line_randomization_logs,
            sid=s_switch_state_short_id,
            cfg=s_switch_state_config_id,
            scenario_uid=s_scenario_uid,
        )

    if not b_is_base_case and Config.ENABLE_DG_CAPACITY_RANDOMIZATION:
        l_dg_randomization_logs = apply_random_dg_capacity_scenario(
            d_original_dg_states,
            s_scenario_id,
            i_dg_random_seed,
            Config.DG_CAPACITY_SCALE_MIN,
            Config.DG_CAPACITY_SCALE_MAX,
        )

        _tag_randomization_logs(
            logs=l_dg_randomization_logs,
            sid=s_switch_state_short_id,
            cfg=s_switch_state_config_id,
            scenario_uid=s_scenario_uid,
        )

    return l_line_randomization_logs, l_dg_randomization_logs


def _build_scenario_metadata(
        sid: str,
        cfg: str,
        global_idx: int,
        open_switch_count: int,
        closed_switch_count: int,
        scid: str,
        scenario_uid: str,
        is_base: bool,
        line_seed: int,
        dg_seed: int,
) -> dict:
    """
    Builds scenario metadata attached to each flat row and graph row.
    """

    s_switch_state_short_id = sid
    s_switch_state_config_id = cfg
    i_global_row_index = global_idx

    i_open_switch_count = open_switch_count
    i_closed_switch_count = closed_switch_count

    s_scenario_id = scid
    s_scenario_uid = scenario_uid

    b_is_base_case = is_base

    i_line_random_seed = line_seed
    i_dg_random_seed = dg_seed

    b_line_randomization_active = (
            not b_is_base_case
            and Config.ENABLE_LINE_RANDOMIZATION
    )

    b_dg_randomization_active = (
            not b_is_base_case
            and Config.ENABLE_DG_CAPACITY_RANDOMIZATION
    )

    return {
        "switch_state_short_id": s_switch_state_short_id,
        "switch_state_config_id": s_switch_state_config_id,
        "switch_state_row_index": i_global_row_index + 1,
        "switch_state_open_switch_count": i_open_switch_count,
        "switch_state_closed_switch_count": i_closed_switch_count,
        "scenario_id": s_scenario_id,
        "scenario_uid": s_scenario_uid,
        "case_uid": s_scenario_uid,

        "line_length_random_seed": (
            i_line_random_seed
            if b_line_randomization_active
            else -1
        ),
        "line_length_scale_min": (
            Config.LINE_LENGTH_SCALE_MIN
            if b_line_randomization_active
            else 1.0
        ),
        "line_length_scale_max": (
            Config.LINE_LENGTH_SCALE_MAX
            if b_line_randomization_active
            else 1.0
        ),

        "dg_capacity_random_seed": (
            i_dg_random_seed
            if b_dg_randomization_active
            else -1
        ),
        "dg_capacity_scale_min": (
            Config.DG_CAPACITY_SCALE_MIN
            if b_dg_randomization_active
            else 1.0
        ),
        "dg_capacity_scale_max": (
            Config.DG_CAPACITY_SCALE_MAX
            if b_dg_randomization_active
            else 1.0
        ),
    }


def _validate_graph_dg_capacity_consistency(
        scenario_uid: str,
        corridor_rows: list[dict],
        graph_row: dict,
        tolerance: float = 1e-3,
) -> None:
    """
    Validates that graph-node DG capacities agree with the
    independently extracted flat/corridor DG capacities
    for the same scenario.
    """

    s_scenario_uid = scenario_uid
    l_corridor_rows = corridor_rows
    d_graph_row = graph_row
    f_tolerance = tolerance

    d_expected_by_bus: dict[str, float] = {}

    d_expected_has_dg_by_bus: dict[str, int] = {}

    # -------------------------------------------------
    # Helper 1: expected DG capacity
    # -------------------------------------------------
    def _record_expected_capacity(bus_id, capacity) -> None:
        if bus_id is None or capacity is None:
            return

        s_bus_id = str(bus_id).strip()
        f_capacity = float(capacity)

        if s_bus_id in d_expected_by_bus:
            f_previous = d_expected_by_bus[s_bus_id]

            if abs(f_previous - f_capacity) > f_tolerance:
                raise DatasetIntegrityError(
                    f"Scenario {s_scenario_uid}: "
                    f"inconsistent flat-context DG capacity "
                    f"for bus {s_bus_id!r}: "
                    f"first={f_previous:.9f} MVA, "
                    f"later={f_capacity:.9f} MVA"
                )

        d_expected_by_bus[s_bus_id] = f_capacity

    # -------------------------------------------------
    # Helper 2: expected DG presence
    # -------------------------------------------------
    def _record_expected_has_dg(
            bus_id,
            dg_count,
    ) -> None:
        if bus_id is None or dg_count is None:
            return

        s_bus_id = str(bus_id).strip()

        i_has_dg = 1 if int(float(dg_count)) > 0 else 0

        if s_bus_id in d_expected_has_dg_by_bus:
            i_previous = d_expected_has_dg_by_bus[s_bus_id]

            if i_previous != i_has_dg:
                raise DatasetIntegrityError(
                    f"Scenario {s_scenario_uid}: "
                    f"inconsistent flat-context DG presence "
                    f"for bus {s_bus_id!r}: "
                    f"first={i_previous}, later={i_has_dg}"
                )

        d_expected_has_dg_by_bus[s_bus_id] = i_has_dg

    # -------------------------------------------------
    # The corridor-row loop
    # -------------------------------------------------
    for d_row in l_corridor_rows:
        # ---- DG capacity ----

        _record_expected_capacity(
            d_row.get("relay_node_id"),
            d_row.get(
                "relay_busbar_distributed_generation_capacity_mva"
            ),
        )

        _record_expected_capacity(
            d_row.get("subsequent_node_id"),
            d_row.get(
                "subsequent_busbar_distributed_generation_capacity_mva"
            ),
        )

        # ---- DG presence ----

        _record_expected_has_dg(
            d_row.get("relay_node_id"),
            d_row.get(
                "relay_busbar_distributed_generation_count"
            ),
        )

        _record_expected_has_dg(
            d_row.get("subsequent_node_id"),
            d_row.get(
                "subsequent_busbar_distributed_generation_count"
            ),
        )

    l_bus_ids = d_graph_row.get("bus_id", [],)
    l_bus_dg_capacities = d_graph_row.get("bus_dg_capacity_mva", [],)
    l_bus_has_dg = d_graph_row.get("bus_has_dg", [],)

    if len(l_bus_ids) != len(l_bus_dg_capacities):
        raise DatasetIntegrityError(
            f"Scenario {s_scenario_uid}: "
            "graph bus array length mismatch: "
            f"bus_id={len(l_bus_ids)}, "
            f"bus_dg_capacity_mva={len(l_bus_dg_capacities)}"
        )

    if len(l_bus_ids) != len(l_bus_has_dg):
        raise DatasetIntegrityError(
            f"Scenario {s_scenario_uid}: "
            "graph bus array length mismatch: "
            f"bus_id={len(l_bus_ids)}, "
            f"bus_has_dg={len(l_bus_has_dg)}"
        )

    d_actual_by_bus = {
        str(s_bus_id).strip(): float(f_capacity)
        for s_bus_id, f_capacity in zip(
            l_bus_ids,
            l_bus_dg_capacities,
        )
    }

    d_actual_has_dg_by_bus = {
        str(s_bus_id).strip(): int(i_has_dg)
        for s_bus_id, i_has_dg in zip(
            l_bus_ids,
            l_bus_has_dg,
        )
    }

    # -------------------------------------------------
    # Check DG capacity
    # -------------------------------------------------
    for s_bus_id, f_expected in d_expected_by_bus.items():

        if s_bus_id not in d_actual_by_bus:
            raise DatasetIntegrityError(
                f"Scenario {s_scenario_uid}: "
                f"bus {s_bus_id!r} exists in corridor context "
                "but is missing from graph bus_ids."
            )

        f_actual = d_actual_by_bus[s_bus_id]

        if abs(f_actual - f_expected) > f_tolerance:
            raise DatasetIntegrityError(
                f"Scenario {s_scenario_uid}: "
                f"graph DG capacity mismatch at bus {s_bus_id!r}: "
                f"graph={f_actual:.9f} MVA, "
                f"flat/live={f_expected:.9f} MVA"
            )

    # -------------------------------------------------
    # Check DG presence
    # -------------------------------------------------
    for s_bus_id, i_expected_has_dg in (
        d_expected_has_dg_by_bus.items()
    ):

        if s_bus_id not in d_actual_has_dg_by_bus:
            raise DatasetIntegrityError(
                f"Scenario {s_scenario_uid}: "
                f"bus {s_bus_id!r} exists in flat DG context "
                "but is missing from graph bus_has_dg."
            )

        i_actual_has_dg = d_actual_has_dg_by_bus[s_bus_id]

        if i_actual_has_dg not in (0, 1):
            raise DatasetIntegrityError(
                f"Scenario {s_scenario_uid}: "
                f"invalid bus_has_dg value at bus {s_bus_id!r}: "
                f"{i_actual_has_dg}"
            )

        if i_actual_has_dg != i_expected_has_dg:
            raise DatasetIntegrityError(
                f"Scenario {s_scenario_uid}: "
                f"graph DG-presence mismatch at bus {s_bus_id!r}: "
                f"graph={i_actual_has_dg}, "
                f"flat/live={i_expected_has_dg}"
            )


def _append_payloads_for_scenario(
        project,
        grid,
        app,
        cached_all_grid_dg: list,
        bus_attrs: dict,
        scenario_uid: str,
        meta: dict,
        sw_vec: list[int],
        line_logs: list[dict],
        dg_logs: list[dict],
        scenario_yields: list[ExportPayload],
) -> None:
    """
    Extracts flat corridor rows, builds the graph row, and appends payloads.
    """

    o_project = project
    o_grid = grid
    o_app = app

    l_cached_all_grid_dg = cached_all_grid_dg
    d_bus_attributes = bus_attrs
    s_scenario_uid = scenario_uid
    d_scenario_metadata = meta
    l_switch_status_vector = sw_vec

    l_line_randomization_logs = line_logs
    l_dg_randomization_logs = dg_logs

    l_scenario_payloads = scenario_yields

    logger.info(f"Extracting scenario in slave case: {s_scenario_uid}")
    logger.info(f"Starting corridor processing for {s_scenario_uid}")

    l_corridor_rows = get_corridor_scenario_rows(
        o_project,
        o_grid,
        o_app,
        cached_all_grid_dg=l_cached_all_grid_dg,
    )

    l_yield_line_logs = l_line_randomization_logs
    l_yield_dg_logs = l_dg_randomization_logs

    for d_corridor_row in l_corridor_rows:
        d_flat_row = d_scenario_metadata.copy()
        d_flat_row.update(d_corridor_row)

        l_scenario_payloads.append(
            ExportPayload(
                kind="flat_row",
                data=d_flat_row,
                line_logs=l_yield_line_logs,
                dg_logs=l_yield_dg_logs,
            )
        )

        l_yield_line_logs = []
        l_yield_dg_logs = []

    try:
        d_graph_row = convert_scenario_to_graph_row(
            l_corridor_rows,
            d_scenario_metadata,
            l_switch_status_vector,
            bus_attributes=d_bus_attributes,
        )

    except ValueError as o_error:
        raise DatasetIntegrityError(
            f"Scenario {s_scenario_uid}: "
            f"graph integrity validation failed: {o_error}"
        ) from o_error

    _validate_graph_dg_capacity_consistency(
        scenario_uid=s_scenario_uid,
        corridor_rows=l_corridor_rows,
        graph_row=d_graph_row,
    )

    d_graph_row["directed_edge_scenario_case_ids"] = [
        int(d_row.get("case_id", i + 1))
        for i, d_row in enumerate(l_corridor_rows)
    ]

    l_scenario_payloads.append(
        ExportPayload(
            kind="graph_row",
            data=d_graph_row,
        )
    )


def _process_single_scenario(
        project,
        grid,
        app,
        row: Any,
        sid: str,
        cfg: str,
        global_idx: int,
        sc: int,
        is_base: bool,
        scid: str,
        scenario_uid: str,
        sw_vec: list[int],
        open_switch_count: int,
        closed_switch_count: int,
        cub_lookup: dict,
        orig_lines: dict,
        orig_dgs: dict,
        orig_out: dict,
        orig_sw: dict,
        out_objs: list,
        cached_all_grid_dg: list,
        bus_attrs: dict,
        scenario_yields: list[ExportPayload],
) -> None:
    """
    Processes one base or randomized scenario inside the active slave case.
    """

    o_project = project
    o_grid = grid
    o_app = app

    d_switch_state_row = row
    s_switch_state_short_id = sid
    s_switch_state_config_id = cfg

    i_global_row_index = global_idx
    i_randomized_scenario_index = sc
    b_is_base_case = is_base
    s_scenario_id = scid
    s_scenario_uid = scenario_uid

    l_switch_status_vector = sw_vec
    i_open_switch_count = open_switch_count
    i_closed_switch_count = closed_switch_count

    d_cubicle_lookup = cub_lookup
    d_original_line_states = orig_lines
    d_original_dg_states = orig_dgs
    d_original_outserv_states = orig_out
    d_original_switch_states = orig_sw
    l_outserv_controlled_objects = out_objs
    l_cached_all_grid_dg = cached_all_grid_dg
    d_bus_attributes = bus_attrs
    l_scenario_payloads = scenario_yields

    _restore_and_apply_switch_state(
        row=d_switch_state_row,
        cfg=s_switch_state_config_id,
        cub_lookup=d_cubicle_lookup,
        out_objs=l_outserv_controlled_objects,
        orig_lines=d_original_line_states,
        orig_dgs=d_original_dg_states,
        orig_out=d_original_outserv_states,
        orig_sw=d_original_switch_states,
    )

    i_line_random_seed, i_dg_random_seed = _get_scenario_seeds(
        is_base=b_is_base_case,
        sc=i_randomized_scenario_index,
        global_idx=i_global_row_index,
    )

    l_line_randomization_logs, l_dg_randomization_logs = (
        _apply_randomization_for_scenario(
            is_base=b_is_base_case,
            scid=s_scenario_id,
            sid=s_switch_state_short_id,
            cfg=s_switch_state_config_id,
            scenario_uid=s_scenario_uid,
            line_seed=i_line_random_seed,
            dg_seed=i_dg_random_seed,
            orig_lines=d_original_line_states,
            orig_dgs=d_original_dg_states,
        )
    )

    # Refresh bus attributes from the fully configured live scenario.
    d_bus_attributes = capture_bus_attributes(
        o_app,
        o_grid,
    )

    d_scenario_metadata = _build_scenario_metadata(
        sid=s_switch_state_short_id,
        cfg=s_switch_state_config_id,
        global_idx=i_global_row_index,
        open_switch_count=i_open_switch_count,
        closed_switch_count=i_closed_switch_count,
        scid=s_scenario_id,
        scenario_uid=s_scenario_uid,
        is_base=b_is_base_case,
        line_seed=i_line_random_seed,
        dg_seed=i_dg_random_seed,
    )

    _append_payloads_for_scenario(
        project=o_project,
        grid=o_grid,
        app=o_app,
        cached_all_grid_dg=l_cached_all_grid_dg,
        bus_attrs=d_bus_attributes,
        scenario_uid=s_scenario_uid,
        meta=d_scenario_metadata,
        sw_vec=l_switch_status_vector,
        line_logs=l_line_randomization_logs,
        dg_logs=l_dg_randomization_logs,
        scenario_yields=l_scenario_payloads,
    )


def _cleanup_after_scenario(
        app,
        scenario_uid: str,
        orig_lines: dict,
        orig_dgs: dict,
        orig_out: dict,
        orig_sw: dict,
) -> None:
    """
    Restores grid state, resets calculation state, and consolidates the study case.
    """

    o_app = app
    s_scenario_uid = scenario_uid

    d_original_line_states = orig_lines
    d_original_dg_states = orig_dgs
    d_original_outserv_states = orig_out
    d_original_switch_states = orig_sw

    try:
        restore_grid_state(
            orig_lines=d_original_line_states,
            orig_dgs=d_original_dg_states,
            orig_out=d_original_outserv_states,
            orig_sw=d_original_switch_states,
        )

    except Exception as o_error:
        raise PowerFactoryStateError(
            f"Could not restore slave state after {s_scenario_uid}: {o_error}"
        ) from o_error

    try:
        o_app.ResetCalculation()

    except Exception as o_error:
        logger.warning(
            "Could not reset PowerFactory calculation after "
            f"{s_scenario_uid}: {o_error}"
        )

    try:
        o_active_study_case = o_app.GetActiveStudyCase()

        if o_active_study_case:
            o_active_study_case.Consolidate()

    except Exception as o_error:
        logger.warning(
            f"Could not consolidate study case after {s_scenario_uid}: {o_error}"
        )


def _reactivate_master_and_delete_slaves(
        project,
        app,
        master_study_case,
        master_operation_scenario,
) -> None:
    """
    Reactivates master case/scenario and deletes remaining slave cases.
    """

    o_project = project
    o_app = app
    o_master_study_case = master_study_case
    o_master_operation_scenario = master_operation_scenario

    try:
        o_master_study_case.Activate()
        o_master_operation_scenario.Activate()

    except Exception:
        pass

    delete_existing_slave_cases(
        o_project,
        o_app,
    )


def generate_dataset_cases(
        session: PowerFactorySession,
        stats: DatasetStatistics,
        df_chunk: pd.DataFrame,
        sw_cols: list,
        row_offset: int = 0,
) -> Generator[ExportPayload, None, None]:
    """
    Slave-case workflow.
    For every switch state:
    - create slave study case and slave operation scenario
    - activate slave pair
    - capture original line/DG/switch/outserv states from the active slave
    - process base/randomized scenarios
    - restore slave state after each scenario
    - delete slave pair
    - move to next switch state
    """

    o_session = session
    o_statistics = stats
    df_switch_state_chunk = df_chunk
    l_switch_columns = sw_cols
    i_row_offset = row_offset

    o_app = o_session.app
    o_project = o_session.project

    (
        o_master_study_case,
        o_master_operation_scenario,
    ) = _get_master_case_pair(o_project)

    delete_existing_slave_cases(
        o_project,
        o_app,
    )

    l_switch_columns = _validate_switch_columns(
        df_chunk=df_switch_state_chunk,
        sw_cols=l_switch_columns,
    )

    i_total_rows = len(df_switch_state_chunk)

    try:
        for i_local_row_position, (_, d_switch_state_row) in enumerate(
                df_switch_state_chunk.iterrows()
        ):
            i_global_row_index = i_row_offset + i_local_row_position

            d_switch_state_context = _build_switch_state_context(
                row=d_switch_state_row,
                sw_cols=l_switch_columns,
                global_idx=i_global_row_index,
                local_pos=i_local_row_position,
                total_rows=i_total_rows,
            )

            s_switch_state_short_id = d_switch_state_context["sid"]
            s_switch_state_config_id = d_switch_state_context["cfg"]
            l_switch_status_vector = d_switch_state_context["sw_vec"]
            i_closed_switch_count = d_switch_state_context[
                "closed_switch_count"
            ]
            i_open_switch_count = d_switch_state_context[
                "open_switch_count"
            ]

            o_slave_study_case = None
            o_slave_operation_scenario = None

            try:
                (
                    o_slave_study_case,
                    o_slave_operation_scenario,
                    o_grid,
                ) = _create_and_activate_slave_for_switch_state(
                    session=o_session,
                    project=o_project,
                    master_study_case=o_master_study_case,
                    master_operation_scenario=o_master_operation_scenario,
                    sid=s_switch_state_short_id,
                )

                (
                    d_cubicle_lookup,
                    d_original_line_states,
                    d_original_dg_states,
                    d_original_outserv_states,
                    d_original_switch_states,
                    l_outserv_controlled_objects,
                    l_cached_all_grid_dg,
                    d_bus_attributes,
                ) = capture_original_state_from_active_slave(
                    o_grid,
                    o_app,
                )

                l_switch_state_payloads = []
                i_randomized_scenarios_seen = 0
                i_extreme_zone2_scenarios = 0
                s_base_scenario_uid = None
                d_base_extreme_zone2 = None

                for (
                        i_randomized_scenario_index,
                        b_is_base_case,
                        s_scenario_id,
                ) in _iter_scenarios():
                    s_scenario_uid = (
                        f"{s_switch_state_short_id}_{s_scenario_id}"
                    )

                    if b_is_base_case:
                        s_base_scenario_uid = s_scenario_uid
                    else:
                        i_randomized_scenarios_seen += 1

                    l_scenario_payloads = []

                    try:
                        _process_single_scenario(
                            project=o_project,
                            grid=o_grid,
                            app=o_app,
                            row=d_switch_state_row,
                            sid=s_switch_state_short_id,
                            cfg=s_switch_state_config_id,
                            global_idx=i_global_row_index,
                            sc=i_randomized_scenario_index,
                            is_base=b_is_base_case,
                            scid=s_scenario_id,
                            scenario_uid=s_scenario_uid,
                            sw_vec=l_switch_status_vector,
                            open_switch_count=i_open_switch_count,
                            closed_switch_count=i_closed_switch_count,
                            cub_lookup=d_cubicle_lookup,
                            orig_lines=d_original_line_states,
                            orig_dgs=d_original_dg_states,
                            orig_out=d_original_outserv_states,
                            orig_sw=d_original_switch_states,
                            out_objs=l_outserv_controlled_objects,
                            cached_all_grid_dg=l_cached_all_grid_dg,
                            bus_attrs=d_bus_attributes,
                            scenario_yields=l_scenario_payloads,
                        )

                        d_extreme_zone2 = _find_extreme_zone2_target(
                            l_scenario_payloads
                        )
                        if d_extreme_zone2 is not None:
                            if b_is_base_case:
                                d_base_extreme_zone2 = d_extreme_zone2
                            else:
                                i_extreme_zone2_scenarios += 1
                            f_ratio = d_extreme_zone2[
                                "target_to_base_ratio"
                            ]
                            s_detail = (
                                f"relay={d_extreme_zone2['relay_id']}, "
                                f"corridor={d_extreme_zone2['protected_corridor_id']}, "
                                f"|Z2,target|/|Z2,base|={f_ratio:.6f} >= "
                                f"{Config.ZONE2_TARGET_TO_BASE_RATIO_LIMIT:.6f}"
                            )
                            l_scenario_payloads.clear()
                            l_scenario_payloads.append(
                                ExportPayload(
                                    kind="skipped_scenario",
                                    data={
                                        "scenario_uid": s_scenario_uid,
                                        "switch_state_row_index": i_global_row_index + 1,
                                        "switch_state_config_id": s_switch_state_config_id,
                                        "reason": "extreme_zone2_target_to_base_ratio",
                                        "detail": s_detail,
                                        **d_extreme_zone2,
                                        "threshold": Config.ZONE2_TARGET_TO_BASE_RATIO_LIMIT,
                                    },
                                )
                            )
                            logger.warning(
                                "Skipping complete scenario %s: %s",
                                s_scenario_uid,
                                s_detail,
                            )

                    except ShortCircuitScenarioError as o_error:
                        # The scenario is indivisible: do not export earlier relay rows.
                        l_scenario_payloads.clear()
                        s_skip_reason = (
                            "short_circuit_execution_failed"
                            if isinstance(o_error, ShortCircuitExecutionError)
                            else "short_circuit_current_invalid"
                        )
                        logger.warning(
                            "Skipping scenario %s (ConfigID=%s, reason=%s): %s",
                            s_scenario_uid,
                            s_switch_state_config_id,
                            s_skip_reason,
                            o_error,
                        )
                        l_scenario_payloads.append(
                            ExportPayload(
                                kind="skipped_scenario",
                                data={
                                    "scenario_uid": s_scenario_uid,
                                    "switch_state_row_index": i_global_row_index + 1,
                                    "switch_state_config_id": s_switch_state_config_id,
                                    "reason": s_skip_reason,
                                    "detail": str(o_error),
                                },
                            )
                        )

                    except DatasetIntegrityError as o_error:
                        raise DatasetIntegrityError(
                            f"Scenario {s_scenario_uid}: {o_error}"
                        ) from o_error

                    except Exception as o_error:
                        l_scenario_payloads.clear()
                        logger.exception(
                            "Scenario generation failed for "
                            f"{s_scenario_uid}: {o_error}"
                        )

                        _increment_invalid_reason(
                            stats=o_statistics,
                            reason="scenario_generation_failed",
                        )

                    finally:
                        _cleanup_after_scenario(
                            app=o_app,
                            scenario_uid=s_scenario_uid,
                            orig_lines=d_original_line_states,
                            orig_dgs=d_original_dg_states,
                            orig_out=d_original_outserv_states,
                            orig_sw=d_original_switch_states,
                        )

                    l_switch_state_payloads.extend(l_scenario_payloads)

                b_persistent_zone2_failure = (
                    i_randomized_scenarios_seen
                    >= Config.PERSISTENT_ZONE2_FAILURE_MIN_RANDOMIZATIONS
                    and i_extreme_zone2_scenarios
                    == i_randomized_scenarios_seen
                )

                if b_persistent_zone2_failure:
                    l_switch_state_payloads = [
                        o_payload
                        for o_payload in l_switch_state_payloads
                        if (
                            o_payload.kind == "skipped_scenario"
                            and o_payload.data.get("scenario_uid")
                            != s_base_scenario_uid
                        )
                    ]

                    if s_base_scenario_uid is not None:
                        l_switch_state_payloads.insert(
                            0,
                            ExportPayload(
                                kind="skipped_scenario",
                                data={
                                    "scenario_uid": s_base_scenario_uid,
                                    "switch_state_row_index": i_global_row_index + 1,
                                    "switch_state_config_id": s_switch_state_config_id,
                                    "reason": "persistent_switch_state_extreme_zone2",
                                    "detail": (
                                        "All "
                                        f"{i_randomized_scenarios_seen} randomized "
                                        "realizations exceeded the Zone-2 "
                                        "target-to-base ratio limit."
                                    ),
                                    "failed_randomizations": i_extreme_zone2_scenarios,
                                    "evaluated_randomizations": i_randomized_scenarios_seen,
                                    "threshold": Config.ZONE2_TARGET_TO_BASE_RATIO_LIMIT,
                                    "base_case_exceeded_ratio_limit": (
                                        d_base_extreme_zone2 is not None
                                    ),
                                },
                            ),
                        )

                    logger.warning(
                        "Dropping switch state %s: all %d randomized "
                        "realizations exceeded the Zone-2 ratio limit.",
                        s_switch_state_short_id,
                        i_randomized_scenarios_seen,
                    )

                for o_payload in l_switch_state_payloads:
                    yield o_payload

            except DatasetIntegrityError:
                raise

            except Exception as o_error:
                logger.exception(
                    "Switch state "
                    f"{s_switch_state_short_id} failed in slave workflow: "
                    f"{o_error}"
                )

                _increment_invalid_reason(
                    stats=o_statistics,
                    reason="switch_state_slave_workflow_failed",
                )

            finally:
                delete_slave_case_pair(
                    app=o_app,
                    master_study_case=o_master_study_case,
                    master_operation_scenario=o_master_operation_scenario,
                    slave_sc=o_slave_study_case,
                    slave_os=o_slave_operation_scenario,
                )

                logger.info(
                    "Deleted slave case/scenario for switch state "
                    f"{s_switch_state_short_id}."
                )

    finally:
        _reactivate_master_and_delete_slaves(
            project=o_project,
            app=o_app,
            master_study_case=o_master_study_case,
            master_operation_scenario=o_master_operation_scenario,
        )

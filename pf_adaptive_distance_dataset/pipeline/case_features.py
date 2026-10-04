# case_features.py
from __future__ import annotations

import json
import logging

from ..core.config import Config
from ..core.dataset_schema import l_case_feature_columns

from ..pf_api.pf_utils import (
    get_boolean_value,
    get_safe_name,
    get_unique_objects,
)

from ..domain.topology import (
    get_terminal_is_junction_node,
    get_relay_id_from_terminal_and_line,
    get_branch_line_names,
    get_line_impedance,
    get_line_length_value,
)

from ..domain.dg_utils import (
    get_unique_distributed_generators_from_terminals,
    summarize_dg_by_corridor_location,
)

from ..domain.network_topology import (
    extract_network_once,
    detect_protected_corridors_once,
    build_empty_branch_summary,
    filter_valid_downstream_branches_excluding_relay_return,
    summarize_next_node_downstream_context,
    summarize_parallel_protected_corridors,
    get_parallel_protected_corridor_line_names,
    select_zone2_downstream_branch_group,
)

from ..domain.zone_reach import (
    select_zone3_longest_valid_downstream_branch,
    count_forward_parallel_branch_groups_for_corridor,
    calculate_distance_zone_reaches_for_corridor,
)

from ..domain.infeed import (
    split_protected_corridor_turbines_by_zone_reach,
    get_relay_side_and_cubicle,
    calculate_zone_infeed_summary_for_turbines,
)


logger = logging.getLogger(__name__)


def get_case_feature_dict_for_corridor(
    o_project,
    o_app,
    o_grid,
    d_corridor,
    l_all_corridors,
    i_case_index,
    cached_all_grid_dg=None,
):
    """
    Builds one complete flat feature row for one protected corridor direction.
    The output dictionary keys must stay aligned with l_case_feature_columns.
    """

    l_cached_all_grid_dg = cached_all_grid_dg

    # ------------------------------------------------------------------
    # Basic corridor objects and identifiers
    # ------------------------------------------------------------------
    o_relay_busbar = d_corridor.get("relay_busbar")
    o_subsequent_busbar = d_corridor.get("subsequent_busbar")
    o_first_line_section = d_corridor.get("first_line_section")
    o_relay_cubicle = d_corridor.get("relay_cubicle")
    l_protected_corridor_lines = d_corridor.get("line_sections", [])

    l_protected_corridor_junction_nodes = d_corridor.get(
        "junction_nodes",
        [],
    )

    # Ordered physical terminal path:
    # relay busbar -> junction(s) -> subsequent busbar
    l_protected_corridor_terminal_path = (
            [o_relay_busbar]
            + list(l_protected_corridor_junction_nodes)
            + [o_subsequent_busbar]
    )

    s_relay_id = get_relay_id_from_terminal_and_line(
        o_relay_busbar,
        o_first_line_section,
    )

    s_relay_node_name = get_safe_name(o_relay_busbar)
    s_subsequent_node_name = get_safe_name(o_subsequent_busbar)

    s_protected_corridor_id = d_corridor.get(
        "protected_corridor_id",
        "Unknown_Corridor",
    )

    # ------------------------------------------------------------------
    # Ordered physical corridor representation
    # ------------------------------------------------------------------
    l_protected_corridor_terminal_path_ids = [
        get_safe_name(o_terminal)
        for o_terminal in l_protected_corridor_terminal_path
    ]

    l_protected_corridor_line_section_ids = [
        get_safe_name(o_line)
        for o_line in l_protected_corridor_lines
    ]

    l_protected_corridor_line_section_length_km = [
        round(get_line_length_value(o_line), 6)
        for o_line in l_protected_corridor_lines
    ]

    l_protected_corridor_line_section_r_ohm = []
    l_protected_corridor_line_section_x_ohm = []

    for o_line in l_protected_corridor_lines:
        f_line_r_ohm, f_line_x_ohm = get_line_impedance(o_line)

        l_protected_corridor_line_section_r_ohm.append(
            round(f_line_r_ohm, 6)
        )

        l_protected_corridor_line_section_x_ohm.append(
            round(f_line_x_ohm, 6)
        )

    if (
            len(l_protected_corridor_terminal_path_ids)
            != len(l_protected_corridor_line_section_ids) + 1
    ):
        raise RuntimeError(
            "Invalid protected corridor physical path: "
            f"corridor={s_protected_corridor_id!r}, "
            f"terminals={len(l_protected_corridor_terminal_path_ids)}, "
            f"line_sections={len(l_protected_corridor_line_section_ids)}"
        )

    # ------------------------------------------------------------------
    # Parallel corridor, downstream branch, and zone-reach summaries
    # ------------------------------------------------------------------
    d_parallel_summary = summarize_parallel_protected_corridors(
        d_corridor,
        l_all_corridors,
    )

    l_valid_downstream_branches = (
        filter_valid_downstream_branches_excluding_relay_return(
            d_corridor,
            l_all_corridors,
        )
    )

    d_zone2_selection = select_zone2_downstream_branch_group(
        d_corridor, l_all_corridors, l_valid_branches=l_valid_downstream_branches
    )

    d_zone2_branch = d_zone2_selection.get(
        "selected_branch",
        build_empty_branch_summary(o_subsequent_busbar),
    )

    d_zone_reaches = calculate_distance_zone_reaches_for_corridor(
        d_corridor,
        d_parallel_summary,
        l_all_corridors,
        l_valid_branches=l_valid_downstream_branches
    )

    d_next_node_summary = summarize_next_node_downstream_context(
        l_valid_downstream_branches,
    )

    _d_zone3_branch = (
            select_zone3_longest_valid_downstream_branch(
                d_corridor,
                l_all_corridors,
                l_valid_branches=l_valid_downstream_branches,
            )
            or build_empty_branch_summary(o_subsequent_busbar)
    )

    i_parallel_group_count_forward, _i_max_parallel_branch_count = (
        count_forward_parallel_branch_groups_for_corridor(
            d_corridor,
            l_all_corridors,
            l_valid_branches=l_valid_downstream_branches,
        )
    )

    # ------------------------------------------------------------------
    # Distributed generation summaries
    # ------------------------------------------------------------------
    d_dg_context = summarize_dg_by_corridor_location(
        d_corridor,
        d_zone2_branch,
    )

    (
        l_zone1_turbines,
        l_zone2_protected_corridor_turbines,
    ) = split_protected_corridor_turbines_by_zone_reach(
        d_corridor,
        d_dg_context["protected_corridor"].get("objects", []),
        Config.REACH_GF,
    )

    l_zone2_turbines = get_unique_objects(
        list(l_zone1_turbines)
        + list(l_zone2_protected_corridor_turbines)
        + d_dg_context["subsequent_busbar"].get("objects", [])
        + d_dg_context["downstream_branch"].get("objects", [])
    )

    # ------------------------------------------------------------------
    # Relay-side Ikss reference
    # ------------------------------------------------------------------
    (
        o_relay_side_cubicle,
        _s_relay_side_cubicle_name,
        s_relay_ikss_line_attr,
        _s_relay_side_bus_attr,
    ) = get_relay_side_and_cubicle(
        o_first_line_section,
        o_relay_busbar,
    )

    # No current is read before the required fault calculation. The infeed
    # routine reads its denominator after each successful DG-specific fault.
    f_relay_reference_ikss_ka = 0.0

    # ------------------------------------------------------------------
    # Zone 1 and Zone 2 infeed summaries
    # ------------------------------------------------------------------
    d_zone1_infeed = calculate_zone_infeed_summary_for_turbines(
        o_project,
        o_app,
        o_grid,
        o_relay_busbar,
        o_relay_side_cubicle or o_relay_cubicle,
        o_first_line_section,
        s_relay_ikss_line_attr,
        l_zone1_turbines,
        l_protected_corridor_lines[:],
        d_zone_reaches.get("zone1_r_reach_ohm", 0.0),
        d_zone_reaches.get("zone1_x_reach_ohm", 0.0),
        f_relay_reference_ikss_ka,
        "zone1",
        l_all_grid_dg=l_cached_all_grid_dg,
    )

    d_zone2_infeed = calculate_zone_infeed_summary_for_turbines(
        o_project,
        o_app,
        o_grid,
        o_relay_busbar,
        o_relay_side_cubicle or o_relay_cubicle,
        o_first_line_section,
        s_relay_ikss_line_attr,
        l_zone2_turbines,
        l_protected_corridor_lines + d_zone2_branch.get("Branch Lines", []),
        d_zone_reaches.get("zone2_r_reach_ohm", 0.0),
        d_zone_reaches.get("zone2_x_reach_ohm", 0.0),
        f_relay_reference_ikss_ka,
        "zone2",
        l_all_grid_dg=l_cached_all_grid_dg,
    )

    # ------------------------------------------------------------------
    # Reused branch helper lists
    # ------------------------------------------------------------------
    l_zone2_branch_terminals = d_zone2_branch.get("Branch Terminals", [])

    l_zone2_branch_junction_terminals = [
        o_terminal
        for o_terminal in l_zone2_branch_terminals
        if get_terminal_is_junction_node(o_terminal)
    ]

    l_zone2_branch_distributed_generators = (
        get_unique_distributed_generators_from_terminals(
            l_zone2_branch_terminals,
        )
    )

    # ------------------------------------------------------------------
    # Build final flat row.
    # Do not rename these dictionary keys. They are the exported schema.
    # ------------------------------------------------------------------
    d_case_row = {
        "case_id": f"{i_case_index:05d}",
        "relay_id": s_relay_id,
        "relay_node_id": s_relay_node_name,
        "subsequent_node_id": s_subsequent_node_name,
        "protected_corridor_id": s_protected_corridor_id,

        # Ordered physical protected-corridor representation.
        "protected_corridor_terminal_path_json": json.dumps(l_protected_corridor_terminal_path_ids),
        "protected_corridor_line_section_id_json": json.dumps(l_protected_corridor_line_section_ids),
        "protected_corridor_line_section_length_km_json": json.dumps(l_protected_corridor_line_section_length_km),
        "protected_corridor_line_section_r_ohm_json": json.dumps(l_protected_corridor_line_section_r_ohm),
        "protected_corridor_line_section_x_ohm_json": json.dumps(l_protected_corridor_line_section_x_ohm),

        "protected_corridor_length_km": d_corridor.get("total_length_km", 0.0),
        "protected_corridor_r_ohm": d_corridor.get("total_r_ohm", 0.0),
        "protected_corridor_x_ohm": d_corridor.get("total_x_ohm", 0.0),
        "corridor_hop_count": d_corridor.get("hop_count", 0),
        # Always 1 by construction. get_line_is_available_for_topology() filters
        # out-of-service and open-switched lines before corridors are built, so
        # every corridor in l_protected_corridors is guaranteed in-service.
        # Topology variation across switch states is captured at the scenario
        # level (each scenario has its own graph with only active corridors),
        # not as edge masking within a fixed graph. This column is therefore
        # constant within the exported corridor rows.
        "line_is_in_service": 1,

        "protected_corridor_is_parallel": get_boolean_value(
            bool(d_parallel_summary.get("is_parallel"))
        ),
        "protected_corridor_parallel_count": d_parallel_summary.get(
            "parallel_count",
            1,
        ),
        "protected_corridor_parallel_id": (
            get_parallel_protected_corridor_line_names(
                d_corridor,
                l_all_corridors,
            )
        ),

        "next_node_busbar_count": d_next_node_summary.get("busbar_count", 0),
        "next_node_junction_node_count": d_next_node_summary.get(
            "junction_node_count",
            0,
        ),
        "next_node_distributed_generation_count": d_next_node_summary.get(
            "distributed_generation_count",
            0,
        ),

        "shortest_downstream_branch_id": d_zone2_branch.get("Branch ID", ""),
        "shortest_downstream_branch_remote_node_id": d_zone2_branch.get(
            "Remote Node ID",
            "",
        ),
        "shortest_downstream_branch_hop_count": int(
            d_zone2_branch.get("Hop Count", 0)
        ),
        "shortest_downstream_branch_length_km": d_zone2_branch.get(
            "Branch Length",
            0.0,
        ),
        "shortest_downstream_branch_r_ohm": d_zone2_branch.get(
            "Branch Resistance",
            0.0,
        ),
        "shortest_downstream_branch_x_ohm": d_zone2_branch.get(
            "Branch Reactance",
            0.0,
        ),
        "shortest_downstream_branch_has_parallel": get_boolean_value(
            d_zone2_branch.get("Has Parallel", False)
        ),
        "shortest_downstream_branch_parallel_count": int(
            d_zone2_branch.get("Parallel Count", 0)
        ),
        "shortest_downstream_branch_parallel_id": d_zone2_branch.get(
            "Parallel Lines",
            "",
        ),
        "shortest_downstream_branch_junction_node_count": len(
            l_zone2_branch_junction_terminals
        ),
        "shortest_downstream_branch_junction_node_id": " | ".join(
            [
                get_safe_name(o_terminal)
                for o_terminal in l_zone2_branch_junction_terminals
            ]
        ),
        "shortest_downstream_branch_distributed_generation_count": len(
            l_zone2_branch_distributed_generators
        ),
        "shortest_downstream_branch_distributed_generation_id": " | ".join(
            [
                get_safe_name(o_dg)
                for o_dg in l_zone2_branch_distributed_generators
            ]
        ),

        "zone2_branch_selection_method": d_zone_reaches.get(
            "zone2_branch_selection_method",
            "",
        ),
        "zone2_selected_branch_id": d_zone_reaches.get(
            "zone2_downstream_branch_id",
            "",
        ),
        "zone2_reach_calculation_method": d_zone_reaches.get(
            "zone2_reach_calculation_method",
            "",
        ),
        "zone2_impedance_basis": d_zone_reaches.get(
            "zone2_impedance_basis",
            "",
        ),

        "parallel_group_count_forward": i_parallel_group_count_forward,
        "parallel_group_id": d_zone_reaches.get(
            "zone2_parallel_branch_for_complex_id",
            "",
        ),
        "zone2_parallel_branch_for_complex_length_km": d_zone_reaches.get(
            "zone2_parallel_branch_for_complex_length_km",
            0.0,
        ),
        "zone2_parallel_branch_for_complex_r_ohm": d_zone_reaches.get(
            "zone2_parallel_branch_for_complex_r_ohm",
            0.0,
        ),
        "zone2_parallel_branch_for_complex_x_ohm": d_zone_reaches.get(
            "zone2_parallel_branch_for_complex_x_ohm",
            0.0,
        ),

        "zone3_applicable": d_zone_reaches.get(
            "zone3_applicable",
            1,
        ),
        "zone3_branch_selection_method": d_zone_reaches.get(
            "zone3_branch_selection_method",
            "",
        ),
        "zone3_selected_branch_id": d_zone_reaches.get(
            "zone3_downstream_branch_id",
            "",
        ),
        "longest_downstream_branch_hop_count": int(
            _d_zone3_branch.get("Hop Count", 0)
        ),
        "longest_downstream_branch_length_km": d_zone_reaches.get(
            "zone3_downstream_branch_length_km",
            0.0,
        ),
        "longest_downstream_branch_r_ohm": d_zone_reaches.get(
            "zone3_downstream_branch_r_ohm",
            0.0,
        ),
        "longest_downstream_branch_x_ohm": d_zone_reaches.get(
            "zone3_downstream_branch_x_ohm",
            0.0,
        ),

        "relay_busbar_distributed_generation_count": d_dg_context[
            "relay_busbar"
        ].get("count", 0),
        "relay_busbar_distributed_generation_id": d_dg_context[
            "relay_busbar"
        ].get("names", ""),
        "relay_busbar_distributed_generation_capacity_mva": d_dg_context[
            "relay_busbar"
        ].get("capacity_mva", 0.0),

        "protected_corridor_distributed_generation_count": d_dg_context[
            "protected_corridor"
        ].get("count", 0),
        "protected_corridor_distributed_generation_id": d_dg_context[
            "protected_corridor"
        ].get("names", ""),
        "protected_corridor_distributed_generation_capacity_mva": d_dg_context[
            "protected_corridor"
        ].get("capacity_mva", 0.0),

        "subsequent_busbar_distributed_generation_count": d_dg_context[
            "subsequent_busbar"
        ].get("count", 0),
        "subsequent_busbar_distributed_generation_id": d_dg_context[
            "subsequent_busbar"
        ].get("names", ""),
        "subsequent_busbar_distributed_generation_capacity_mva": d_dg_context[
            "subsequent_busbar"
        ].get("capacity_mva", 0.0),

        "downstream_branch_distributed_generation_count": d_dg_context[
            "downstream_branch"
        ].get("count", 0),
        "downstream_branch_distributed_generation_id": d_dg_context[
            "downstream_branch"
        ].get("names", ""),
        "downstream_branch_distributed_generation_capacity_mva": d_dg_context[
            "downstream_branch"
        ].get("capacity_mva", 0.0),

        "remote_busbar_distributed_generation_count": d_dg_context[
            "remote_busbar"
        ].get("count", 0),
        "remote_busbar_distributed_generation_id": d_dg_context[
            "remote_busbar"
        ].get("names", ""),
        "remote_busbar_distributed_generation_capacity_mva": d_dg_context[
            "remote_busbar"
        ].get("capacity_mva", 0.0),

        "zone1_turbines_candidate_count": d_zone1_infeed.get(
            "turbines_candidate_count",
            0,
        ),
        "zone1_turbines_candidate_id": d_zone1_infeed.get(
            "turbines_candidate_id",
            "",
        ),
        "zone1_turbines_considered_count": d_zone1_infeed.get(
            "turbines_considered_count",
            0,
        ),
        "zone1_turbines_considered_id": d_zone1_infeed.get(
            "turbines_considered_id",
            "",
        ),
        "zone1_turbines_skipped_count": d_zone1_infeed.get(
            "turbines_skipped_count",
            0,
        ),
        "zone1_turbines_skipped_id": d_zone1_infeed.get(
            "turbines_skipped_id",
            "",
        ),
        "zone1_turbines_total_capacity_mva": d_zone1_infeed.get(
            "turbines_total_capacity_mva",
            0.0,
        ),
        "zone1_total_ikss_contribution_ratio": d_zone1_infeed.get(
            "total_ikss_contribution_ratio",
            0.0,
        ),
        "zone1_max_single_ikss_contribution_ratio": d_zone1_infeed.get(
            "max_single_ikss_contribution_ratio",
            0.0,
        ),

        "zone2_turbines_candidate_count": d_zone2_infeed.get(
            "turbines_candidate_count",
            0,
        ),
        "zone2_turbines_candidate_id": d_zone2_infeed.get(
            "turbines_candidate_id",
            "",
        ),
        "zone2_turbines_considered_count": d_zone2_infeed.get(
            "turbines_considered_count",
            0,
        ),
        "zone2_turbines_considered_id": d_zone2_infeed.get(
            "turbines_considered_id",
            "",
        ),
        "zone2_turbines_skipped_count": d_zone2_infeed.get(
            "turbines_skipped_count",
            0,
        ),
        "zone2_turbines_skipped_id": d_zone2_infeed.get(
            "turbines_skipped_id",
            "",
        ),
        "zone2_turbines_total_capacity_mva": d_zone2_infeed.get(
            "turbines_total_capacity_mva",
            0.0,
        ),
        "zone2_total_ikss_contribution_ratio": d_zone2_infeed.get(
            "total_ikss_contribution_ratio",
            0.0,
        ),
        "zone2_max_single_ikss_contribution_ratio": d_zone2_infeed.get(
            "max_single_ikss_contribution_ratio",
            0.0,
        ),

        "base_zone1_r_reach_ohm": d_zone_reaches.get(
            "zone1_r_reach_ohm",
            0.0,
        ),
        "base_zone1_x_reach_ohm": d_zone_reaches.get(
            "zone1_x_reach_ohm",
            0.0,
        ),
        "zone1_infeed_correction_r_ohm": d_zone1_infeed.get(
            "infeed_correction_r_ohm",
            0.0,
        ),
        "zone1_infeed_correction_x_ohm": d_zone1_infeed.get(
            "infeed_correction_x_ohm",
            0.0,
        ),
        "target_zone1_r_reach_ohm": round(
            d_zone_reaches.get("zone1_r_reach_ohm", 0.0)
            + d_zone1_infeed.get("infeed_correction_r_ohm", 0.0),
            3,
        ),
        "target_zone1_x_reach_ohm": round(
            d_zone_reaches.get("zone1_x_reach_ohm", 0.0)
            + d_zone1_infeed.get("infeed_correction_x_ohm", 0.0),
            3,
        ),

        "base_zone2_r_reach_ohm": d_zone_reaches.get(
            "zone2_r_reach_ohm",
            0.0,
        ),
        "base_zone2_x_reach_ohm": d_zone_reaches.get(
            "zone2_x_reach_ohm",
            0.0,
        ),
        "zone2_infeed_correction_r_ohm": d_zone2_infeed.get(
            "infeed_correction_r_ohm",
            0.0,
        ),
        "zone2_infeed_correction_x_ohm": d_zone2_infeed.get(
            "infeed_correction_x_ohm",
            0.0,
        ),
        "target_zone2_r_reach_ohm": round(
            d_zone_reaches.get("zone2_r_reach_ohm", 0.0)
            + d_zone2_infeed.get("infeed_correction_r_ohm", 0.0),
            3,
        ),
        "target_zone2_x_reach_ohm": round(
            d_zone_reaches.get("zone2_x_reach_ohm", 0.0)
            + d_zone2_infeed.get("infeed_correction_x_ohm", 0.0),
            3,
        ),

        "base_zone3_r_reach_ohm": d_zone_reaches.get(
            "zone3_r_reach_ohm",
            0.0,
        ),
        "base_zone3_x_reach_ohm": d_zone_reaches.get(
            "zone3_x_reach_ohm",
            0.0,
        ),
        "target_zone3_r_reach_ohm": d_zone_reaches.get(
            "zone3_r_reach_ohm",
            0.0,
        ),
        "target_zone3_x_reach_ohm": d_zone_reaches.get(
            "zone3_x_reach_ohm",
            0.0,
        ),
    }

    s_zone3_log = (
        f"{d_case_row['target_zone3_r_reach_ohm']:.2f}|"
        f"{d_case_row['target_zone3_x_reach_ohm']:.2f}"
        if d_case_row.get("zone3_applicable", 1)
        else "N/A"
    )

    logger.info(
        f"Processed {i_case_index:03d} - {s_relay_node_name}: "
        f"{s_relay_id} -> {s_protected_corridor_id} -> "
        f"{s_subsequent_node_name} -> "
        f"{d_case_row.get('zone2_selected_branch_id')} -> "
        f"{d_case_row.get('shortest_downstream_branch_remote_node_id', '?')} -> "
        f"DG_Z1={d_case_row.get('zone1_turbines_candidate_count', 0)} "
        f"DG_Z2={d_case_row.get('zone2_turbines_candidate_count', 0)} -> "
        f"Z1: {d_case_row.get('target_zone1_r_reach_ohm', 0.0):.2f}|"
        f"{d_case_row.get('target_zone1_x_reach_ohm', 0.0):.2f}, "
        f"Z2: {d_case_row.get('target_zone2_r_reach_ohm', 0.0):.2f}|"
        f"{d_case_row.get('target_zone2_x_reach_ohm', 0.0):.2f}, "
        f"Z3: {s_zone3_log}"
    )

    return {
        s_column: d_case_row.get(s_column, None)
        for s_column in l_case_feature_columns
    }


def get_corridor_scenario_rows(
    o_project,
    o_grid,
    o_app,
    cached_all_grid_dg=None,
):
    """
    Extracts all protected-corridor feature rows for the active grid scenario.
    """

    l_cached_all_grid_dg = cached_all_grid_dg

    d_network = extract_network_once(
        o_grid,
        o_app,
    )

    l_protected_corridors = detect_protected_corridors_once(d_network)

    l_protected_corridors = sorted(
        l_protected_corridors,
        key=lambda d_corridor: (
            get_safe_name(d_corridor.get("relay_busbar")),
            get_safe_name(d_corridor.get("subsequent_busbar")),
            get_branch_line_names(d_corridor.get("line_sections", [])),
        ),
    )

    return [
        get_case_feature_dict_for_corridor(
            o_project,
            o_app,
            o_grid,
            d_corridor,
            l_protected_corridors,
            i_case_index,
            cached_all_grid_dg=l_cached_all_grid_dg,
        )
        for i_case_index, d_corridor in enumerate(l_protected_corridors, 1)
    ]

# network_topology.py
from __future__ import annotations

from collections import defaultdict

from ..core.config import PFAttr

from ..pf_api.pf_utils import (
    get_boolean_value,
    get_safe_name,
    get_safe_full_name,
    get_pf_attribute,
    get_unique_objects,
    is_object_inside_grid,
)

from .topology import (
    get_terminal_from_cubicle,
    get_opposite_terminal,
    get_line_impedance,
    get_line_length_value,
    get_line_is_available_for_topology,
    get_terminal_is_busbar,
    get_terminal_is_junction_node,
    get_terminal_connected_lines,
    get_cubicle_for_line_at_terminal,
    line_connects_terminal_pair,
    get_parallel_lines_for_line_list,
    get_branch_line_names,
)

from .dg_utils import (
    get_unique_distributed_generators_from_terminals,
)

import logging


def extract_network_once(grid, app):
    """
    Extracts all available topology lines inside the selected grid once.
    """

    o_grid = grid
    o_app = app

    l_available_lines = [
        o_line
        for o_line in o_app.GetCalcRelevantObjects("*.ElmLne") or []
        if get_line_is_available_for_topology(o_line)
    ]

    l_line_data = []
    l_skipped_lines = []

    for o_line in l_available_lines:
        try:
            o_from_cubicle = get_pf_attribute(o_line, PFAttr.BUS1)
            o_to_cubicle = get_pf_attribute(o_line, PFAttr.BUS2)

            if not o_from_cubicle or not o_to_cubicle:
                continue

            o_from_terminal = get_terminal_from_cubicle(o_from_cubicle)
            o_to_terminal = get_terminal_from_cubicle(o_to_cubicle)

            if (
                not o_from_terminal
                or not o_to_terminal
                or not is_object_inside_grid(o_from_terminal, o_grid)
                or not is_object_inside_grid(o_to_terminal, o_grid)
            ):
                continue

            l_line_data.append(
                {
                    "_pf_line_object": o_line,
                    "_pf_terminal_from": o_from_terminal,
                    "_pf_terminal_to": o_to_terminal,
                    "_pf_cubicle_from": o_from_cubicle,
                    "_pf_cubicle_to": o_to_cubicle,
                }
            )

        except Exception as o_error:
            l_skipped_lines.append(
                (
                    get_safe_name(o_line),
                    str(o_error),
                )
            )

    return {
        "line_data": l_line_data,
        "skipped_lines": l_skipped_lines,
    }


def calculate_line_path_impedance(lines):
    """
    Calculates total R, X, length, and hop count for a list of line sections.
    """

    l_lines = lines

    f_total_r_ohm = 0.0
    f_total_x_ohm = 0.0
    f_total_length_km = 0.0

    for o_line in l_lines:
        f_line_r_ohm, f_line_x_ohm = get_line_impedance(o_line)

        f_total_r_ohm += f_line_r_ohm
        f_total_x_ohm += f_line_x_ohm
        f_total_length_km += get_line_length_value(o_line)

    return {
        "total_r_ohm": round(f_total_r_ohm, 3),
        "total_x_ohm": round(f_total_x_ohm, 3),
        "total_length_km": round(f_total_length_km, 3),
        "hop_count": len(l_lines),
    }


def get_next_line_from_junction(junction, prev_line, visited):
    """
    Finds the next unvisited line connected to a junction terminal.
    """

    o_junction_terminal = junction
    o_previous_line = prev_line
    l_visited_lines = visited

    for o_candidate_line in get_terminal_connected_lines(o_junction_terminal):
        if (
            o_candidate_line != o_previous_line
            and o_candidate_line not in l_visited_lines
        ):
            return o_candidate_line

    return None


def trace_protected_corridor_from_relay_busbar(rb, first_line):
    """
    Traces a protected corridor from a relay busbar through line/junction
    sections until the next busbar is reached.
    """

    o_relay_busbar = rb
    o_first_line = first_line

    if not get_terminal_is_busbar(o_relay_busbar):
        return None

    l_corridor_lines = []
    l_junction_nodes = []
    l_visited_lines = []

    o_current_terminal = o_relay_busbar
    o_current_line = o_first_line

    while o_current_line and o_current_line not in l_visited_lines:
        l_visited_lines.append(o_current_line)
        l_corridor_lines.append(o_current_line)

        o_next_terminal = get_opposite_terminal(
            o_current_line,
            o_current_terminal,
        )

        if not o_next_terminal:
            break

        if get_terminal_is_busbar(o_next_terminal):
            d_path_impedance = calculate_line_path_impedance(l_corridor_lines)

            return {
                "relay_busbar": o_relay_busbar,
                "subsequent_busbar": o_next_terminal,
                "line_sections": l_corridor_lines,
                "junction_nodes": l_junction_nodes,
                "first_line_section": l_corridor_lines[0],
                "last_line_section": l_corridor_lines[-1],
                "protected_corridor_id": get_branch_line_names(l_corridor_lines),
                **d_path_impedance,
            }

        if get_terminal_is_junction_node(o_next_terminal):
            l_junction_nodes.append(o_next_terminal)

            o_current_line = get_next_line_from_junction(
                o_next_terminal,
                o_current_line,
                l_visited_lines,
            )

            o_current_terminal = o_next_terminal
            continue

        break

    return None


def build_all_directional_protected_corridors(line_data):
    """
    Builds all unique directional protected corridors from extracted line data.
    """

    l_line_data = line_data

    l_protected_corridors = []
    s_seen_corridor_keys = set()

    for d_line_record in l_line_data:
        o_line = d_line_record["_pf_line_object"]
        o_from_terminal = d_line_record["_pf_terminal_from"]
        o_to_terminal = d_line_record["_pf_terminal_to"]

        for o_relay_busbar in [o_from_terminal, o_to_terminal]:
            if not get_terminal_is_busbar(o_relay_busbar):
                continue

            d_corridor = trace_protected_corridor_from_relay_busbar(
                o_relay_busbar,
                o_line,
            )

            if not d_corridor:
                continue

            t_corridor_key = (
                get_safe_full_name(d_corridor["relay_busbar"]),
                get_safe_full_name(d_corridor["subsequent_busbar"]),
                d_corridor["protected_corridor_id"],
            )

            if t_corridor_key not in s_seen_corridor_keys:
                d_corridor["relay_cubicle"] = get_cubicle_for_line_at_terminal(
                    d_corridor["first_line_section"],
                    d_corridor["relay_busbar"],
                )

                s_seen_corridor_keys.add(t_corridor_key)
                l_protected_corridors.append(d_corridor)

    return l_protected_corridors


def detect_protected_corridors_once(net):
    """
    Detects protected corridors from the already extracted network dictionary.
    """

    d_network = net

    return build_all_directional_protected_corridors(
        d_network.get("line_data", [])
    )


def trace_branch_from_busbar_to_next_busbar(sb, first_line, excluded):
    """
    Traces a downstream branch from a busbar to the next busbar.
    """

    o_start_busbar = sb
    o_first_line = first_line
    l_excluded_lines = excluded

    l_branch_lines = []
    l_branch_terminals = [o_start_busbar]
    l_visited_lines = l_excluded_lines[:]

    o_current_terminal = o_start_busbar
    o_current_line = o_first_line

    while o_current_line and o_current_line not in l_visited_lines:
        l_visited_lines.append(o_current_line)
        l_branch_lines.append(o_current_line)

        o_next_terminal = get_opposite_terminal(
            o_current_line,
            o_current_terminal,
        )

        if not o_next_terminal:
            break

        l_branch_terminals.append(o_next_terminal)

        if get_terminal_is_busbar(o_next_terminal):
            return build_branch_summary(
                l_branch_lines,
                l_branch_terminals,
            )

        if get_terminal_is_junction_node(o_next_terminal):
            o_current_line = get_next_line_from_junction(
                o_next_terminal,
                o_current_line,
                l_visited_lines,
            )

            o_current_terminal = o_next_terminal
            continue

        break

    return None


def build_empty_branch_summary(sb):
    """
    Builds an empty branch summary for cases where no valid branch exists.
    """

    o_start_busbar = sb

    return {
        "Branch Lines": [],
        "Branch Terminals": [o_start_busbar] if o_start_busbar else [],
        "Branch ID": "",
        "Remote Node ID": "",
        "Hop Count": 0,
        "Branch Length": 0.0,
        "Branch Resistance": 0.0,
        "Branch Reactance": 0.0,
        "Has Parallel": False,
        "Parallel Count": 0,
        "Parallel Lines": "",
        "Junction Node Count": 0,
        "Distributed Generation Count": 0,
    }


def get_branch_far_end_busbar(branch):
    """
    Returns the final terminal of a branch summary, normally the remote busbar.
    """

    d_branch = branch
    l_branch_terminals = d_branch.get("Branch Terminals", [])

    return l_branch_terminals[-1] if l_branch_terminals else None


def build_branch_summary(lines, terms):
    """
    Builds a complete branch summary from branch lines and branch terminals.
    """

    l_branch_lines = lines
    l_branch_terminals = terms

    d_branch_impedance = calculate_line_path_impedance(l_branch_lines)

    l_parallel_lines = get_parallel_lines_for_line_list(l_branch_lines)

    l_junction_nodes = [
        o_terminal
        for o_terminal in l_branch_terminals
        if get_terminal_is_junction_node(o_terminal)
    ]

    l_distributed_generators = get_unique_distributed_generators_from_terminals(
        l_branch_terminals
    )

    return {
        "Branch Lines": l_branch_lines,
        "Branch Terminals": l_branch_terminals,
        "Branch ID": get_branch_line_names(l_branch_lines),
        "Remote Node ID": get_safe_name(l_branch_terminals[-1]) if l_branch_terminals else "",
        "Hop Count": len(l_branch_lines),
        "Branch Length": d_branch_impedance.get("total_length_km", 0.0),
        "Branch Resistance": d_branch_impedance.get("total_r_ohm", 0.0),
        "Branch Reactance": d_branch_impedance.get("total_x_ohm", 0.0),
        "Has Parallel": len(l_parallel_lines) > 0,
        "Parallel Count": len(l_parallel_lines),
        "Parallel Lines": get_branch_line_names(l_parallel_lines),
        "Junction Node Count": len(l_junction_nodes),
        "Distributed Generation Count": len(l_distributed_generators),
    }


def filter_valid_downstream_branches_excluding_relay_return(corr, all_corridors=None):
    """
    Finds valid downstream branches from the subsequent busbar while excluding
    the relay-return direction and parallel protected corridors.
    """

    d_corridor = corr
    l_all_corridors = all_corridors

    o_relay_busbar = d_corridor.get("relay_busbar")
    o_subsequent_busbar = d_corridor.get("subsequent_busbar")

    l_excluded_lines = d_corridor.get("line_sections", [])[:]

    if not o_relay_busbar or not o_subsequent_busbar:
        return []

    if l_all_corridors:
        for d_parallel_corridor in find_parallel_protected_corridors(
            d_corridor,
            l_all_corridors,
        ):
            for o_line in d_parallel_corridor.get("line_sections", []):
                if o_line not in l_excluded_lines:
                    l_excluded_lines.append(o_line)

    l_valid_branches = []

    for o_candidate_line in get_terminal_connected_lines(o_subsequent_busbar):
        if (
            o_candidate_line in l_excluded_lines
            or line_connects_terminal_pair(
                o_candidate_line,
                get_safe_full_name(o_relay_busbar),
                get_safe_full_name(o_subsequent_busbar),
            )
        ):
            continue

        d_branch = trace_branch_from_busbar_to_next_busbar(
            o_subsequent_busbar,
            o_candidate_line,
            l_excluded_lines[:],
        )

        if not d_branch:
            continue

        o_far_end_busbar = get_branch_far_end_busbar(d_branch)

        if (
            not o_far_end_busbar
            or get_safe_full_name(o_far_end_busbar)
            in [
                get_safe_full_name(o_relay_busbar),
                get_safe_full_name(o_subsequent_busbar),
            ]
        ):
            continue

        l_valid_branches.append(d_branch)

    return l_valid_branches


def summarize_next_node_downstream_context(branches):
    """
    Summarizes busbars, junction nodes, and DGs downstream of the next node.
    """

    l_branches = branches

    l_remote_busbars = []
    l_junction_nodes = []
    l_distributed_generators = []

    for d_branch in l_branches:
        l_branch_terminals = d_branch.get("Branch Terminals", [])

        if l_branch_terminals and get_terminal_is_busbar(l_branch_terminals[-1]):
            l_remote_busbars.append(l_branch_terminals[-1])

        l_junction_nodes.extend(
            [
                o_terminal
                for o_terminal in l_branch_terminals
                if get_terminal_is_junction_node(o_terminal)
            ]
        )

        l_distributed_generators.extend(
            get_unique_distributed_generators_from_terminals(
                l_branch_terminals
            )
        )

    return {
        "busbar_count": len(get_unique_objects(l_remote_busbars)),
        "junction_node_count": len(get_unique_objects(l_junction_nodes)),
        "distributed_generation_count": len(
            get_unique_objects(l_distributed_generators)
        ),
    }


def protected_corridors_have_same_busbar_pair(ca, cb):
    """
    Checks whether two protected corridors connect the same busbar pair.
    """

    d_corridor_a = ca
    d_corridor_b = cb

    if not all(
        [
            d_corridor_a.get("relay_busbar"),
            d_corridor_a.get("subsequent_busbar"),
            d_corridor_b.get("relay_busbar"),
            d_corridor_b.get("subsequent_busbar"),
        ]
    ):
        return False

    # Use an ordered tuple so that A→B and B→A are treated as different pairs.
    # A parallel corridor must protect the same line direction (same relay busbar,
    # same subsequent busbar), not just the same pair of busbars.
    s_busbar_pair_a = (
        get_safe_full_name(d_corridor_a.get("relay_busbar")),
        get_safe_full_name(d_corridor_a.get("subsequent_busbar")),
    )

    s_busbar_pair_b = (
        get_safe_full_name(d_corridor_b.get("relay_busbar")),
        get_safe_full_name(d_corridor_b.get("subsequent_busbar")),
    )

    return s_busbar_pair_a == s_busbar_pair_b


def get_protected_corridor_line_full_name_set(corr):
    """
    Returns the protected corridor line set using full PowerFactory object names.
    """

    d_corridor = corr

    return frozenset(
        [
            get_safe_full_name(o_line)
            for o_line in d_corridor.get("line_sections", [])
        ]
    )


def find_parallel_protected_corridors(tcorr, all_corrs):
    """
    Finds protected corridors that share the same busbar pair but use different
    physical line sections.
    """

    d_target_corridor = tcorr
    l_all_corridors = all_corrs

    s_target_line_set = get_protected_corridor_line_full_name_set(
        d_target_corridor
    )

    return [
        d_candidate_corridor
        for d_candidate_corridor in l_all_corridors
        if (
            d_candidate_corridor != d_target_corridor
            and protected_corridors_have_same_busbar_pair(
                d_target_corridor,
                d_candidate_corridor,
            )
            and get_protected_corridor_line_full_name_set(
                d_candidate_corridor
            )
            != s_target_line_set
        )
    ]


def summarize_parallel_protected_corridors(tcorr, all_corrs):
    """
    Summarizes parallel protected corridors for a target corridor.

    For triple-circuit (or higher) corridors, the exported impedance values
    represent the equivalent parallel impedance across ALL parallel corridors,
    computed as Z_eq = 1 / sum(1/Zi). This is the value seen by the relay
    for an infeed via the parallel path and is more accurate than reporting
    only the first parallel corridor.

    Note: parallel_r_ohm / parallel_x_ohm / parallel_length_km are exported
    as informational columns only. They do not feed into the Zone 2 reach
    formula (which derives its parallel branch impedance independently via
    select_zone2_downstream_branch_group).
    """

    d_target_corridor = tcorr
    l_all_corridors = all_corrs

    l_parallel_corridors = find_parallel_protected_corridors(
        d_target_corridor,
        l_all_corridors,
    )

    if not l_parallel_corridors:
        return {
            "is_parallel": False,
            "parallel_count": 1,
            "parallel_alternative_lines": "",
            "parallel_r_ohm": 0.0,
            "parallel_x_ohm": 0.0,
            "parallel_length_km": 0.0,
        }

    i_parallel_count = len(l_parallel_corridors)

    if i_parallel_count > 1:
        logging.getLogger(__name__).warning(
            f"Triple-circuit (or higher) corridor detected: "
            f"{i_parallel_count} parallel corridors found for "
            f"'{get_branch_line_names(d_target_corridor.get('line_sections', []))}'. "
            f"Exporting equivalent parallel impedance across all {i_parallel_count} parallels."
        )

    # Equivalent parallel impedance: Z_eq = 1 / sum(1/Zi)
    # Use complex arithmetic to handle R and X together.
    c_admittance_sum = complex(0.0, 0.0)
    f_total_length_km = 0.0

    for d_par_corridor in l_parallel_corridors:
        f_r = float(d_par_corridor.get("total_r_ohm", 0.0))
        f_x = float(d_par_corridor.get("total_x_ohm", 0.0))
        f_total_length_km += float(d_par_corridor.get("total_length_km", 0.0))

        c_impedance = complex(f_r, f_x)
        if abs(c_impedance) > 1e-12:
            c_admittance_sum += 1.0 / c_impedance

    if abs(c_admittance_sum) > 1e-12:
        c_z_eq = 1.0 / c_admittance_sum
        f_parallel_r_ohm = round(c_z_eq.real, 6)
        f_parallel_x_ohm = round(c_z_eq.imag, 6)
    else:
        f_parallel_r_ohm = 0.0
        f_parallel_x_ohm = 0.0

    # Average length across parallel corridors (informational only)
    f_avg_length_km = round(f_total_length_km / i_parallel_count, 3)

    # All parallel line names joined — not just the first
    s_all_parallel_line_names = " | ".join(
        get_branch_line_names(d_par.get("line_sections", []))
        for d_par in l_parallel_corridors
    )

    return {
        "is_parallel": True,
        "parallel_count": i_parallel_count + 1,
        "parallel_alternative_lines": s_all_parallel_line_names,
        "parallel_r_ohm": f_parallel_r_ohm,
        "parallel_x_ohm": f_parallel_x_ohm,
        "parallel_length_km": f_avg_length_km,
    }


def get_physical_corridor_line_key(corr):
    """
    Builds a stable physical corridor key from sorted full line names.
    """

    d_corridor = corr

    return " || ".join(
        sorted(
            [
                get_safe_full_name(o_line)
                for o_line in d_corridor.get("line_sections", [])
                if o_line
            ]
        )
    )


def get_parallel_protected_corridor_line_names(corr, all_corrs):
    """
    Returns line names of parallel protected corridors with the same busbar pair.
    """

    d_target_corridor = corr
    l_all_corridors = all_corrs

    s_target_busbar_pair = (
        get_safe_full_name(d_target_corridor.get("relay_busbar")),
        get_safe_full_name(d_target_corridor.get("subsequent_busbar")),
    )

    s_target_physical_line_key = get_physical_corridor_line_key(
        d_target_corridor
    )

    d_parallel_line_names_by_key = {}

    for d_candidate_corridor in l_all_corridors:
        s_candidate_busbar_pair = (
            get_safe_full_name(d_candidate_corridor.get("relay_busbar")),
            get_safe_full_name(d_candidate_corridor.get("subsequent_busbar")),
        )

        s_candidate_physical_line_key = get_physical_corridor_line_key(
            d_candidate_corridor
        )

        if (
            s_candidate_busbar_pair == s_target_busbar_pair
            and s_candidate_physical_line_key
            and s_candidate_physical_line_key != s_target_physical_line_key
        ):
            d_parallel_line_names_by_key[s_candidate_physical_line_key] = (
                get_branch_line_names(
                    d_candidate_corridor.get("line_sections", [])
                )
            )

    return " | ".join(
        sorted(d_parallel_line_names_by_key.values())
    )


def select_zone2_downstream_branch_group(corr, l_all_corridors=None, l_valid_branches=None):
    """
    Selects the Zone 2 downstream branch group.
    Branch groups are formed by remote busbar. For each group, the branch with
    the smallest reactance, then length, then resistance is selected. The
    selected remote-busbar group with the smallest selected branch reactance and
    length becomes the Zone 2 branch group.
    """

    d_corridor = corr
    l_corridors = l_all_corridors
    o_subsequent_busbar = d_corridor.get("subsequent_busbar")

    if l_valid_branches is None:
        l_valid_branches = filter_valid_downstream_branches_excluding_relay_return(
            d_corridor,
            l_corridors,
        )

    if not l_valid_branches:
        return {
            "selected_branch": build_empty_branch_summary(o_subsequent_busbar),
            "selected_parallel_branches": [],
            "selected_remote_busbar_name": "",
            "zone2_branch_selection_method": "no_valid_downstream_branch",
            "zone2_reach_calculation_method": "simple_no_downstream_branch",
            "zone2_selected_branch_has_parallel": 0,
            "zone2_selected_branch_parallel_count": 0,
        }

    d_branches_by_remote_busbar = defaultdict(list)

    for d_branch in l_valid_branches:
        o_remote_busbar = get_branch_far_end_busbar(d_branch)

        d_branches_by_remote_busbar[
            get_safe_full_name(o_remote_busbar)
        ].append(d_branch)

    l_remote_busbar_group_summaries = []

    for _s_remote_busbar_full_name, l_parallel_branches in (
        d_branches_by_remote_busbar.items()
    ):
        l_sorted_parallel_branches = sorted(
            l_parallel_branches,
            key=lambda d_branch: (
                float(d_branch.get("Branch Reactance", 0.0)),
                float(d_branch.get("Branch Length", 0.0)),
                float(d_branch.get("Branch Resistance", 0.0)),
            ),
        )

        l_remote_busbar_group_summaries.append(
            {
                "selected_branch": l_sorted_parallel_branches[0],
                "parallel_branches": l_sorted_parallel_branches,
                "parallel_count": len(l_sorted_parallel_branches),
                "remote_busbar": get_branch_far_end_busbar(
                    l_sorted_parallel_branches[0]
                ),
            }
        )

    l_remote_busbar_group_summaries = sorted(
        l_remote_busbar_group_summaries,
        key=lambda d_group_summary: (
            float(
                d_group_summary["selected_branch"].get(
                    "Branch Reactance",
                    0.0,
                )
            ),
            float(
                d_group_summary["selected_branch"].get(
                    "Branch Length",
                    0.0,
                )
            ),
        ),
    )

    d_selected_group_summary = l_remote_busbar_group_summaries[0]

    b_selected_branch_has_parallel = (
        d_selected_group_summary["parallel_count"] > 1
    )

    return {
        "selected_branch": d_selected_group_summary["selected_branch"],
        "selected_parallel_branches": d_selected_group_summary["parallel_branches"],
        "selected_remote_busbar_name": get_safe_name(
            d_selected_group_summary["remote_busbar"]
        ),
        "zone2_branch_selection_method": (
            "shortest_valid_remote_busbar_group_with_parallel"
            if b_selected_branch_has_parallel
            else "shortest_valid_downstream_branch"
        ),
        "zone2_reach_calculation_method": (
            "complex_parallel_zone2_reach"
            if b_selected_branch_has_parallel
            else "simple_zone2_reach"
        ),
        "zone2_selected_branch_has_parallel": get_boolean_value(
            b_selected_branch_has_parallel
        ),
        "zone2_selected_branch_parallel_count": d_selected_group_summary[
            "parallel_count"
        ],
    }

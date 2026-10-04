# zone_reach.py
from __future__ import annotations

from collections import defaultdict

from ..core.config import Config

from ..pf_api.pf_utils import (
    get_boolean_value,
    get_safe_full_name,
)

from .topology import get_branch_line_names

from .network_topology import (
    build_empty_branch_summary,
    filter_valid_downstream_branches_excluding_relay_return,
    select_zone2_downstream_branch_group,
    get_branch_far_end_busbar,
)


def select_parallel_branch_for_complex_zone2(sel_branch, par_branches):
    """
    Selects the shortest valid parallel branch alternative for complex Zone 2 reach.
    The selected branch itself is excluded. Branch identity is compared using
    branch line names, matching the original implementation.
    """

    d_selected_branch = sel_branch
    l_parallel_branches = par_branches

    l_candidate_parallel_branches = [
        d_branch
        for d_branch in l_parallel_branches
        if d_branch != d_selected_branch
        and get_branch_line_names(d_branch.get("Branch Lines", []))
        != get_branch_line_names(d_selected_branch.get("Branch Lines", []))
    ]

    if not l_candidate_parallel_branches:
        return None

    return sorted(
        l_candidate_parallel_branches,
        key=lambda d_branch: (
            float(d_branch.get("Branch Reactance", 0.0)),
            float(d_branch.get("Branch Length", 0.0)),
        ),
    )[0]


def select_zone3_longest_valid_downstream_branch(corr, l_all_corridors=None, l_valid_branches=None):
    """
    Selects the longest valid downstream branch for Zone 3 reach.
    """

    d_corridor = corr
    l_corridors = l_all_corridors

    if l_valid_branches is None:
        l_valid_branches = filter_valid_downstream_branches_excluding_relay_return(
            d_corridor,
            l_corridors
        )

    l_valid_downstream_branches = l_valid_branches

    if not l_valid_downstream_branches:
        return build_empty_branch_summary(
            d_corridor.get("subsequent_busbar")
        )

    return sorted(
        l_valid_downstream_branches,
        key=lambda d_branch: (
            float(d_branch.get("Branch Length", 0.0)),
            float(d_branch.get("Branch Reactance", 0.0)),
        ),
        reverse=True,
    )[0]


def count_forward_parallel_branch_groups_for_corridor(corr, l_all_corridors=None, l_valid_branches=None):
    """
    Counts downstream remote-busbar groups that contain parallel branches.
    """

    d_corridor = corr
    l_corridors = l_all_corridors

    if l_valid_branches is None:
        l_valid_branches = filter_valid_downstream_branches_excluding_relay_return(
            d_corridor, l_corridors
        )

    l_valid_downstream_branches = l_valid_branches

    d_branches_by_remote_busbar = defaultdict(list)

    for d_branch in l_valid_downstream_branches:
        s_remote_busbar_full_name = get_safe_full_name(
            get_branch_far_end_busbar(d_branch)
        )

        d_branches_by_remote_busbar[s_remote_busbar_full_name].append(
            d_branch
        )

    i_parallel_group_count = 0
    i_max_parallel_count = 0

    for _s_remote_busbar_full_name, l_branches in (
        d_branches_by_remote_busbar.items()
    ):
        if len(l_branches) > 1:
            i_parallel_group_count += 1
            i_max_parallel_count = max(
                i_max_parallel_count,
                len(l_branches),
            )

    return i_parallel_group_count, i_max_parallel_count


def calculate_simple_zone2_reach(r_conn, x_conn, r_branch, x_branch):
    """
    Calculates simple Zone 2 reach using the configured reach grading factor.
    """

    f_corridor_r_ohm = r_conn
    f_corridor_x_ohm = x_conn
    f_branch_r_ohm = r_branch
    f_branch_x_ohm = x_branch

    f_reach_grading_factor = Config.REACH_GF

    return (
        round(
            f_reach_grading_factor
            * (
                f_corridor_r_ohm
                + f_reach_grading_factor * f_branch_r_ohm
            ),
            3,
        ),
        round(
            f_reach_grading_factor
            * (
                f_corridor_x_ohm
                + f_reach_grading_factor * f_branch_x_ohm
            ),
            3,
        ),
    )


def calculate_complex_zone2_reach(
    r_conn,
    x_conn,
    r_short,
    x_short,
    r_parallel,
    x_parallel,
):
    """
    Calculates complex Zone 2 reach for a selected branch with a parallel branch.
    """

    f_corridor_r_ohm = r_conn
    f_corridor_x_ohm = x_conn

    f_short_branch_r_ohm = r_short
    f_short_branch_x_ohm = x_short

    f_parallel_branch_r_ohm = r_parallel
    f_parallel_branch_x_ohm = x_parallel

    c_corridor_impedance = complex(
        f_corridor_r_ohm,
        f_corridor_x_ohm,
    )

    c_short_branch_impedance = complex(
        f_short_branch_r_ohm,
        f_short_branch_x_ohm,
    )

    c_parallel_branch_impedance = complex(
        f_parallel_branch_r_ohm,
        f_parallel_branch_x_ohm,
    )

    if (
        abs(c_short_branch_impedance + c_parallel_branch_impedance)
        <= Config.ZERO_TOLERANCE
    ):
        return calculate_simple_zone2_reach(
            f_corridor_r_ohm,
            f_corridor_x_ohm,
            f_short_branch_r_ohm,
            f_short_branch_x_ohm,
        )

    f_reach_grading_factor = Config.REACH_GF

    # Z2 = GF * (Z_corr + ((Z_par + (1-GF)*Z_short) * GF * Z_short) / (Z_short + Z_par))
    # Z_corr  = protected corridor impedance
    # Z_short = selected (shortest) downstream branch
    # Z_par   = parallel branch for the same remote busbar
    c_zone2_reach_impedance = f_reach_grading_factor * (
        c_corridor_impedance
        + (
            (
                c_parallel_branch_impedance
                + (1.0 - f_reach_grading_factor)
                * c_short_branch_impedance
            )
            * f_reach_grading_factor
            * c_short_branch_impedance
        )
        / (c_short_branch_impedance + c_parallel_branch_impedance)
    )

    return (
        round(c_zone2_reach_impedance.real, 3),
        round(c_zone2_reach_impedance.imag, 3),
    )


def calculate_distance_zone_reaches_for_corridor(corr, p_summary, all_corrs=None, l_valid_branches=None):
    """
    Calculates Zone 1, Zone 2, and Zone 3 distance-protection reaches.

    The original implementation recalculates the Zone 2 branch group internally
    using select_zone2_downstream_branch_group(), so this replacement keeps
    the same behavior.
    """

    d_corridor = corr
    d_parallel_summary = p_summary
    l_all_corridors = all_corrs

    f_corridor_r_ohm = float(d_corridor.get("total_r_ohm", 0.0))
    f_corridor_x_ohm = float(d_corridor.get("total_x_ohm", 0.0))
    f_corridor_length_km = float(d_corridor.get("total_length_km", 0.0))

    f_zone1_r_reach_ohm = round(
        Config.REACH_GF * f_corridor_r_ohm,
        3,
    )

    f_zone1_x_reach_ohm = round(
        Config.REACH_GF * f_corridor_x_ohm,
        3,
    )

    d_zone2_selection = select_zone2_downstream_branch_group(
        d_corridor, l_all_corridors, l_valid_branches=l_valid_branches
    )

    b_has_valid_downstream_branch = (
            d_zone2_selection.get("zone2_branch_selection_method") != "no_valid_downstream_branch")

    d_zone2_branch = d_zone2_selection.get(
        "selected_branch",
        build_empty_branch_summary(d_corridor.get("subsequent_busbar")),
    )

    l_zone2_parallel_branches = d_zone2_selection.get(
        "selected_parallel_branches",
        [],
    )

    f_zone2_branch_r_ohm = float(
        d_zone2_branch.get("Branch Resistance", 0.0)
    )

    f_zone2_branch_x_ohm = float(
        d_zone2_branch.get("Branch Reactance", 0.0)
    )

    f_zone2_branch_length_km = float(
        d_zone2_branch.get("Branch Length", 0.0)
    )

    (
        f_intended_zone2_r_reach_ohm,
        f_intended_zone2_x_reach_ohm,
    ) = calculate_simple_zone2_reach(
        f_corridor_r_ohm,
        f_corridor_x_ohm,
        f_zone2_branch_r_ohm,
        f_zone2_branch_x_ohm,
    )

    f_zone2_parallel_branch_r_ohm = 0.0
    f_zone2_parallel_branch_x_ohm = 0.0
    f_zone2_parallel_branch_length_km = 0.0
    s_zone2_parallel_branch_id = ""

    b_zone2_selected_branch_has_parallel = bool(
        d_zone2_selection.get(
            "zone2_selected_branch_has_parallel",
            0,
        )
    )

    if not b_has_valid_downstream_branch:
        # Supervisor-approved terminal-line policy: Zone 2 reaches 120%.
        f_intended_zone2_r_reach_ohm = round(
            Config.TERMINAL_ZONE2_REACH_FACTOR * f_corridor_r_ohm,
            3,
        )
        f_intended_zone2_x_reach_ohm = round(
            Config.TERMINAL_ZONE2_REACH_FACTOR * f_corridor_x_ohm,
            3,
        )

        f_zone2_r_reach_ohm = f_intended_zone2_r_reach_ohm
        f_zone2_x_reach_ohm = f_intended_zone2_x_reach_ohm

        s_zone2_impedance_basis = (
            "terminal_120_percent_protected_corridor_impedance"
        )

    elif (
        b_zone2_selected_branch_has_parallel
        and len(l_zone2_parallel_branches) > 1
    ):
        # Standard parallel Zone 2 check.
        d_zone2_complex_parallel_branch = (
            select_parallel_branch_for_complex_zone2(
                d_zone2_branch,
                l_zone2_parallel_branches,
            )
        )

        if d_zone2_complex_parallel_branch:
            f_zone2_parallel_branch_r_ohm = float(
                d_zone2_complex_parallel_branch.get(
                    "Branch Resistance",
                    0.0,
                )
            )

            f_zone2_parallel_branch_x_ohm = float(
                d_zone2_complex_parallel_branch.get(
                    "Branch Reactance",
                    0.0,
                )
            )

            f_zone2_parallel_branch_length_km = float(
                d_zone2_complex_parallel_branch.get(
                    "Branch Length",
                    0.0,
                )
            )

            s_zone2_parallel_branch_id = get_branch_line_names(
                d_zone2_complex_parallel_branch.get("Branch Lines", [])
            )

            (
                f_zone2_r_reach_ohm,
                f_zone2_x_reach_ohm,
            ) = calculate_complex_zone2_reach(
                f_corridor_r_ohm,
                f_corridor_x_ohm,
                f_zone2_branch_r_ohm,
                f_zone2_branch_x_ohm,
                f_zone2_parallel_branch_r_ohm,
                f_zone2_parallel_branch_x_ohm,
            )

            s_zone2_impedance_basis = (
                "complex_parallel_shortest_branch_and_parallel_branch_impedance"
            )

        else:
            (
                f_zone2_r_reach_ohm,
                f_zone2_x_reach_ohm,
            ) = calculate_simple_zone2_reach(
                f_corridor_r_ohm,
                f_corridor_x_ohm,
                f_zone2_branch_r_ohm,
                f_zone2_branch_x_ohm,
            )

            s_zone2_impedance_basis = (
                "simple_selected_downstream_branch_impedance_no_parallel_alternative_found"
            )

    else:
        (
            f_zone2_r_reach_ohm,
            f_zone2_x_reach_ohm,
        ) = calculate_simple_zone2_reach(
            f_corridor_r_ohm,
            f_corridor_x_ohm,
            f_zone2_branch_r_ohm,
            f_zone2_branch_x_ohm,
        )

        s_zone2_impedance_basis = "simple_selected_downstream_branch_impedance"

    if b_has_valid_downstream_branch:
        d_zone3_branch = select_zone3_longest_valid_downstream_branch(
            d_corridor, l_all_corridors, l_valid_branches=l_valid_branches,
        )

        f_zone3_branch_r_ohm = float(
            d_zone3_branch.get("Branch Resistance", 0.0)
        )

        f_zone3_branch_x_ohm = float(
            d_zone3_branch.get("Branch Reactance", 0.0)
        )

        f_zone3_r_reach_ohm = round(
            f_corridor_r_ohm
            + (Config.ZONE3_REACH_FACTOR * f_zone3_branch_r_ohm),
            3,
        )

        f_zone3_x_reach_ohm = round(
            f_corridor_x_ohm
            + (Config.ZONE3_REACH_FACTOR * f_zone3_branch_x_ohm),
            3,
        )

    else:
        # Terminal line: there is no downstream branch requiring Zone 3.
        d_zone3_branch = build_empty_branch_summary(
            d_corridor.get("subsequent_busbar")
        )

        f_zone3_branch_r_ohm = 0.0
        f_zone3_branch_x_ohm = 0.0

        f_zone3_r_reach_ohm = None
        f_zone3_x_reach_ohm = None

    return {
        "protected_corridor_r_ohm": round(f_corridor_r_ohm, 3),
        "protected_corridor_x_ohm": round(f_corridor_x_ohm, 3),
        "protected_corridor_length_km": round(f_corridor_length_km, 3),

        "zone1_r_reach_ohm": f_zone1_r_reach_ohm,
        "zone1_x_reach_ohm": f_zone1_x_reach_ohm,

        "zone2_r_reach_ohm": f_zone2_r_reach_ohm,
        "zone2_x_reach_ohm": f_zone2_x_reach_ohm,

        "intended_zone2_r_reach_ohm": f_intended_zone2_r_reach_ohm,
        "intended_zone2_x_reach_ohm": f_intended_zone2_x_reach_ohm,

        "zone2_reach_calculation_method": d_zone2_selection.get(
            "zone2_reach_calculation_method",
            "",
        ),
        "zone2_branch_selection_method": d_zone2_selection.get(
            "zone2_branch_selection_method",
            "",
        ),
        "zone2_selected_remote_busbar": d_zone2_selection.get(
            "selected_remote_busbar_name",
            "",
        ),
        "zone2_downstream_branch_id": get_branch_line_names(
            d_zone2_branch.get("Branch Lines", [])
        ),
        "zone2_downstream_branch_length_km": round(
            f_zone2_branch_length_km,
            3,
        ),
        "zone2_downstream_branch_r_ohm": round(
            f_zone2_branch_r_ohm,
            3,
        ),
        "zone2_downstream_branch_x_ohm": round(
            f_zone2_branch_x_ohm,
            3,
        ),
        "zone2_selected_branch_has_parallel": get_boolean_value(
            b_zone2_selected_branch_has_parallel
        ),
        "zone2_selected_branch_parallel_count": int(
            d_zone2_selection.get(
                "zone2_selected_branch_parallel_count",
                0,
            )
        ),
        "zone2_impedance_basis": s_zone2_impedance_basis,
        "zone2_branch_for_reach_r_ohm": round(f_zone2_branch_r_ohm, 3),
        "zone2_branch_for_reach_x_ohm": round(f_zone2_branch_x_ohm, 3),
        "zone2_parallel_branch_for_complex_id": s_zone2_parallel_branch_id,
        "zone2_parallel_branch_for_complex_length_km": round(
            f_zone2_parallel_branch_length_km,
            3,
        ),
        "zone2_parallel_branch_for_complex_r_ohm": round(
            f_zone2_parallel_branch_r_ohm,
            3,
        ),
        "zone2_parallel_branch_for_complex_x_ohm": round(
            f_zone2_parallel_branch_x_ohm,
            3,
        ),

        "zone3_applicable": get_boolean_value(
            b_has_valid_downstream_branch
        ),
        "zone3_r_reach_ohm": f_zone3_r_reach_ohm,
        "zone3_x_reach_ohm": f_zone3_x_reach_ohm,
        "zone3_branch_selection_method": (
            "longest_valid_downstream_branch_excluding_relay_return"
            if b_has_valid_downstream_branch
            else "not_applicable_no_downstream_branch"
        ),
        "zone3_downstream_branch_id": get_branch_line_names(
            d_zone3_branch.get("Branch Lines", [])
        ),
        "zone3_downstream_branch_length_km": round(
            float(d_zone3_branch.get("Branch Length", 0.0)),
            3,
        ),
        "zone3_downstream_branch_r_ohm": round(
            f_zone3_branch_r_ohm,
            3,
        ),
        "zone3_downstream_branch_x_ohm": round(
            f_zone3_branch_x_ohm,
            3,
        ),

        "protected_corridor_is_parallel": get_boolean_value(
            bool(d_parallel_summary.get("is_parallel", False))
        ),
        "protected_corridor_parallel_count": int(
            d_parallel_summary.get("parallel_count", 1)
        ),
        "protected_corridor_parallel_alternative_lines": (
            d_parallel_summary.get(
                "parallel_alternative_lines",
                "",
            )
        ),
    }

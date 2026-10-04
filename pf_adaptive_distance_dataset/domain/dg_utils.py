# dg_utils.py
from __future__ import annotations

from ..core.config import PFAttr

from ..pf_api.pf_utils import (
    get_boolean_value,
    get_safe_name,
    get_pf_attribute,
    get_unique_objects,
)

from .topology import (
    get_terminal_from_cubicle,
    get_terminal_connected_distributed_generators,
    get_opposite_terminal,
)


def get_unique_distributed_generators_from_terminals(terminals):
    """
    Collects all unique distributed generators connected to a list of terminals.
    """

    l_terminals = terminals
    l_distributed_generators = []

    for o_terminal in l_terminals:
        l_distributed_generators.extend(
            get_terminal_connected_distributed_generators(o_terminal)
        )

    return get_unique_objects(l_distributed_generators)


def get_distributed_generator_capacity_mva(dg):
    """
    Reads the installed apparent power of a distributed generator in MVA.
    The function tries all configured DG capacity attribute candidates and
    returns the first available value.
    """

    o_dg = dg

    for s_attribute_name in PFAttr.DG_CAPACITY_CANDIDATES:
        f_capacity_mva = get_pf_attribute(
            o_dg,
            s_attribute_name,
            None,
            float,
        )

        if f_capacity_mva is not None:
            return f_capacity_mva

    return 0.0


def get_connected_terminal_for_distributed_generator(o_dg):
    """
    Returns the terminal connected to the distributed generator bus1 cubicle.
    """

    o_dg_cubicle = get_pf_attribute(
        o_dg,
        PFAttr.BUS1,
    )

    return get_terminal_from_cubicle(o_dg_cubicle)


def get_dg_summary_from_terminals(terminals):
    """
    Builds a summary of all distributed generators connected to a list of terminals.
    The returned dictionary keys are used by other modules and must not be renamed.
    """

    l_terminals = terminals

    l_distributed_generators = get_unique_distributed_generators_from_terminals(
        l_terminals
    )

    f_total_capacity_mva = round(
        sum(
            get_distributed_generator_capacity_mva(o_dg)
            for o_dg in l_distributed_generators
        ),
        3,
    )

    s_distributed_generator_names = " -> ".join(
        get_safe_name(o_dg)
        for o_dg in l_distributed_generators
    )

    return {
        "has_dg": get_boolean_value(len(l_distributed_generators) > 0),
        "count": len(l_distributed_generators),
        "capacity_mva": f_total_capacity_mva,
        "names": s_distributed_generator_names,
        "objects": l_distributed_generators,
    }


def get_ordered_terminals_along_protected_corridor(corridor):
    """
    Returns the ordered terminal path along a protected corridor.
    The path starts at the relay busbar and follows each line section until the
    opposite terminal can no longer be found.
    """

    d_corridor = corridor

    l_ordered_terminals = []

    o_current_terminal = d_corridor.get("relay_busbar")

    if not o_current_terminal:
        return l_ordered_terminals

    l_ordered_terminals.append(o_current_terminal)

    for o_line in d_corridor.get("line_sections", []):
        o_next_terminal = get_opposite_terminal(
            o_line,
            o_current_terminal,
        )

        if not o_next_terminal:
            break

        l_ordered_terminals.append(o_next_terminal)
        o_current_terminal = o_next_terminal

    return l_ordered_terminals


def summarize_dg_by_corridor_location(corridor, shortest_branch):
    """
    Summarizes distributed generation by location relative to a protected corridor.
    The returned dictionary groups DGs into:
    - relay busbar
    - subsequent busbar
    - protected corridor internal terminals
    - downstream branch internal terminals
    - remote busbar
    The returned dictionary keys are used by case_features.py and must not be renamed.
    """

    d_corridor = corridor
    d_shortest_branch = shortest_branch

    o_relay_busbar = d_corridor.get("relay_busbar")
    o_subsequent_busbar = d_corridor.get("subsequent_busbar")

    l_corridor_terminals = get_ordered_terminals_along_protected_corridor(
        d_corridor
    )

    l_branch_terminals = d_shortest_branch.get(
        "Branch Terminals",
        [],
    )

    l_internal_corridor_terminals = [
        o_terminal
        for o_terminal in l_corridor_terminals
        if o_terminal not in (o_relay_busbar, o_subsequent_busbar)
    ]

    l_internal_downstream_branch_terminals = (
        l_branch_terminals[1:-1]
        if len(l_branch_terminals) > 2
        else []
    )

    l_remote_busbar_terminals = (
        [l_branch_terminals[-1]]
        if len(l_branch_terminals) > 1
        else []
    )

    return {
        "relay_busbar": get_dg_summary_from_terminals(
            [o_relay_busbar] if o_relay_busbar else []
        ),
        "subsequent_busbar": get_dg_summary_from_terminals(
            [o_subsequent_busbar] if o_subsequent_busbar else []
        ),
        "protected_corridor": get_dg_summary_from_terminals(
            l_internal_corridor_terminals
        ),
        "downstream_branch": get_dg_summary_from_terminals(
            l_internal_downstream_branch_terminals
        ),
        "remote_busbar": get_dg_summary_from_terminals(
            l_remote_busbar_terminals
        ),
    }

# topology.py
from __future__ import annotations

from ..core.config import PFAttr

from ..pf_api.pf_utils import (
    get_safe_name,
    get_safe_class_name,
    get_safe_full_name,
    get_pf_attribute,
    get_unique_objects,
    is_object_in_service,
)


def get_terminal_from_cubicle(cubicle):
    o_cubicle = cubicle

    return get_pf_attribute(o_cubicle, PFAttr.CTERM)


def get_line_terminals(line):
    o_line = line

    o_bus1_cubicle = get_pf_attribute(o_line, PFAttr.BUS1)
    o_bus2_cubicle = get_pf_attribute(o_line, PFAttr.BUS2)

    o_bus1_terminal = get_terminal_from_cubicle(o_bus1_cubicle)
    o_bus2_terminal = get_terminal_from_cubicle(o_bus2_cubicle)

    return (
        o_bus1_terminal,
        o_bus2_terminal,
        o_bus1_cubicle,
        o_bus2_cubicle,
    )


def get_opposite_terminal(line, terminal):
    o_line = line
    o_terminal = terminal

    (
        o_terminal_1,
        o_terminal_2,
        _o_cubicle_1,
        _o_cubicle_2,
    ) = get_line_terminals(o_line)

    if o_terminal_1 == o_terminal:
        return o_terminal_2

    if o_terminal_2 == o_terminal:
        return o_terminal_1

    return None


def get_line_impedance(line):
    o_line = line

    f_line_r_ohm = get_pf_attribute(
        o_line,
        PFAttr.LINE_R,
        0.0,
        float,
    )

    f_line_x_ohm = get_pf_attribute(
        o_line,
        PFAttr.LINE_X,
        0.0,
        float,
    )

    return f_line_r_ohm, f_line_x_ohm


def get_line_length_value(line):
    o_line = line

    f_line_length_km = get_pf_attribute(
        o_line,
        PFAttr.LINE_LENGTH,
        0.0,
        float,
    )

    return f_line_length_km


def get_cubicle_switch_closed_state(cubicle):
    o_cubicle = cubicle

    if o_cubicle is None:
        return 0

    try:
        l_switches = o_cubicle.GetChildren(1, "*.StaSwitch") or []

        if l_switches:
            i_switch_state = get_pf_attribute(
                l_switches[0],
                PFAttr.SWITCH_STATE,
                1,
                int,
            )

            return 1 if i_switch_state == 1 else 0


    except Exception:
        pass

    return 1


def get_line_is_available_for_topology(line):
    o_line = line

    if o_line is None or not is_object_in_service(o_line):
        return False

    (
        _o_terminal_1,
        _o_terminal_2,
        o_cubicle_1,
        o_cubicle_2,
    ) = get_line_terminals(o_line)

    b_cubicle_1_switch_closed = (
            get_cubicle_switch_closed_state(o_cubicle_1) == 1
    )

    b_cubicle_2_switch_closed = (
            get_cubicle_switch_closed_state(o_cubicle_2) == 1
    )

    return b_cubicle_1_switch_closed and b_cubicle_2_switch_closed


def get_distributed_generator_is_available_for_topology(dg):
    o_dg = dg

    if o_dg is None or not is_object_in_service(o_dg):
        return False

    o_dg_cubicle = get_pf_attribute(o_dg, PFAttr.BUS1)

    return get_cubicle_switch_closed_state(o_dg_cubicle) == 1


def get_terminal_is_busbar(terminal):
    o_terminal = terminal

    i_terminal_usage = get_pf_attribute(
        o_terminal,
        PFAttr.TERMINAL_USAGE,
        0,
        int,
    )

    return i_terminal_usage == 0


def get_terminal_is_junction_node(terminal):
    o_terminal = terminal

    i_terminal_usage = get_pf_attribute(
        o_terminal,
        PFAttr.TERMINAL_USAGE,
        0,
        int,
    )

    return i_terminal_usage == 1


def get_terminal_connected_elements(terminal):
    o_terminal = terminal

    try:
        return list(o_terminal.GetConnectedElements())


    except Exception:
        return []


def get_terminal_connected_lines(terminal):
    o_terminal = terminal

    l_connected_elements = get_terminal_connected_elements(o_terminal)

    return [
        o_element
        for o_element in l_connected_elements
        if (
                get_safe_class_name(o_element) == "ElmLne"
                and get_line_is_available_for_topology(o_element)
        )
    ]


def get_terminal_connected_distributed_generators(terminal):
    o_terminal = terminal

    l_connected_elements = get_terminal_connected_elements(o_terminal)

    return [
        o_element
        for o_element in l_connected_elements
        if (
                get_safe_class_name(o_element) in ["ElmGenstat", "ElmPvsys"]
                and get_distributed_generator_is_available_for_topology(o_element)
        )
    ]


def get_object_is_relay(obj):
    o_object = obj

    s_class_name_and_object_name = (
            get_safe_class_name(o_object).lower()
            + get_safe_name(o_object).lower()
    )

    l_relay_name_markers = [
        "relay",
        "rel",
        "reldis",
        "distance",
        "reloc",
        "elmrelay",
    ]

    return any(
        s_marker in s_class_name_and_object_name
        for s_marker in l_relay_name_markers
    )


def get_cubicle_for_line_at_terminal(line, terminal):
    o_line = line
    o_terminal = terminal

    (
        o_terminal_1,
        o_terminal_2,
        o_cubicle_1,
        o_cubicle_2,
    ) = get_line_terminals(o_line)

    if o_terminal_1 == o_terminal:
        return o_cubicle_1

    if o_terminal_2 == o_terminal:
        return o_cubicle_2

    return None


def get_relay_id_from_terminal_and_line(terminal, line):
    o_terminal = terminal
    o_line = line

    o_cubicle = get_cubicle_for_line_at_terminal(
        o_line,
        o_terminal,
    )

    if o_cubicle:
        try:
            l_cubicle_contents = o_cubicle.GetContents("*", 1)


        except Exception:
            l_cubicle_contents = []

        l_relays = [
            o_element
            for o_element in l_cubicle_contents
            if get_object_is_relay(o_element)
        ]

        if l_relays:
            return get_safe_name(l_relays[0])

    return (
        f"relay_node_{get_safe_name(o_terminal)}"
        f"_line_{get_safe_name(o_line)}"
    )


def line_connects_terminal_pair(line, term_a_name, term_b_name):
    o_line = line
    s_terminal_a_name = term_a_name
    s_terminal_b_name = term_b_name

    (
        o_terminal_1,
        o_terminal_2,
        _o_cubicle_1,
        _o_cubicle_2,
    ) = get_line_terminals(o_line)

    if not o_terminal_1 or not o_terminal_2:
        return False

    return {
        get_safe_full_name(o_terminal_1),
        get_safe_full_name(o_terminal_2),
    } == {
        s_terminal_a_name,
        s_terminal_b_name,
    }


def get_parallel_lines_for_line(line):
    o_line = line

    if not o_line:
        return []

    (
        o_terminal_1,
        o_terminal_2,
        _o_cubicle_1,
        _o_cubicle_2,
    ) = get_line_terminals(o_line)

    if not o_terminal_1 or not o_terminal_2:
        return []

    s_terminal_1_full_name = get_safe_full_name(o_terminal_1)
    s_terminal_2_full_name = get_safe_full_name(o_terminal_2)

    l_parallel_line_candidates = [
        o_candidate_line
        for o_candidate_line in get_terminal_connected_lines(o_terminal_1)
        if (
                o_candidate_line != o_line
                and line_connects_terminal_pair(
            o_candidate_line,
            s_terminal_1_full_name,
            s_terminal_2_full_name,
        )
        )
    ]

    return get_unique_objects(l_parallel_line_candidates)


def get_parallel_lines_for_line_list(lines):
    l_lines = lines

    l_parallel_lines = []

    for o_line in l_lines:
        l_parallel_lines.extend(
            get_parallel_lines_for_line(o_line)
        )

    return get_unique_objects(l_parallel_lines)


def get_branch_line_names(lines):
    l_lines = lines

    return " | ".join(
        [
            get_safe_name(o_line)
            for o_line in l_lines
            if o_line
        ]
    )

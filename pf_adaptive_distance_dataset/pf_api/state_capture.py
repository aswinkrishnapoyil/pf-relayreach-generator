# state_capture.py
from __future__ import annotations

from ..core.config import PFAttr

from .pf_utils import (
    get_safe_full_name,
    get_pf_attribute,
    get_required_binary_attribute,
    get_required_pf_attribute,
    get_unique_objects,
    is_object_inside_grid,
)

from ..domain.topology import (
    get_cubicle_switch_closed_state,
    get_terminal_from_cubicle,
    get_line_terminals,
    get_line_impedance,
    get_line_length_value,
)

from ..pipeline.switch_states import (
    build_cubicle_lookup,
    get_first_cubicle_switch,
    get_switch_state_outserv_controlled_objects,
)


def capture_bus_attributes(app, grid):
    """
    Builds a lookup of bus-level attributes for all terminals inside the grid.
    Called once per slave case, while PowerFactory objects are live.
    Returns: dict  loc_name -> attribute dict
    """
    from .pf_utils import get_safe_name, is_object_in_service

    d_bus_attributes = {}

    for o_terminal in app.GetCalcRelevantObjects("*.ElmTerm") or []:
        if not is_object_inside_grid(o_terminal, grid):
            continue

        s_name = get_safe_name(o_terminal)
        i_usage = get_pf_attribute(o_terminal, "iUsage", 0, int)
        f_uknom = get_pf_attribute(o_terminal, "uknom", 0.0, float)

        d_bus_attributes[s_name] = {
            "iUsage": i_usage,
            "uknom_kv": round(f_uknom, 3),
            "has_sync_dg": 0,
            "has_pv_dg": 0,
            "has_xnet": 0,
            "has_transformer_hv": 0,
            "has_transformer_lv": 0,
            "dg_capacity_mva": 0.0,
        }

    for o_dg in app.GetCalcRelevantObjects("*.ElmGenstat") or []:
        if not is_object_in_service(o_dg):
            continue
        o_cub = get_pf_attribute(o_dg, PFAttr.BUS1)
        if get_cubicle_switch_closed_state(o_cub) == 0:
            continue
        s_key = get_safe_name(get_terminal_from_cubicle(o_cub))
        if s_key in d_bus_attributes:
            d_bus_attributes[s_key]["has_sync_dg"] = 1
            for s_attr in PFAttr.DG_CAPACITY_CANDIDATES:
                f_cap = get_pf_attribute(o_dg, s_attr, None, float)
                if f_cap is not None:
                    d_bus_attributes[s_key]["dg_capacity_mva"] += f_cap
                    break

    for o_dg in app.GetCalcRelevantObjects("*.ElmPvsys") or []:
        if not is_object_in_service(o_dg):
            continue
        o_cub = get_pf_attribute(o_dg, PFAttr.BUS1)
        if get_cubicle_switch_closed_state(o_cub) == 0:
            continue
        s_key = get_safe_name(get_terminal_from_cubicle(o_cub))
        if s_key in d_bus_attributes:
            d_bus_attributes[s_key]["has_pv_dg"] = 1
            for s_attr in PFAttr.DG_CAPACITY_CANDIDATES:
                f_cap = get_pf_attribute(o_dg, s_attr, None, float)
                if f_cap is not None:
                    d_bus_attributes[s_key]["dg_capacity_mva"] += f_cap
                    break

    for o_xnet in app.GetCalcRelevantObjects("*.ElmXnet") or []:
        if not is_object_in_service(o_xnet):
            continue
        o_cub = get_pf_attribute(o_xnet, PFAttr.BUS1)
        s_key = get_safe_name(get_terminal_from_cubicle(o_cub))
        if s_key in d_bus_attributes:
            d_bus_attributes[s_key]["has_xnet"] = 1

    for o_tr in app.GetCalcRelevantObjects("*.ElmTr2") or []:
        if not is_object_in_service(o_tr):
            continue
        o_hv_cub = get_pf_attribute(o_tr, PFAttr.BUSHV)
        o_lv_cub = get_pf_attribute(o_tr, PFAttr.BUSLV)
        s_hv = get_safe_name(get_terminal_from_cubicle(o_hv_cub))
        s_lv = get_safe_name(get_terminal_from_cubicle(o_lv_cub))
        if s_hv in d_bus_attributes:
            d_bus_attributes[s_hv]["has_transformer_hv"] = 1
        if s_lv in d_bus_attributes:
            d_bus_attributes[s_lv]["has_transformer_lv"] = 1

    return d_bus_attributes


def capture_original_state_from_active_slave(grid, app):
    """
    Capture original line, DG, switch, and outserv states
    from the currently active slave study case/scenario.
    This must be called AFTER:
    - slave study case is created
    - slave operation scenario is created
    - both are activated
    """

    o_grid = grid
    o_app = app

    # ------------------------------------------------------------
    # 1. Capture all relevant lines in the target grid
    # ------------------------------------------------------------
    l_lines = []

    for o_line in o_app.GetCalcRelevantObjects("*.ElmLne") or []:
        try:
            (
                o_terminal_1,
                o_terminal_2,
                _o_cubicle_1,
                _o_cubicle_2,
            ) = get_line_terminals(o_line)

            if not (
                is_object_inside_grid(o_terminal_1, o_grid)
                or is_object_inside_grid(o_terminal_2, o_grid)
            ):
                continue

            f_line_length_km = get_line_length_value(o_line)

            if f_line_length_km <= 0:
                continue

            l_lines.append(o_line)

        except Exception:
            continue

    l_lines = get_unique_objects(l_lines)

    d_original_line_states = {}

    for o_line in l_lines:
        f_line_r_ohm, f_line_x_ohm = get_line_impedance(o_line)

        d_original_line_states[get_safe_full_name(o_line)] = {
            "obj": o_line,
            "l": get_line_length_value(o_line),
            "r": f_line_r_ohm,
            "x": f_line_x_ohm,

            # Original shared line type. This must be restored after each scenario.
            "typ_id": get_required_pf_attribute(o_line, PFAttr.LINE_TYPE),
            "temporary_line_types": [],
        }

    # ------------------------------------------------------------
    # 2. Capture all DG capacities in the target grid
    # ------------------------------------------------------------
    l_distributed_generators = []

    for s_object_filter in ["*.ElmGenstat", "*.ElmPvsys"]:
        for o_dg in o_app.GetCalcRelevantObjects(s_object_filter) or []:
            try:
                o_dg_cubicle = get_pf_attribute(o_dg, PFAttr.BUS1)
                o_dg_terminal = get_terminal_from_cubicle(o_dg_cubicle)

                if is_object_inside_grid(o_dg_terminal, o_grid):
                    l_distributed_generators.append(o_dg)

            except Exception:
                continue

    l_distributed_generators = get_unique_objects(l_distributed_generators)

    d_original_dg_states = {}

    for o_dg in l_distributed_generators:
        for s_capacity_attribute_name in PFAttr.DG_CAPACITY_CANDIDATES:
            f_capacity_mva = get_pf_attribute(
                o_dg,
                s_capacity_attribute_name,
                None,
                float,
            )

            if f_capacity_mva is not None:
                d_original_dg_states[get_safe_full_name(o_dg)] = {
                    "obj": o_dg,
                    "attr": s_capacity_attribute_name,
                    "cap": f_capacity_mva,
                }
                break

    # ------------------------------------------------------------
    # 3. Capture switch states
    # ------------------------------------------------------------
    d_cubicle_lookup = build_cubicle_lookup(o_app, o_grid)

    d_original_switch_states = {}

    for s_rdf_identifier, o_cubicle in d_cubicle_lookup.items():
        o_switch = get_first_cubicle_switch(o_cubicle)

        if o_switch is not None:
            i_original_switch_state = get_required_binary_attribute(o_switch, PFAttr.SWITCH_STATE)

            d_original_switch_states[s_rdf_identifier] = {
                "sw": o_switch,
                "v": i_original_switch_state,
            }

    # ------------------------------------------------------------
    # 4. Capture outserv states
    # ------------------------------------------------------------
    l_outserv_controlled_objects = get_switch_state_outserv_controlled_objects(
        o_grid,
        o_app,
    )

    d_original_outserv_states = {
        o_object: get_required_binary_attribute(o_object, PFAttr.OUTSERV)
        for o_object in l_outserv_controlled_objects
    }

    l_cached_all_grid_dg = l_distributed_generators

    d_bus_attributes = capture_bus_attributes(o_app, o_grid)

    return (
        d_cubicle_lookup,
        d_original_line_states,
        d_original_dg_states,
        d_original_outserv_states,
        d_original_switch_states,
        l_outserv_controlled_objects,
        l_cached_all_grid_dg,
        d_bus_attributes,
    )

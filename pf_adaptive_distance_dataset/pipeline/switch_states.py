# switch_states.py
from __future__ import annotations

import os
import uuid

import pandas as pd

from ..core.config import Config, PFAttr, SWITCH_STATE_FILE
from ..core.models import PowerFactoryStateError

from ..pf_api.pf_utils import (
    get_pf_attribute,
    get_safe_class_name,
    get_safe_full_name,
    get_safe_name,
    set_pf_attribute_verified,
    verify_pf_attribute,
    get_required_binary_attribute,
    get_unique_objects,
    is_object_inside_grid,
)


def _get_terminal_from_cubicle(cubicle):
    """
    Returns the terminal connected to a cubicle.
    """

    o_cubicle = cubicle

    return get_pf_attribute(o_cubicle, PFAttr.CTERM)


def _get_cubicle_switch_closed_state(cubicle):
    """
    Returns the first cubicle switch state as an integer flag.

    Returns:
        1 if the cubicle switch is closed or no switch is found.
        0 if the cubicle is missing or the switch is open.
    """

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


def _clean_cim_rdf_id(obj):
    """
    Returns a normalised CIM RDF ID for a PowerFactory object.
    """

    s_rdf_id = get_pf_attribute(obj, "cimRdfId")

    if isinstance(s_rdf_id, list):
        s_rdf_id = s_rdf_id[0] if s_rdf_id else ""

    return str(s_rdf_id or "").lstrip("_").strip()


def _get_active_project_name(app):
    """
    Returns the active project name used by generated switch-state IDs.
    """

    try:
        return get_safe_name(app.GetActiveProject())

    except Exception:
        return ""


def _get_generated_switch_identifier(switch, cubicle, project_name, grid_name):
    """
    Rebuilds the deterministic UUID used by manual switch-state libraries when
    PowerFactory objects do not expose native CIM RDF IDs.
    """

    if switch is None or cubicle is None:
        return ""

    s_identity_string = (
        "powerfactory://"
        f"{project_name}|"
        f"{grid_name}|"
        "switch="
        f"{get_safe_class_name(switch)}:"
        f"{get_safe_full_name(switch)}|"
        "reference="
        f"{get_safe_class_name(cubicle)}:"
        f"{get_safe_full_name(cubicle)}"
    )

    return str(uuid.uuid5(uuid.NAMESPACE_URL, s_identity_string))


def _add_cubicle_lookup_key(lookup, key, cubicle):
    """
    Adds a non-empty switch-state key to the cubicle lookup.
    """

    s_key = str(key or "").lstrip("_").strip()

    if s_key:
        if s_key in lookup and lookup[s_key] != cubicle:
            raise PowerFactoryStateError(f"Ambiguous switch identifier {s_key!r}")
        lookup[s_key] = cubicle


def load_switch_state_dataframe():
    """
    Loads switch-state scenarios from the configured switch-state file.

    Returns:
        tuple:
            - switch-state DataFrame
            - list of switch column names
    """

    if not Config.ENABLE_SWITCH_STATE_SCENARIOS:
        return pd.DataFrame([{"ConfigID": "live_grid_state"}]), []

    if not os.path.exists(SWITCH_STATE_FILE):
        raise RuntimeError(f"Switch file missing: {SWITCH_STATE_FILE}")

    df_switch_states = pd.read_csv(
        SWITCH_STATE_FILE,
        sep=None,
        engine="python",
    )

    l_switch_columns = [
        s_column
        for s_column in df_switch_states.columns
        if str(s_column).startswith("switch_")
    ]

    if not l_switch_columns:
        raise PowerFactoryStateError("Switch-state CSV has no switch_ columns.")
    for s_switch_column in l_switch_columns:
        sr_values = pd.to_numeric(df_switch_states[s_switch_column], errors="coerce")
        sr_invalid = ~sr_values.isin([0, 1])
        if sr_invalid.any():
            l_rows = [int(i_index) + 1 for i_index in df_switch_states.index[sr_invalid]]
            raise PowerFactoryStateError(
                f"Switch-state CSV requires 0/1 values: column={s_switch_column!r}, "
                f"data rows={l_rows[:10]}"
            )
        df_switch_states[s_switch_column] = sr_values.astype(int)

    if Config.MAX_SWITCH_STATE_CONFIG_COUNT:
        df_switch_states = df_switch_states.head(
            Config.MAX_SWITCH_STATE_CONFIG_COUNT
        )

    return df_switch_states, l_switch_columns


def build_cubicle_lookup(app, grid=None):
    """
    Builds a lookup dictionary from CIM RDF ID to PowerFactory cubicle object.
    If grid is provided, only cubicles whose parent terminal is inside that
    grid are included — this prevents MV/LV grid cubicles from being toggled
    when applying 110 kV switch-state configurations.
    """

    o_app = app
    o_grid = grid
    d_cubicle_lookup = {}
    s_project_name = _get_active_project_name(o_app)
    s_grid_name = get_safe_name(o_grid)

    l_cubicles = o_app.GetCalcRelevantObjects("*.StaCubic") or []

    for o_cubicle in l_cubicles:

        # If a grid filter is provided, skip cubicles outside it
        if o_grid is not None:
            o_terminal = _get_terminal_from_cubicle(o_cubicle)
            if o_terminal is not None and not is_object_inside_grid(o_terminal, o_grid):
                continue

        o_switch = get_first_cubicle_switch(o_cubicle)

        _add_cubicle_lookup_key(
            d_cubicle_lookup,
            _clean_cim_rdf_id(o_cubicle),
            o_cubicle,
        )

        _add_cubicle_lookup_key(
            d_cubicle_lookup,
            _clean_cim_rdf_id(o_switch),
            o_cubicle,
        )

        _add_cubicle_lookup_key(
            d_cubicle_lookup,
            _get_generated_switch_identifier(
                o_switch,
                o_cubicle,
                s_project_name,
                s_grid_name,
            ),
            o_cubicle,
        )

    return d_cubicle_lookup


def get_first_cubicle_switch(cubicle):
    """
    Returns the first switch object connected to a cubicle.
    """

    o_cubicle = cubicle

    l_switches = (
        o_cubicle.GetChildren(1, "*.StaSwitch")
        if o_cubicle
        else []
    )

    if l_switches and len(l_switches) > 1:
        raise PowerFactoryStateError(
            f"Ambiguous cubicle {get_safe_full_name(o_cubicle)}: multiple StaSwitch objects"
        )
    return l_switches[0] if l_switches else None


def apply_switch_state(row, lookup, cfg):
    """
    Applies one switch-state row to the PowerFactory cubicle switches.
    """

    d_switch_state_row = row
    d_cubicle_lookup = lookup
    s_switch_state_config_id = cfg

    # Resolve and validate the complete request before changing any switch.
    d_requested = {}
    for s_column_name, switch_value in d_switch_state_row.items():
        if not str(s_column_name).startswith("switch_"):
            continue

        s_rdf_id = str(s_column_name).split("_", 1)[1].lstrip("_").strip()

        o_switch = get_first_cubicle_switch(
            d_cubicle_lookup.get(s_rdf_id)
        )

        if o_switch is None:
            raise PowerFactoryStateError(
                f"{s_switch_state_config_id}: unmapped switch {s_column_name!r}"
            )
        if switch_value not in (0, 1):
            raise PowerFactoryStateError(
                f"{s_switch_state_config_id}: invalid switch value {switch_value!r} for {s_column_name}"
            )
        i_expected = int(switch_value)
        s_switch_id = get_safe_full_name(o_switch)
        if s_switch_id in d_requested and d_requested[s_switch_id][1] != i_expected:
            raise PowerFactoryStateError(f"{s_switch_state_config_id}: conflicting aliases for {s_switch_id}")
        get_required_binary_attribute(o_switch, PFAttr.SWITCH_STATE)
        d_requested[s_switch_id] = (o_switch, i_expected)

    for o_switch, i_expected in d_requested.values():
        set_pf_attribute_verified(o_switch, PFAttr.SWITCH_STATE, i_expected)
    # Also check the final state after all switches have been applied.
    for o_switch, i_expected in d_requested.values():
        verify_pf_attribute(o_switch, PFAttr.SWITCH_STATE, i_expected)


def get_switch_state_outserv_controlled_objects(grid, app):
    """
    Collects grid objects whose outserv state can be controlled by switch states.
    """

    o_grid = grid
    o_app = app

    l_outserv_controlled_objects = []

    l_class_filters = [
        "*.ElmLne",
        "*.ElmGenstat",
        "*.ElmPvsys",
        "*.ElmLod",
        "*.ElmLodlv",
        "*.ElmTr2",
        "*.ElmTr3",
    ]

    for s_class_filter in l_class_filters:
        for o_object in o_app.GetCalcRelevantObjects(s_class_filter) or []:
            l_cubicles = [
                get_pf_attribute(o_object, s_attribute_name)
                for s_attribute_name in PFAttr.CUBICLE_ATTR_CANDIDATES
            ]

            l_valid_cubicles = [
                o_cubicle
                for o_cubicle in l_cubicles
                if o_cubicle is not None
            ]

            for o_cubicle in l_valid_cubicles:
                o_terminal = _get_terminal_from_cubicle(o_cubicle)

                if is_object_inside_grid(o_terminal, o_grid):
                    l_outserv_controlled_objects.append(o_object)
                    break

    return get_unique_objects(l_outserv_controlled_objects)


def apply_outserv_for_components_behind_open_switches(objs, orig_states):
    """
    Sets objects out of service if any connected cubicle switch is open.

    Objects that were originally out of service are left unchanged.
    """

    l_outserv_controlled_objects = objs
    d_original_outserv_states = orig_states

    for o_object in l_outserv_controlled_objects:
        if d_original_outserv_states.get(o_object, 0) != 0:
            continue

        l_cubicles = []

        for s_attribute_name in PFAttr.CUBICLE_ATTR_CANDIDATES:
            o_cubicle = get_pf_attribute(o_object, s_attribute_name)

            if o_cubicle is not None:
                l_cubicles.append(o_cubicle)

        if any(
            _get_cubicle_switch_closed_state(o_cubicle) == 0
            for o_cubicle in l_cubicles
        ):
            set_pf_attribute_verified(
                o_object,
                PFAttr.OUTSERV,
                1,
            )

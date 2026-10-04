# grid_state.py
from __future__ import annotations

from ..core.config import PFAttr
from ..core.models import PowerFactoryStateError
from .pf_utils import get_safe_full_name, set_pf_attribute_verified, verify_pf_attribute


def _delete_temporary_powerfactory_object(obj) -> None:
    """Delete a detached temporary type, reporting unsuccessful deletion."""
    l_errors = []
    for s_method_name in ["Delete", "DeleteObject"]:
        try:
            i_result = getattr(obj, s_method_name)()
            if i_result not in (None, 0):
                raise RuntimeError(f"{s_method_name} returned {i_result!r}")
            return
        except Exception as o_error:
            l_errors.append(str(o_error))
    raise PowerFactoryStateError(
        f"Cannot delete temporary type {get_safe_full_name(obj)}: {'; '.join(l_errors)}"
    )


def restore_grid_state(orig_lines, orig_dgs, orig_out, orig_sw):
    """Attempt every baseline restoration; any failure stops the worker."""
    l_settings = []
    l_errors = []
    for d_line_state in orig_lines.values():
        o_line = d_line_state.get("obj")
        o_original_type = d_line_state.get("typ_id")
        if o_line is None or o_original_type is None or "l" not in d_line_state:
            l_errors.append("Incomplete original line state")
            continue
        l_settings.extend([
            (o_line, PFAttr.LINE_TYPE, o_original_type),
            (o_line, PFAttr.LINE_LENGTH, float(d_line_state["l"])),
        ])

    for d_dg_state in orig_dgs.values():
        if any(d_dg_state.get(s_key) is None for s_key in ("obj", "attr", "cap")):
            l_errors.append("Incomplete original DG state")
            continue
        l_settings.append((d_dg_state["obj"], d_dg_state["attr"], float(d_dg_state["cap"])))
    l_settings.extend((o_obj, PFAttr.OUTSERV, i_value) for o_obj, i_value in orig_out.items())
    for d_switch_state in orig_sw.values():
        if d_switch_state.get("sw") is None or d_switch_state.get("v") not in (0, 1):
            l_errors.append("Incomplete original switch state")
            continue
        l_settings.append((d_switch_state["sw"], PFAttr.SWITCH_STATE, d_switch_state["v"]))

    for o_object, s_attr, value in l_settings:
        try:
            set_pf_attribute_verified(o_object, s_attr, value)
        except Exception as o_error:
            l_errors.append(str(o_error))

    # Check the final state, including settings that another write could affect.
    for o_object, s_attr, value in l_settings:
        try:
            verify_pf_attribute(o_object, s_attr, value)
        except Exception as o_error:
            l_errors.append(str(o_error))

    # Keep failed/unverified clones tracked; never delete a type still in use.
    for d_line_state in orig_lines.values():
        l_temporary_types = d_line_state.get("temporary_line_types", [])
        if not l_temporary_types:
            continue
        try:
            verify_pf_attribute(d_line_state["obj"], PFAttr.LINE_TYPE, d_line_state["typ_id"])
        except Exception as o_error:
            l_errors.append(str(o_error))
            continue
        l_remaining = []
        for o_type in l_temporary_types:
            try:
                _delete_temporary_powerfactory_object(o_type)
            except Exception as o_error:
                l_errors.append(str(o_error))
                l_remaining.append(o_type)
        d_line_state["temporary_line_types"] = l_remaining

    if l_errors:
        raise PowerFactoryStateError("Grid restoration failed: " + "; ".join(l_errors))

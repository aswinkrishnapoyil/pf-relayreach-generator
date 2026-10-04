# pf_utils.py
from __future__ import annotations

import math
from typing import Any

from ..core.config import PFAttr
from ..core.models import PowerFactoryStateError


def get_boolean_value(b_value: Any) -> int:
    """
    Converts any truthy/falsy value into an integer flag.
    Returns:
        1 if value is truthy.
        0 if value is falsy.
    """

    b_input_value = bool(b_value)

    return 1 if b_input_value else 0


def get_safe_name(obj) -> str:
    """
    Safely returns the PowerFactory object's loc_name.
    Returns 'UNKNOWN' if the object is missing or the attribute cannot be read.
    """

    o_object = obj

    try:
        return str(o_object.loc_name) if o_object else "UNKNOWN"

    except Exception:
        return "UNKNOWN"


def get_safe_class_name(obj) -> str:
    """
    Safely returns the PowerFactory object's class name.
    Returns 'UNKNOWN' if the object is missing or the class name cannot be read.
    """

    o_object = obj

    try:
        return str(o_object.GetClassName()) if o_object else "UNKNOWN"

    except Exception:
        return "UNKNOWN"


def get_safe_full_name(obj) -> str:
    """
    Safely returns the PowerFactory object's full project-tree name.
    Falls back to get_safe_name() if the full name cannot be read.
    """

    o_object = obj

    try:
        return str(o_object.GetFullName()) if o_object else get_safe_name(o_object)

    except Exception:
        return get_safe_name(o_object)


def get_pf_attribute(obj, attr: str, default=None, cast_type=None):
    """
    Safely reads a PowerFactory object attribute.
    The function first tries PowerFactory's GetAttribute() method.
    If that fails, it falls back to normal Python getattr() access.
    If cast_type is provided, the value is cast before returning.
    If the attribute cannot be read or cast, the default value is returned.
    """

    o_object = obj
    s_attribute_name = attr
    default_value = default
    cast_function = cast_type

    if o_object is None:
        return default_value

    try:
        attribute_value = o_object.GetAttribute(s_attribute_name)

    except Exception:
        try:
            attribute_value = getattr(o_object, s_attribute_name)

        except Exception:
            return default_value

    if attribute_value is None:
        return default_value

    if cast_function:
        try:
            return cast_function(attribute_value)

        except Exception:
            return default_value

    return attribute_value


def safe_set_attribute(obj, attr: str, value) -> bool:
    """
    Safely sets a PowerFactory object attribute.
    The function first tries PowerFactory's SetAttribute() method.
    If that fails, it falls back to normal Python setattr() access.
    Returns:
        True if the attribute was set successfully.
        False otherwise.
    """

    o_object = obj
    s_attribute_name = attr
    attribute_value = value

    if o_object is None:
        return False

    try:
        o_object.SetAttribute(
            s_attribute_name,
            attribute_value,
        )
        return True

    except Exception:
        try:
            setattr(
                o_object,
                s_attribute_name,
                attribute_value,
            )
            return True

        except Exception:
            return False


def get_required_pf_attribute(obj, attr: str):
    """Read a required live value without substituting a default."""
    o_missing = object()
    value = get_pf_attribute(obj, attr, o_missing)
    if value is o_missing:
        raise PowerFactoryStateError(
            f"Cannot read {get_safe_full_name(obj)}.{attr}"
        )
    return value


def verify_pf_attribute(obj, attr: str, expected) -> None:
    """Check flags/type references exactly and floating-point settings with tolerance."""
    actual = get_required_pf_attribute(obj, attr)
    if isinstance(expected, float):
        try:
            # Live dline read-back can be rounded to float32 precision.
            # Allow that relative roundoff, not a materially different setting.
            b_matches = (
                math.isfinite(float(actual)) and math.isfinite(expected)
                and math.isclose(float(actual), expected, rel_tol=1e-7, abs_tol=1e-9)
            )
        except (TypeError, ValueError):
            b_matches = False
    else:
        b_matches = actual == expected
    if not b_matches:
        raise PowerFactoryStateError(
            f"Read-back mismatch for {get_safe_full_name(obj)}.{attr}: "
            f"expected={expected!r}, actual={actual!r}"
        )


def set_pf_attribute_verified(obj, attr: str, value) -> bool:
    """Write a required setting and confirm the live value before continuing."""
    if not safe_set_attribute(obj, attr, value):
        raise PowerFactoryStateError(f"Cannot write {get_safe_full_name(obj)}.{attr}")
    verify_pf_attribute(obj, attr, value)
    return True


def get_required_binary_attribute(obj, attr: str) -> int:
    value = get_required_pf_attribute(obj, attr)
    if value not in (0, 1):
        raise PowerFactoryStateError(
            f"Expected 0/1 for {get_safe_full_name(obj)}.{attr}, got {value!r}"
        )
    return int(value)


def get_unique_objects(obj_list: list) -> list:
    """
    Removes duplicate PowerFactory objects using their full names.
    The original order is preserved.
    """

    l_object_list = obj_list

    l_unique_objects = []
    s_seen_full_names = set()

    for o_object in l_object_list:
        s_full_name = get_safe_full_name(o_object)

        if s_full_name not in s_seen_full_names:
            s_seen_full_names.add(s_full_name)
            l_unique_objects.append(o_object)

    return l_unique_objects


def is_object_in_service(obj) -> bool:
    """
    Checks whether a PowerFactory object is in service.
    PowerFactory convention:
        outserv = 0 means in service.
        outserv = 1 means out of service.
    If the state cannot be read, the function assumes the object is in service.
    """

    o_object = obj

    try:
        i_outserv_state = int(
            get_pf_attribute(
                o_object,
                PFAttr.OUTSERV,
                0,
                int,
            )
        )

        return i_outserv_state == 0

    except Exception:
        return True


def is_object_inside_grid(obj, grid) -> bool:
    """
    Checks whether a PowerFactory object belongs to or is located inside a grid.
    The function walks up the parent hierarchy and compares full object names.
    As fallback, it checks whether the grid full name appears inside the
    object's full name.
    """

    o_object = obj
    o_grid = grid

    try:
        if o_object is None or o_grid is None:
            return False

        s_grid_full_name = o_grid.GetFullName()
        o_parent = o_object

        while o_parent is not None:
            if o_parent.GetFullName() == s_grid_full_name:
                return True

            o_parent = o_parent.GetParent()

        return s_grid_full_name in o_object.GetFullName()

    except Exception:
        return False

# randomization.py
from __future__ import annotations

import logging
import random
from typing import Optional

from ..core.config import Config, PFAttr

from ..pf_api.pf_utils import (
    get_safe_name,
    get_safe_class_name,
    get_safe_full_name,
    get_pf_attribute,
    safe_set_attribute,
    set_pf_attribute_verified,
)

logger = logging.getLogger(__name__)


def _get_first_numeric_attribute(obj, attrs: list[str]):
    o_object = obj
    l_attribute_names = attrs

    for s_attribute_name in l_attribute_names:
        f_value = get_pf_attribute(
            o_object,
            s_attribute_name,
            None,
            float,
        )

        if f_value is not None:
            return s_attribute_name, f_value

    return "", None


def _copy_powerfactory_object(obj, name: str):
    o_object = obj
    s_new_name = name

    try:
        o_parent = o_object.GetParent()

    except Exception:
        o_parent = None

    if o_parent is None:
        return None

    for t_args in [(o_object, s_new_name), (o_object,)]:
        try:
            o_copy = o_parent.AddCopy(*t_args)

            if o_copy is not None:
                safe_set_attribute(o_copy, "loc_name", s_new_name)
                return o_copy

        except Exception:
            pass

    return None


def _build_line_rx_ratio_randomization(
    line,
    line_state: dict,
    random_generator,
) -> dict:
    o_line = line
    d_line_state = line_state
    o_random_generator = random_generator

    d_log = {
        "line_type_class": "",
        "rx_ratio_randomization_applied": 0,
        "rx_ratio_randomization_skipped_reason": "",
        "original_type_r_per_km": None,
        "original_type_x_per_km": None,
        "original_rx_ratio": None,
        "rx_scale_factor": None,
        "rx_scale_max_allowed": None,
        "randomized_type_r_per_km": None,
        "randomized_type_x_per_km": None,
        "randomized_rx_ratio": None,
        "temporary_line_type_created": 0,
        "temporary_line_type_id": "",
        "line_type_assignment_write_ok": False,
        "type_r_write_ok": False,
    }

    if not Config.ENABLE_LINE_RX_RATIO_RANDOMIZATION:
        d_log["rx_ratio_randomization_skipped_reason"] = "config_disabled"
        return d_log

    o_original_line_type = d_line_state.get(
        "typ_id",
        get_pf_attribute(o_line, PFAttr.LINE_TYPE),
    )

    if o_original_line_type is None:
        d_log["rx_ratio_randomization_skipped_reason"] = "missing_line_type"
        return d_log

    s_line_type_class = get_safe_class_name(o_original_line_type)
    d_log["line_type_class"] = s_line_type_class

    if s_line_type_class != "TypLne":
        d_log["rx_ratio_randomization_skipped_reason"] = s_line_type_class
        return d_log

    (
        s_type_r_attribute_name,
        f_original_type_r_per_km,
    ) = _get_first_numeric_attribute(
        o_original_line_type,
        PFAttr.LINE_TYPE_R_CANDIDATES,
    )

    (
        _s_type_x_attribute_name,
        f_original_type_x_per_km,
    ) = _get_first_numeric_attribute(
        o_original_line_type,
        PFAttr.LINE_TYPE_X_CANDIDATES,
    )

    d_log["original_type_r_per_km"] = (
        round(f_original_type_r_per_km, 6)
        if f_original_type_r_per_km is not None
        else None
    )
    d_log["original_type_x_per_km"] = (
        round(f_original_type_x_per_km, 6)
        if f_original_type_x_per_km is not None
        else None
    )

    if (
        not s_type_r_attribute_name
        or f_original_type_r_per_km is None
        or f_original_type_x_per_km is None
        or f_original_type_x_per_km <= 0.0
    ):
        d_log["rx_ratio_randomization_skipped_reason"] = "invalid_type_rx"
        return d_log

    f_base_rx_ratio = f_original_type_r_per_km / f_original_type_x_per_km
    d_log["original_rx_ratio"] = round(f_base_rx_ratio, 6)

    if f_base_rx_ratio <= 0.0:
        d_log["rx_ratio_randomization_skipped_reason"] = "invalid_type_rx"
        return d_log

    f_rx_scale_upper = min(
        float(Config.LINE_RX_SCALE_MAX),
        float(Config.LINE_RX_RATIO_MAX) / f_base_rx_ratio,
    )

    d_log["rx_scale_max_allowed"] = round(f_rx_scale_upper, 6)

    if f_rx_scale_upper <= 0.0:
        d_log["rx_ratio_randomization_skipped_reason"] = "invalid_rx_cap"
        return d_log

    if f_rx_scale_upper < float(Config.LINE_RX_SCALE_MIN):
        f_rx_scale_factor = f_rx_scale_upper

    else:
        f_rx_scale_factor = o_random_generator.uniform(
            float(Config.LINE_RX_SCALE_MIN),
            f_rx_scale_upper,
        )

    f_randomized_type_r_per_km = (
        f_original_type_r_per_km * f_rx_scale_factor
    )

    f_randomized_rx_ratio = (
        f_randomized_type_r_per_km / f_original_type_x_per_km
    )

    s_temporary_line_type_name = (
        f"{get_safe_name(o_original_line_type)}"
        f"__rx_{get_safe_name(o_line)}"
    )

    o_temporary_line_type = _copy_powerfactory_object(
        o_original_line_type,
        s_temporary_line_type_name,
    )

    if o_temporary_line_type is None:
        d_log["rx_ratio_randomization_skipped_reason"] = "copy_failed"
        return d_log

    d_line_state.setdefault("temporary_line_types", []).append(
        o_temporary_line_type
    )

    d_log["temporary_line_type_created"] = 1
    d_log["temporary_line_type_id"] = get_safe_full_name(
        o_temporary_line_type
    )

    b_line_type_assignment_ok = set_pf_attribute_verified(
        o_line,
        PFAttr.LINE_TYPE,
        o_temporary_line_type,
    )

    d_log["line_type_assignment_write_ok"] = b_line_type_assignment_ok

    b_type_r_write_ok = set_pf_attribute_verified(
        o_temporary_line_type,
        s_type_r_attribute_name,
        f_randomized_type_r_per_km,
    )

    d_log["type_r_write_ok"] = b_type_r_write_ok

    d_log["rx_ratio_randomization_applied"] = 1
    d_log["rx_ratio_randomization_skipped_reason"] = ""
    d_log["rx_scale_factor"] = round(f_rx_scale_factor, 6)
    d_log["randomized_type_r_per_km"] = round(
        f_randomized_type_r_per_km,
        6,
    )
    d_log["randomized_type_x_per_km"] = round(
        f_original_type_x_per_km,
        6,
    )
    d_log["randomized_rx_ratio"] = round(f_randomized_rx_ratio, 6)

    return d_log


def apply_random_line_parameter_scenario(
    orig_lines: dict,
    scenario_id: str,
    seed: int,
    scale_min: Optional[float] = None,
    scale_max: Optional[float] = None,
) -> list[dict]:
    """
    Applies approved line randomization.
    All line types receive line-length randomization.
    TypLne objects additionally receive R/X-ratio randomization through a
    temporary cloned line type. TypTow objects remain length-only because
    their impedance values are geometry-derived.
    """

    d_original_line_states = orig_lines
    s_scenario_id = scenario_id
    i_random_seed = seed

    f_scale_min = scale_min
    f_scale_max = scale_max

    if f_scale_min is None:
        f_scale_min = Config.LINE_LENGTH_SCALE_MIN

    if f_scale_max is None:
        f_scale_max = Config.LINE_LENGTH_SCALE_MAX

    o_random_generator = random.Random(int(i_random_seed))

    l_randomization_logs = []

    for s_line_key, d_line_state in d_original_line_states.items():
        o_line = d_line_state.get("obj")

        if o_line is None:
            continue

        f_original_length_km = float(
            d_line_state.get(
                "l",
                get_pf_attribute(
                    o_line,
                    PFAttr.LINE_LENGTH,
                    0.0,
                    float,
                ),
            )
            or 0.0
        )

        f_original_r_ohm = float(
            d_line_state.get(
                "r",
                get_pf_attribute(
                    o_line,
                    PFAttr.LINE_R,
                    0.0,
                    float,
                ),
            )
            or 0.0
        )

        f_original_x_ohm = float(
            d_line_state.get(
                "x",
                get_pf_attribute(
                    o_line,
                    PFAttr.LINE_X,
                    0.0,
                    float,
                ),
            )
            or 0.0
        )

        if f_original_length_km <= 0.0:
            continue

        f_length_scale_factor = o_random_generator.uniform(
            float(f_scale_min),
            float(f_scale_max),
        )

        f_randomized_length_km = (
            f_original_length_km * f_length_scale_factor
        )

        b_length_set_ok = set_pf_attribute_verified(
            o_line,
            PFAttr.LINE_LENGTH,
            f_randomized_length_km,
        )

        d_rx_ratio_log = _build_line_rx_ratio_randomization(
            o_line,
            d_line_state,
            o_random_generator,
        )

        f_effective_line_r_ohm_after = get_pf_attribute(
            o_line,
            PFAttr.LINE_R,
            None,
            float,
        )

        f_effective_line_x_ohm_after = get_pf_attribute(
            o_line,
            PFAttr.LINE_X,
            None,
            float,
        )

        d_log_row = {
            "scenario_id": s_scenario_id,
            "seed": i_random_seed,
            "object_id": s_line_key,
            "object_name": get_safe_name(o_line),
            "object_full_name": get_safe_full_name(o_line),

            "randomization_mode": (
                "line_length_and_typ_lne_rx_ratio"
                if d_rx_ratio_log.get("rx_ratio_randomization_applied")
                else "line_length_only"
            ),

            "length_scale_factor": round(f_length_scale_factor, 6),

            "original_length_km": round(f_original_length_km, 6),
            "randomized_length_km": round(f_randomized_length_km, 6),
            "line_length_write_ok": b_length_set_ok,

            "original_r_ohm": round(f_original_r_ohm, 6),
            "original_x_ohm": round(f_original_x_ohm, 6),

            "effective_line_r_ohm_after": (
                round(f_effective_line_r_ohm_after, 6)
                if f_effective_line_r_ohm_after is not None
                else None
            ),
            "effective_line_x_ohm_after": (
                round(f_effective_line_x_ohm_after, 6)
                if f_effective_line_x_ohm_after is not None
                else None
            ),
        }

        d_log_row.update(d_rx_ratio_log)

        l_randomization_logs.append(d_log_row)

    logger.info(
        f"Applied line parameter randomization for {s_scenario_id}: "
        f"{len(l_randomization_logs)} lines, "
        f"seed={i_random_seed}, "
        f"range=({f_scale_min}, {f_scale_max})"
    )

    return l_randomization_logs


def apply_random_dg_capacity_scenario(
    orig_dgs: dict,
    scenario_id: str,
    seed: int,
    scale_min: Optional[float] = None,
    scale_max: Optional[float] = None,
) -> list[dict]:
    """Applies random DG capacity scaling."""

    d_original_dg_states = orig_dgs
    s_scenario_id = scenario_id
    i_random_seed = seed

    f_scale_min = scale_min
    f_scale_max = scale_max

    if f_scale_min is None:
        f_scale_min = Config.DG_CAPACITY_SCALE_MIN

    if f_scale_max is None:
        f_scale_max = Config.DG_CAPACITY_SCALE_MAX

    o_random_generator = random.Random(int(i_random_seed))

    l_randomization_logs = []

    for s_dg_key, d_dg_state in d_original_dg_states.items():
        o_dg = d_dg_state.get("obj")
        s_capacity_attribute_name = d_dg_state.get("attr")
        f_original_capacity_mva = d_dg_state.get("cap")

        if (
            o_dg is None
            or s_capacity_attribute_name is None
            or f_original_capacity_mva is None
        ):
            continue

        f_original_capacity_mva = float(f_original_capacity_mva)

        f_scale_factor = o_random_generator.uniform(
            float(f_scale_min),
            float(f_scale_max),
        )

        f_randomized_capacity_mva = (
            f_original_capacity_mva
            * f_scale_factor
        )

        set_pf_attribute_verified(
            o_dg,
            s_capacity_attribute_name,
            f_randomized_capacity_mva,
        )

        l_randomization_logs.append(
            {
                "scenario_id": s_scenario_id,
                "seed": i_random_seed,
                "object_id": s_dg_key,
                "object_name": get_safe_name(o_dg),
                "object_full_name": get_safe_full_name(o_dg),
                "capacity_attribute": s_capacity_attribute_name,
                "scale_factor": round(f_scale_factor, 6),
                "original_capacity_mva": round(f_original_capacity_mva, 6),
                "randomized_capacity_mva": round(
                    f_randomized_capacity_mva,
                    6,
                ),
            }
        )

    logger.info(
        f"Applied DG capacity randomization for {s_scenario_id}: "
        f"{len(l_randomization_logs)} DGs, "
        f"seed={i_random_seed}, "
        f"range=({f_scale_min}, {f_scale_max})"
    )

    return l_randomization_logs

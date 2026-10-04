# infeed.py
from __future__ import annotations

import math
from ..core.config import PFAttr
from ..core.models import (
    DatasetIntegrityError,
    PowerFactoryStateError,
    ShortCircuitCalculationError,
    ShortCircuitCurrentError,
    ShortCircuitExecutionError,
)

from ..pf_api.pf_utils import (
    get_safe_name,
    get_pf_attribute,
    set_pf_attribute_verified,
    verify_pf_attribute,
    get_required_binary_attribute,
    get_unique_objects,
    is_object_in_service,
    is_object_inside_grid,
)

from .topology import (
    get_terminal_from_cubicle,
    get_opposite_terminal,
    get_line_length_value,
    get_line_impedance,
    get_cubicle_switch_closed_state,
    get_distributed_generator_is_available_for_topology,
    get_terminal_is_busbar,
    get_terminal_is_junction_node,
)

from .dg_utils import (
    get_unique_distributed_generators_from_terminals,
    get_distributed_generator_capacity_mva,
    get_connected_terminal_for_distributed_generator,
)


def split_protected_corridor_turbines_by_zone_reach(
    d_corridor,
    l_turbines,
    f_reach_fraction,
):
    """
    Splits distributed generators on the protected corridor into Zone 1 and Zone 2 candidate groups.
    The function builds an ordered path from the relay busbar along the protected corridor lines.
    The Zone 1 boundary is calculated as:
        Zone 1 distance = reach_fraction * total protected corridor distance

    Each DG is assigned based on its terminal position along this ordered path:
    - DGs at or before the Zone 1 boundary are returned as Zone 1 turbines.
    - DGs beyond the Zone 1 boundary are returned as Zone 2 turbines.

    Returns:
        tuple[list, list]:
            First list contains Zone 1 DG objects.
            Second list contains Zone 2 DG objects.
    """
    o_relay_busbar = d_corridor.get("relay_busbar")
    l_corridor_lines = d_corridor.get("line_sections", [])

    l_path_segments = build_ordered_path_segments(
        o_relay_busbar,
        l_corridor_lines,
    )

    if not l_path_segments:
        return [], []

    f_zone1_boundary_distance_km = (
        float(f_reach_fraction or 0.0)
        * l_path_segments[-1].get("end_distance_km", 0.0)
    )

    l_zone1_turbines = []
    l_zone2_turbines = []

    for o_dg in l_turbines:
        o_dg_terminal = get_connected_terminal_for_distributed_generator(o_dg)

        f_dg_distance_km = get_terminal_distance_on_ordered_path(
            o_dg_terminal,
            l_path_segments,
        )

        if f_dg_distance_km is None:
            continue

        if f_dg_distance_km <= f_zone1_boundary_distance_km + 1e-9:
            l_zone1_turbines.append(o_dg)
        else:
            l_zone2_turbines.append(o_dg)

    return get_unique_objects(l_zone1_turbines), get_unique_objects(l_zone2_turbines)


def get_short_circuit_command(o_project, o_app):
    """
    Finds the PowerFactory short-circuit calculation command object.
    The function first tries to retrieve the active ComShc object from the current study case.
    If that fails, it searches the project for an object named 'Short-Circuit Calculation*'.

    Returns:
        PowerFactory object or None:
            The short-circuit command object if found; otherwise None.
    """
    try:
        o_shc = o_app.GetFromStudyCase("ComShc")

        if o_shc:
            return o_shc

    except Exception:
        pass

    try:
        l_shc_objects = o_project.GetContents("Short-Circuit Calculation*", 1)

        if l_shc_objects:
            return l_shc_objects[0]

    except Exception:
        pass

    return None


def configure_short_circuit_command(o_shc):
    """
    Configures the PowerFactory short-circuit command before execution.
    Available settings are written and read back. Attribute availability
    can vary between PowerFactory versions and study-case templates.
    This function does not execute the short-circuit calculation. It only prepares
    the ComShc object.
    """
    if not o_shc:
        return

    l_shc_settings = [
        ("iopt_allbus", 0),
        ("iopt_mode", 3),
        ("iopt_mde", 3),
        ("ip_flt", 0),
        ("iopt_opt", "pro"),
        ("iopt_cur", 0),
    ]

    for s_attribute_name, attribute_value in l_shc_settings:
        if get_pf_attribute(o_shc, s_attribute_name, None) is not None:
            set_pf_attribute_verified(o_shc, s_attribute_name, attribute_value)


def execute_short_circuit_at_line_fault(
    o_project,
    o_app,
    o_fault_line,
    f_fault_percent,
    f_local_fault_distance_km=None,
):
    """
    Executes a PowerFactory short-circuit calculation at a selected location on a line.
    The function:
    1. Retrieves the ComShc short-circuit command.
    2. Configures the short-circuit command.
    3. Sets the faulted line as the short-circuit object.
    4. Sets the fault location using both percentage-based and distance-based
       attributes where available.
    5. Executes the short-circuit calculation.

    Returns:
        bool:
            True if the short-circuit calculation executed successfully.
            False otherwise.
    """
    if not o_fault_line:
        raise PowerFactoryStateError("No fault line was provided for short-circuit calculation")

    if not math.isfinite(float(f_fault_percent)) or not 0.0 <= float(f_fault_percent) <= 100.0:
        raise ShortCircuitCalculationError("Invalid fault percentage")
    if f_local_fault_distance_km is not None and (
        not math.isfinite(float(f_local_fault_distance_km)) or f_local_fault_distance_km < 0.0
    ):
        raise ShortCircuitCalculationError("Invalid fault distance")

    o_shc = get_short_circuit_command(o_project, o_app)

    if not o_shc:
        raise PowerFactoryStateError("PowerFactory short-circuit command is unavailable")

    configure_short_circuit_command(o_shc)

    if not set_pf_attribute_verified(o_shc, "shcobj", o_fault_line):
        raise PowerFactoryStateError("Could not set the short-circuit fault line")

    b_location_set = False
    for s_location_attribute in ["ppro", "relpos", "xloc", "fltloc", "loc_fault"]:
        if get_pf_attribute(o_shc, s_location_attribute, None) is not None:
            b_location_set = (
                set_pf_attribute_verified(o_shc, s_location_attribute, float(f_fault_percent))
                or b_location_set
            )

    if f_local_fault_distance_km is not None and get_pf_attribute(o_shc, "faultloc", None) is not None:
        b_location_set = (
            set_pf_attribute_verified(
                o_shc,
                "faultloc",
                float(f_local_fault_distance_km or 0.0),
            )
            or b_location_set
        )

    if not b_location_set:
        raise ShortCircuitCalculationError("No supported fault-location attribute could be verified")

    try:
        return o_shc.Execute() == 0

    except Exception:
        return False


def get_ikss_value(o_object):
    """
    Reads the initial symmetrical short-circuit current Ikss from a PowerFactory object.
    The function tries the common PowerFactory result attributes in this order:
    1. m:Ikss:bus1
    2. m:Ikss:bus2
    3. m:Ikss

    This function is used for reading both DG Ikss and fallback cubicle Ikss values.

    Returns:
        float:
            Ikss value in kA if available; otherwise None (not a measured zero).
    """
    for s_ikss_attribute_name in [PFAttr.IKSS_BUS1, PFAttr.IKSS_BUS2, PFAttr.IKSS]:
        f_ikss_ka = get_pf_attribute(
            o_object,
            s_ikss_attribute_name,
            None,
            float,
        )

        if f_ikss_ka is not None:
            return f_ikss_ka

    return None


def get_relay_side_and_cubicle(o_line, o_start_terminal):
    """
    Determines which side of a protected line is connected to the relay/start terminal.
    The function checks the bus1 and bus2 cubicles of the line and compares their
    connected terminals with the given start terminal.
    This is needed because relay-side short-circuit current should be read from the
    correct side-specific result attribute:
    - m:Ikss:bus1 if the relay is on bus1
    - m:Ikss:bus2 if the relay is on bus2

    Returns:
        tuple:(relay-side cubicle object or None, relay-side cubicle name,
               relay-side Ikss attribute name,relay-side cubicle attribute name)
    """
    try:
        o_bus1_cubicle = get_pf_attribute(o_line, PFAttr.BUS1)
        o_bus2_cubicle = get_pf_attribute(o_line, PFAttr.BUS2)

        o_bus1_terminal = get_terminal_from_cubicle(o_bus1_cubicle)
        o_bus2_terminal = get_terminal_from_cubicle(o_bus2_cubicle)

        if o_bus1_terminal == o_start_terminal:
            return (
                o_bus1_cubicle,
                get_safe_name(o_bus1_cubicle),
                PFAttr.IKSS_BUS1,
                PFAttr.BUS1,
            )

        if o_bus2_terminal == o_start_terminal:
            return (
                o_bus2_cubicle,
                get_safe_name(o_bus2_cubicle),
                PFAttr.IKSS_BUS2,
                PFAttr.BUS2,
            )

    except Exception:
        pass

    return None, "UNKNOWN", PFAttr.IKSS_BUS1, "unknown"


def read_relay_reference_ikss(
    o_reference_cubicle,
    o_reference_line,
    s_relay_ikss_line_attribute,
):
    """
    Reads the relay/reference short-circuit current Ikss after a PowerFactory SHC run.
    The function tries to read the reference Ikss in this order:
    1. Side-specific line result, such as m:Ikss:bus1 or m:Ikss:bus2.
    2. Generic line result m:Ikss.
    3. Cubicle result attributes defined in PFAttr.IKSS_CANDIDATES.

    This value is used as the denominator for the DG infeed ratio:
        Ikss ratio = DG Ikss / relay reference Ikss

    Returns:
        tuple[float, str]:
            Reference Ikss value in kA and a string describing where it was read from.
    """
    f_reference_ikss_ka = get_pf_attribute(
        o_reference_line,
        s_relay_ikss_line_attribute,
        None,
        float,
    )

    if f_reference_ikss_ka is not None:
        return f_reference_ikss_ka, f"line.{s_relay_ikss_line_attribute}"

    f_reference_ikss_ka = get_pf_attribute(
        o_reference_line,
        PFAttr.IKSS,
        None,
        float,
    )

    if f_reference_ikss_ka is not None:
        return f_reference_ikss_ka, f"line.{PFAttr.IKSS}"

    if o_reference_cubicle:
        for s_ikss_attribute_name in PFAttr.IKSS_CANDIDATES:
            f_reference_ikss_ka = get_pf_attribute(
                o_reference_cubicle,
                s_ikss_attribute_name,
                None,
                float,
            )

            if f_reference_ikss_ka is not None:
                return f_reference_ikss_ka, f"cubicle.{s_ikss_attribute_name}"

    return 0.0, "NOT_FOUND"


def build_ordered_path_segments(o_start_terminal, l_lines):
    """
    Builds an ordered path representation from a start terminal and a list of lines.
    Each path segment stores:
    - line object
    - start terminal
    - end terminal
    - cumulative start distance in km
    - cumulative end distance in km
    - line length in km
    - line resistance in ohm
    - line reactance in ohm

    The ordered path is later used to locate DG terminals, zone boundaries, and fault positions.

    Returns:
        list[dict]:
            List of path segment dictionaries.
    """
    l_path_segments = []

    if not o_start_terminal or not l_lines:
        return l_path_segments

    o_current_terminal = o_start_terminal
    f_cumulative_distance_km = 0.0

    for o_line in l_lines:
        o_next_terminal = get_opposite_terminal(o_line, o_current_terminal)

        if not o_next_terminal:
            break

        f_line_length_km = get_line_length_value(o_line)
        f_line_r_ohm, f_line_x_ohm = get_line_impedance(o_line)

        l_path_segments.append(
            {
                "line": o_line,
                "start_terminal": o_current_terminal,
                "end_terminal": o_next_terminal,
                "start_distance_km": f_cumulative_distance_km,
                "end_distance_km": f_cumulative_distance_km + f_line_length_km,
                "length_km": f_line_length_km,
                "r_ohm": f_line_r_ohm,
                "x_ohm": f_line_x_ohm,
            }
        )

        f_cumulative_distance_km += f_line_length_km
        o_current_terminal = o_next_terminal

    return l_path_segments


def get_terminal_distance_on_ordered_path(o_terminal, l_path_segments):
    """
    Finds the cumulative distance of a terminal on an ordered path.
    The function checks whether the terminal appears as the start or end terminal
    of any path segment. If found, it returns the corresponding cumulative distance
    from the relay/start terminal.

    Returns:
        float or None:
            Distance in km from the start terminal if found.
            None if the terminal is not on the path.
    """
    if not o_terminal:
        return None

    for d_segment in l_path_segments:
        if o_terminal == d_segment["start_terminal"]:
            return d_segment["start_distance_km"]

        if o_terminal == d_segment["end_terminal"]:
            return d_segment["end_distance_km"]

    return None


def calculate_path_impedance_between_distances(
    l_path_segments,
    f_from_distance_km,
    f_to_distance_km,
):
    """
    Calculates R/X impedance between two distances on an ordered path.
    The function determines which parts of the path lie between the two distances.
    For each overlapping line segment, it adds the proportional R and X impedance:
        segment contribution = full segment impedance * overlap fraction

    This is used to calculate the impedance between a DG location and the selected
    short-circuit fault point.

    Returns:
        tuple[float, float]:
            R and X impedance between the two distances in ohm.
    """
    if f_from_distance_km is None or f_to_distance_km is None:
        return 0.0, 0.0

    f_start_distance_km = min(f_from_distance_km, f_to_distance_km)

    f_end_distance_km = max(f_from_distance_km, f_to_distance_km)

    f_total_r_ohm = 0.0
    f_total_x_ohm = 0.0

    for d_segment in l_path_segments:
        f_overlap_start_km = max(
            f_start_distance_km,
            d_segment["start_distance_km"],
        )

        f_overlap_end_km = min(
            f_end_distance_km,
            d_segment["end_distance_km"],
        )

        if (
            f_overlap_end_km <= f_overlap_start_km
            or d_segment["length_km"] <= 0.0
        ):
            continue

        f_overlap_fraction = (
            (f_overlap_end_km - f_overlap_start_km)
            / d_segment["length_km"]
        )

        f_total_r_ohm += d_segment["r_ohm"] * f_overlap_fraction
        f_total_x_ohm += d_segment["x_ohm"] * f_overlap_fraction

    return round(f_total_r_ohm, 3), round(f_total_x_ohm, 3)


def select_fault_location_by_reach_impedance(
    l_path_segments,
    f_target_reach_r_ohm,
    f_target_reach_x_ohm,
):
    """
    Selects the zone boundary location on the ordered path based on target reach X.
    The function walks along the path and accumulates line reactance until the
    target zone X reach is reached. The location is interpolated within the line
    segment where the target X lies.
    The R reach value is accepted as an argument for interface completeness, but
    the current selection logic uses X as the primary reach coordinate.
    Assumption: this is valid for high-voltage grids (≥110 kV) where the R/X
    ratio of lines is typically small (< 0.3). For resistive distribution or
    sub-transmission grids with high R/X, the fault point may be placed slightly
    beyond the true zone boundary in the R dimension, causing a small positive
    bias in the in-feed correction. Extend to complex impedance magnitude if
    the dataset is later extended to MV grids.

    Returns:
        tuple:
            (
                fault line object,
                fault percentage on that line,
                local fault distance in km on that line,
                cumulative fault distance in km from the start terminal
            )
    """
    if not l_path_segments:
        return None, 0.0, 0.0, 0.0

    f_target_reach_x_ohm = max(0.0, float(f_target_reach_x_ohm or 0.0))

    f_cumulative_x_ohm = 0.0

    for d_segment in l_path_segments:
        f_segment_x_ohm = float(d_segment.get("x_ohm", 0.0))

        f_segment_length_km = float(d_segment.get("length_km", 0.0))

        f_segment_start_distance_km = float(
            d_segment.get("start_distance_km", 0.0)
        )

        if (
            f_segment_x_ohm > 0.0
            and f_cumulative_x_ohm + f_segment_x_ohm >= f_target_reach_x_ohm
        ):
            f_remaining_x_ohm = f_target_reach_x_ohm - f_cumulative_x_ohm

            f_fraction_on_segment = f_remaining_x_ohm / f_segment_x_ohm

            f_fraction_on_segment = max(0.0, min(1.0, f_fraction_on_segment))

            f_local_fault_distance_km = (
                f_fraction_on_segment * f_segment_length_km
            )

            f_fault_percent = 100.0 * f_fraction_on_segment

            f_fault_distance_km = (
                f_segment_start_distance_km + f_local_fault_distance_km
            )

            return (
                d_segment["line"],
                f_fault_percent,
                f_local_fault_distance_km,
                f_fault_distance_km,
            )

        f_cumulative_x_ohm += f_segment_x_ohm

    d_last_segment = l_path_segments[-1]

    return (
        d_last_segment["line"],
        100.0,
        d_last_segment.get("length_km", 0.0),
        d_last_segment.get("end_distance_km", 0.0),
    )


def select_shc_fault_location_for_dg_context(o_dg_terminal, l_path_segments):
    """
    Selects a short-circuit fault location for a specific DG contribution study.
    The rule depends on where the DG is connected:

    1. If the DG is connected to a busbar:
       The fault is placed approximately halfway along the downstream branch below
       that busbar.
    2. If the DG is connected to a junction node:
       The fault is placed at 50% of the next relevant line section.

    The returned location is used by execute_short_circuit_at_line_fault().
    Returns:
        dict or None:
            Fault context dictionary containing:
            - fault_line
            - fault_percent
            - local_fault_distance_km
            - fault_distance_km
            - fault_rule

            Returns None if no valid fault location can be selected.
    """
    if not o_dg_terminal or not l_path_segments:
        return None

    if get_terminal_is_busbar(o_dg_terminal):
        l_downstream_segments = []
        b_collecting_downstream_segments = False

        for d_segment in l_path_segments:
            if not b_collecting_downstream_segments:
                if d_segment.get("start_terminal") != o_dg_terminal:
                    continue

                b_collecting_downstream_segments = True

            l_downstream_segments.append(d_segment)

            o_end_terminal = d_segment.get("end_terminal")

            if (
                o_end_terminal != o_dg_terminal
                and get_terminal_is_busbar(o_end_terminal)
            ):
                break

        if not l_downstream_segments:
            return None

        f_total_downstream_length_km = sum(
            float(d_segment.get("length_km", 0.0))
            for d_segment in l_downstream_segments
        )

        if f_total_downstream_length_km <= 0.0:
            return None

        f_midpoint_distance_km = 0.5 * f_total_downstream_length_km
        f_accumulated_length_km = 0.0

        for d_segment in l_downstream_segments:
            f_segment_length_km = float(d_segment.get("length_km", 0.0))

            if (
                f_accumulated_length_km + f_segment_length_km
            ) >= f_midpoint_distance_km:
                f_local_fault_distance_km = (
                    f_midpoint_distance_km - f_accumulated_length_km
                )

                f_fault_percent = (
                    100.0 * f_local_fault_distance_km / f_segment_length_km
                    if f_segment_length_km > 0.0
                    else 0.0
                )

                return {
                    "fault_line": d_segment.get("line"),
                    "fault_percent": f_fault_percent,
                    "local_fault_distance_km": f_local_fault_distance_km,
                    "fault_distance_km": (
                        float(d_segment.get("start_distance_km", 0.0))
                        + f_local_fault_distance_km
                    ),
                    "fault_rule": "busbar_dg",
                }

            f_accumulated_length_km += f_segment_length_km

        d_last_segment = l_downstream_segments[-1]

        return {
            "fault_line": d_last_segment.get("line"),
            "fault_percent": 100.0,
            "local_fault_distance_km": float(
                d_last_segment.get("length_km", 0.0)
            ),
            "fault_distance_km": float(
                d_last_segment.get("end_distance_km", 0.0)
            ),
            "fault_rule": "busbar_dg",
        }

    if get_terminal_is_junction_node(o_dg_terminal):
        i_start_segment_index = next(
            (
                i_index
                for i_index, d_segment in enumerate(l_path_segments)
                if (
                    d_segment.get("start_terminal") == o_dg_terminal
                    or d_segment.get("end_terminal") == o_dg_terminal
                )
            ),
            None,
        )

        if i_start_segment_index is None:
            return None

        if l_path_segments[i_start_segment_index].get("end_terminal") == o_dg_terminal:
            i_start_segment_index += 1

        d_last_line_section = None

        for d_segment in l_path_segments[i_start_segment_index:]:
            d_last_line_section = d_segment

            if get_terminal_is_busbar(d_segment.get("end_terminal")):
                break

        if not d_last_line_section:
            return None

        f_local_fault_distance_km = 0.5 * float(
            d_last_line_section.get("length_km", 0.0)
        )

        return {
            "fault_line": d_last_line_section.get("line"),
            "fault_percent": 50.0,
            "local_fault_distance_km": f_local_fault_distance_km,
            "fault_distance_km": (
                float(d_last_line_section.get("start_distance_km", 0.0))
                + f_local_fault_distance_km
            ),
            "fault_rule": "junction_dg",
        }

    return None


def get_all_grid_distributed_generators(o_grid, o_app):
    """
    Collects all in-service distributed generators inside the selected grid.
    The function searches for PowerFactory objects of type:
    - ElmGenstat
    - ElmPvsys
    A DG is included only if:
    - it is in service,
    - its cubicle switch is closed,
    - its connected terminal is inside the selected grid.

    Returns:
        list:
            Unique list of distributed generator objects.
    """
    l_distributed_generators = []

    for s_object_filter in ["*.ElmGenstat", "*.ElmPvsys"]:
        try:
            l_objects = o_app.GetCalcRelevantObjects(s_object_filter) or []

        except Exception:
            l_objects = []

        for o_dg in l_objects:
            if not is_object_in_service(o_dg):
                continue

            o_dg_cubicle = get_pf_attribute(o_dg, PFAttr.BUS1)

            if get_cubicle_switch_closed_state(o_dg_cubicle) == 0:
                continue

            o_dg_terminal = get_terminal_from_cubicle(o_dg_cubicle)

            if is_object_inside_grid(o_dg_terminal, o_grid):
                l_distributed_generators.append(o_dg)

    return get_unique_objects(l_distributed_generators)


def get_original_outserv_states(l_objects):
    """
    Stores the original outserv state of a list of PowerFactory objects.
    This is used before temporarily switching DGs in or out of service during the
    one-DG-at-a-time short-circuit contribution calculation.

    Returns:
        dict:
            Dictionary mapping each object to its original outserv value.
    """
    return {
        o_object: get_required_binary_attribute(o_object, PFAttr.OUTSERV)
        for o_object in l_objects
    }


def restore_original_outserv_states(d_original_outserv_states):
    """
    Restores previously stored outserv states for PowerFactory objects.
    This function is called after the one-DG-at-a-time short-circuit calculations
    to return the grid to its previous state.
    """
    l_errors = []
    for o_object, i_original_outserv in d_original_outserv_states.items():
        try:
            set_pf_attribute_verified(o_object, PFAttr.OUTSERV, i_original_outserv)
        except Exception as o_error:
            l_errors.append(str(o_error))
    for o_object, i_original_outserv in d_original_outserv_states.items():
        try:
            verify_pf_attribute(o_object, PFAttr.OUTSERV, i_original_outserv)
        except Exception as o_error:
            l_errors.append(str(o_error))
    if l_errors:
        raise PowerFactoryStateError("DG restoration failed: " + "; ".join(l_errors))


def _get_dg_full_name(o_dg):
    """Use the live PowerFactory path, never a Python ID or display-name fallback."""
    try:
        s_full_name = o_dg.GetFullName()
        if not isinstance(s_full_name, str) or not s_full_name.strip():
            raise ValueError("empty full name")
        return s_full_name
    except Exception as o_error:
        raise PowerFactoryStateError(
            f"Cannot read stable DG identity for {get_safe_name(o_dg)}"
        ) from o_error


def activate_only_one_distributed_generator(l_all_grid_dg, o_active_dg):
    """
    Switches all distributed generators out of service except one selected DG.
    The selected DG is kept in service with:
        outserv = 0
    All other DGs are switched out of service with:
        outserv = 1

    This allows the code to calculate the short-circuit contribution of one DG at a time.
    """
    s_active_dg_full_name = _get_dg_full_name(o_active_dg)
    l_requested_states = [
        (o_dg, 0 if _get_dg_full_name(o_dg) == s_active_dg_full_name else 1)
        for o_dg in l_all_grid_dg
    ]
    if not any(i_outserv == 0 for _, i_outserv in l_requested_states):
        raise PowerFactoryStateError(
            f"Selected generator is not in the DG registry: {s_active_dg_full_name}"
        )
    for o_dg, i_outserv in l_requested_states:
        set_pf_attribute_verified(o_dg, PFAttr.OUTSERV, i_outserv)
    for o_dg, i_outserv in l_requested_states:
        verify_pf_attribute(o_dg, PFAttr.OUTSERV, i_outserv)


def get_object_id_string(l_objects):
    """
    Creates a readable object-name string from a list of PowerFactory objects.
    Duplicate objects are removed before names are joined.
    Example output:
        DG_1 | DG_2 | DG_3

    Returns:
        str:
            Object names joined by ' | '.
    """
    return " | ".join(
        [
            get_safe_name(o_object)
            for o_object in get_unique_objects(l_objects)
            if o_object is not None
        ]
    )


def get_branch_junction_node_id(d_branch):
    """
    Returns a readable string of junction-node names in a downstream branch.
    The function checks the branch terminal list and keeps only terminals classified
    as junction nodes.

    Returns:
        str:
            Junction node names joined by ' | '.
    """
    l_junction_nodes = [
        o_terminal
        for o_terminal in d_branch.get("Branch Terminals", [])
        if get_terminal_is_junction_node(o_terminal)
    ]

    return get_object_id_string(l_junction_nodes)


def get_branch_distributed_generation_id(d_branch):
    """
    Returns a readable string of DG names connected to a downstream branch.
    The function checks all branch terminals, finds connected distributed generators,
    removes duplicates, and joins their names into one string.

    Returns:
        str:
            Distributed generator names joined by ' | '.
    """
    l_distributed_generators = get_unique_distributed_generators_from_terminals(
        d_branch.get("Branch Terminals", [])
    )

    return get_object_id_string(l_distributed_generators)


def calculate_zone_infeed_summary_for_turbines(
    o_project,
    o_app,
    o_grid,
    o_start_terminal,
    o_reference_cubicle,
    o_reference_line,
    s_relay_ikss_line_attr,
    l_turbines,
    l_fault_lines,
    f_fault_reach_r_ohm,
    f_fault_reach_x_ohm,
    f_relay_reference_ikss_ka,
    s_zone_name,
    l_all_grid_dg=None,
):
    """
    Calculates short-circuit based DG in-feed correction for one protection zone.
    The function performs the full in-feed workflow for Zone 1 or Zone 2:

    1. Remove duplicate turbine/DG objects.
    2. Build the ordered path from relay terminal to the selected fault-path end.
    3. Locate the zone boundary based on the target reach impedance.
    4. Keep only DGs that are on the path and inside the zone boundary.
    5. Select a DG-specific downstream fault location.
    6. Save original DG outserv states.
    7. For each valid DG:
       - keep only that DG in service,
       - run PowerFactory short-circuit calculation,
       - read DG Ikss,
       - read relay/reference Ikss,
       - calculate DG contribution ratio,
       - calculate impedance from DG to fault,
       - add R/X in-feed correction.
    8. Restore original DG outserv states.
    9. Return a summary dictionary.

    The main formulas are:
        Ikss ratio = DG Ikss / relay reference Ikss

        R correction += R_DG_to_fault * Ikss ratio
        X correction += X_DG_to_fault * Ikss ratio

    Returns:
        dict:
            Summary containing turbine counts, IDs, Ikss ratios, relay reference
            Ikss, and R/X in-feed correction values.
    """
    d_summary = {
        "turbines_candidate_count": 0,
        "turbines_candidate_id": "",
        "turbines_considered_count": 0,
        "turbines_considered_id": "",
        "turbines_skipped_count": 0,
        "turbines_skipped_id": "",
        "turbines_total_capacity_mva": 0.0,
        "total_ikss_contribution_ratio": 0.0,
        "max_single_ikss_contribution_ratio": 0.0,
        "infeed_correction_r_ohm": 0.0,
        "infeed_correction_x_ohm": 0.0,
        "relay_reference_ikss_ka": 0.0,
    }

    if l_all_grid_dg is None:
        l_all_grid_dg = get_all_grid_distributed_generators(o_grid, o_app)

    # Separate API calls can return different Python wrappers for the same DG.
    # Match full paths and use the registry object for activation and readings.
    d_active_grid_dg_by_name = {
        _get_dg_full_name(o_dg): o_dg
        for o_dg in l_all_grid_dg
        if get_distributed_generator_is_available_for_topology(o_dg)
    }
    l_all_grid_dg = list(d_active_grid_dg_by_name.values())

    d_candidate_dg_by_name = {}
    for o_dg in l_turbines:
        if not get_distributed_generator_is_available_for_topology(o_dg):
            continue
        s_full_name = _get_dg_full_name(o_dg)
        if s_full_name in d_active_grid_dg_by_name:
            d_candidate_dg_by_name[s_full_name] = d_active_grid_dg_by_name[s_full_name]
    l_turbines = list(d_candidate_dg_by_name.values())

    d_summary["turbines_candidate_count"] = len(l_turbines)
    d_summary["turbines_candidate_id"] = get_object_id_string(l_turbines)

    if not l_turbines:
        return d_summary

    l_path_segments = build_ordered_path_segments(o_start_terminal, l_fault_lines)

    if not l_path_segments:
        raise ShortCircuitCalculationError(
            f"{s_zone_name}, relay={get_safe_name(o_reference_line)}: no ordered fault path for eligible DGs"
        )

    _, _, _, f_zone_boundary_distance_km = select_fault_location_by_reach_impedance(
        l_path_segments,
        f_fault_reach_r_ohm,
        f_fault_reach_x_ohm,
    )

    l_valid_turbine_items = []
    l_skipped_turbines = []

    for o_dg in l_turbines:
        o_dg_terminal = get_connected_terminal_for_distributed_generator(o_dg)

        f_dg_distance_km = get_terminal_distance_on_ordered_path(
            o_dg_terminal,
            l_path_segments,
        )

        if (
            f_dg_distance_km is None
            or f_dg_distance_km > f_zone_boundary_distance_km + 1e-9
        ):
            l_skipped_turbines.append(o_dg)
            continue

        d_fault_context = select_shc_fault_location_for_dg_context(
            o_dg_terminal,
            l_path_segments,
        )

        if d_fault_context is None:
            l_skipped_turbines.append(o_dg)
            continue

        f_fault_distance_km = d_fault_context.get("fault_distance_km")

        if (
            f_fault_distance_km is None
            or f_fault_distance_km <= f_dg_distance_km + 1e-9
        ):
            l_skipped_turbines.append(o_dg)
            continue

        l_valid_turbine_items.append(
            {
                "object": o_dg,
                "terminal": o_dg_terminal,
                "distance_km": f_dg_distance_km,
                "fault_context": d_fault_context,
            }
        )

    d_summary["turbines_skipped_count"] = len(
        get_unique_objects(l_skipped_turbines)
    )
    d_summary["turbines_skipped_id"] = get_object_id_string(l_skipped_turbines)

    if not l_valid_turbine_items:
        return d_summary

    d_summary["turbines_considered_count"] = len(l_valid_turbine_items)

    d_summary["turbines_considered_id"] = get_object_id_string(
        [
            d_item["object"]
            for d_item in l_valid_turbine_items
        ]
    )

    d_summary["turbines_total_capacity_mva"] = round(
        sum(
            get_distributed_generator_capacity_mva(d_item["object"])
            for d_item in l_valid_turbine_items
        ),
        3,
    )

    d_original_outserv_states = get_original_outserv_states(l_all_grid_dg)

    l_single_dg_ratios = []

    f_total_correction_r_ohm = 0.0
    f_total_correction_x_ohm = 0.0
    f_last_reference_ikss_ka = 0.0

    try:
        for d_item in l_valid_turbine_items:
            o_dg = d_item["object"]

            s_calculation_context = (
                f"zone={s_zone_name}, relay_bus={get_safe_name(o_start_terminal)}, "
                f"relay_line={get_safe_name(o_reference_line)}, DG={get_safe_name(o_dg)}, "
                f"fault_line={get_safe_name(d_item['fault_context']['fault_line'])}, "
                f"fault_percent={d_item['fault_context']['fault_percent']}"
            )
            try:
                activate_only_one_distributed_generator(l_all_grid_dg, o_dg)
                b_success = execute_short_circuit_at_line_fault(
                    o_project,
                    o_app,
                    d_item["fault_context"]["fault_line"],
                    d_item["fault_context"]["fault_percent"],
                    d_item["fault_context"]["local_fault_distance_km"],
                )
            except DatasetIntegrityError as o_error:
                raise type(o_error)(f"{s_calculation_context}: {o_error}") from o_error

            if not b_success:
                raise ShortCircuitExecutionError(f"{s_calculation_context}: short-circuit execution failed")

            f_dg_ikss_ka = get_ikss_value(o_dg)

            if f_dg_ikss_ka is None:
                f_dg_ikss_ka = get_ikss_value(
                    get_pf_attribute(o_dg, PFAttr.BUS1)
                )

            f_reference_ikss_ka, _ = read_relay_reference_ikss(
                o_reference_cubicle,
                o_reference_line,
                s_relay_ikss_line_attr,
            )

            if f_dg_ikss_ka is None or not math.isfinite(f_dg_ikss_ka) or f_dg_ikss_ka < 0.0:
                raise ShortCircuitCurrentError(f"{s_calculation_context}: missing/invalid DG current")
            if not math.isfinite(f_reference_ikss_ka) or f_reference_ikss_ka <= 0.0:
                raise ShortCircuitCurrentError(
                    f"{s_calculation_context}: missing/invalid current from this fault at the relay"
                )

            f_last_reference_ikss_ka = f_reference_ikss_ka

            f_ikss_ratio = f_dg_ikss_ka / f_reference_ikss_ka

            (
                f_impedance_to_fault_r_ohm,
                f_impedance_to_fault_x_ohm,
            ) = calculate_path_impedance_between_distances(
                l_path_segments,
                d_item["distance_km"],
                d_item["fault_context"]["fault_distance_km"],
            )

            f_total_correction_r_ohm += (
                f_impedance_to_fault_r_ohm * f_ikss_ratio
            )

            f_total_correction_x_ohm += (
                f_impedance_to_fault_x_ohm * f_ikss_ratio
            )

            l_single_dg_ratios.append(f_ikss_ratio)

    finally:
        restore_original_outserv_states(d_original_outserv_states)

    d_summary["relay_reference_ikss_ka"] = round(f_last_reference_ikss_ka, 3)

    d_summary["total_ikss_contribution_ratio"] = round(
        sum(l_single_dg_ratios),
        3,
    )

    d_summary["max_single_ikss_contribution_ratio"] = round(
        max(l_single_dg_ratios) if l_single_dg_ratios else 0.0,
        3,
    )

    d_summary["infeed_correction_r_ohm"] = round(f_total_correction_r_ohm, 3)

    d_summary["infeed_correction_x_ohm"] = round(f_total_correction_x_ohm, 3)

    return d_summary

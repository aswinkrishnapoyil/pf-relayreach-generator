# graph_arrays.py
from __future__ import annotations

import json
import math
from typing import Any

from ..core.models import PhysicalEdge
from ..exports.validation import validate_case

from ..core.dataset_schema import (
    base_reach_columns,
    reach_infeed_correction_columns,
    target_reach_columns,
    directed_numeric_context_columns,
    directed_string_context_columns,
)

from .graph_array_utils import (
    clean_string,
    to_float,
    to_int,
    upper_triangle_index,
    flatten_row_major,
    canonical_corridor_id,
)


BUS_TYPE_CODE_BY_LABEL = {
    "plain_or_tie_bus": 101,
    "sync_dg_bus": 103,
    "pv_or_inverter_dg_bus": 107,
    "mixed_dg_bus": 109,
    "external_grid_or_slack_bus": 113,
    "transformer_hv_bus": 127,
    "transformer_lv_bus": 131,
    "junction_node": 137,
}

BUS_TYPE_LABEL_BY_CODE = {
    i_code: s_label
    for s_label, i_code in BUS_TYPE_CODE_BY_LABEL.items()
}


def collect_numeric_array(rows, col, default=0.0):
    """
    Collects one numeric column from all directed corridor rows.
    """

    l_rows = rows
    s_column_name = col
    f_default_value = default

    return [
        to_float(d_row.get(s_column_name), f_default_value)
        for d_row in l_rows
    ]


def collect_string_array(rows, col, default=""):
    """
    Collects one string column from all directed corridor rows.
    """

    l_rows = rows
    s_column_name = col
    s_default_value = default

    return [
        clean_string(d_row.get(s_column_name), s_default_value)
        for d_row in l_rows
    ]


def parse_json_list(value, field_name=""):
    """
    Parses one JSON-encoded list stored in a flat corridor row.

    Raises an error instead of silently ignoring malformed structural
    graph data.
    """

    if isinstance(value, list):
        return value

    s_value = clean_string(value)

    if not s_value:
        raise ValueError(
            f"Missing required graph structural field: {field_name}"
        )

    try:
        l_value = json.loads(s_value)
    except Exception as o_error:
        raise ValueError(
            f"Invalid JSON in graph structural field "
            f"{field_name!r}: {s_value!r}"
        ) from o_error

    if not isinstance(l_value, list):
        raise ValueError(
            f"Graph structural field {field_name!r} "
            "must contain a JSON list."
        )

    return l_value


def build_bus_index(rows, bus_attributes=None):
    """
    Builds the graph bus index from all physical protected-corridor
    terminals, including internal junction nodes.

    Assigns bus_typ per terminal using the bus_attributes lookup from
    capture_bus_attributes() in state_capture.py.

    bus_typ is a categorical code, not a continuous numeric feature.
    ML code should remap it to contiguous embedding indices or one-hot
    vectors before training.

    bus_typ codes:
      101  Plain bus (load/tie)
      103  Sync DG bus (ElmGenstat)
      107  PV/inverter bus (ElmPvsys)
      109  Mixed DG bus (both)
      113  External grid / slack (ElmXnet)
      127  Transformer HV bus
      131  Transformer LV bus
      137  Junction node (iUsage=1)
    """

    l_rows = rows
    d_bus_attributes = bus_attributes or {}

    s_bus_ids = set()

    for d_row in l_rows:

        # Always retain relay and subsequent busbars.
        s_relay_node_id = clean_string(
            d_row.get("relay_node_id")
        )

        s_subsequent_node_id = clean_string(
            d_row.get("subsequent_node_id")
        )

        if s_relay_node_id:
            s_bus_ids.add(s_relay_node_id)

        if s_subsequent_node_id:
            s_bus_ids.add(s_subsequent_node_id)

        # Add every physical terminal encountered along the protected
        # corridor, including internal junction nodes.
        l_terminal_path_ids = parse_json_list(
            d_row.get(
                "protected_corridor_terminal_path_json"
            ),
            field_name="protected_corridor_terminal_path_json",
        )

        for s_terminal_id in l_terminal_path_ids:
            s_terminal_id = clean_string(s_terminal_id)

            if s_terminal_id:
                s_bus_ids.add(s_terminal_id)

    l_bus_ids = sorted(s_bus_ids)

    d_bus_index_by_id = {
        s_bus_id: i_bus_index
        for i_bus_index, s_bus_id in enumerate(l_bus_ids)
    }

    l_bus_type = []

    for s_bus_id in l_bus_ids:
        d_attrs = d_bus_attributes.get(s_bus_id)

        if d_attrs is None:
            l_bus_type.append(BUS_TYPE_CODE_BY_LABEL["plain_or_tie_bus"])
            continue

        i_usage    = d_attrs.get("iUsage", 0)
        b_sync     = d_attrs.get("has_sync_dg", 0)
        b_pv       = d_attrs.get("has_pv_dg", 0)
        b_xnet     = d_attrs.get("has_xnet", 0)
        b_tr_hv    = d_attrs.get("has_transformer_hv", 0)
        b_tr_lv    = d_attrs.get("has_transformer_lv", 0)

        if i_usage == 1:
            i_typ = BUS_TYPE_CODE_BY_LABEL["junction_node"]
        elif b_xnet:
            i_typ = BUS_TYPE_CODE_BY_LABEL["external_grid_or_slack_bus"]
        elif b_sync and b_pv:
            i_typ = BUS_TYPE_CODE_BY_LABEL["mixed_dg_bus"]
        elif b_sync:
            i_typ = BUS_TYPE_CODE_BY_LABEL["sync_dg_bus"]
        elif b_pv:
            i_typ = BUS_TYPE_CODE_BY_LABEL["pv_or_inverter_dg_bus"]
        elif b_tr_hv:
            i_typ = BUS_TYPE_CODE_BY_LABEL["transformer_hv_bus"]
        elif b_tr_lv:
            i_typ = BUS_TYPE_CODE_BY_LABEL["transformer_lv_bus"]
        else:
            i_typ = BUS_TYPE_CODE_BY_LABEL["plain_or_tie_bus"]

        l_bus_type.append(i_typ)

    l_bus_uknom_kv = []
    l_bus_has_dg = []
    l_bus_dg_capacity = []
    l_bus_has_xnet = []

    for s_bus_id, i_typ in zip(l_bus_ids, l_bus_type):
        d_attrs = d_bus_attributes.get(s_bus_id) or {}
        l_bus_uknom_kv.append(round(d_attrs.get("uknom_kv", 0.0), 3))
        l_bus_has_dg.append(1 if (d_attrs.get("has_sync_dg", 0) or d_attrs.get("has_pv_dg", 0)) else 0)
        l_bus_dg_capacity.append(round(d_attrs.get("dg_capacity_mva", 0.0), 4))
        l_bus_has_xnet.append(d_attrs.get("has_xnet", 0))
        if not all(math.isfinite(f_value) and f_value >= 0.0 for f_value in (
            l_bus_uknom_kv[-1], l_bus_dg_capacity[-1],
        )):
            raise ValueError(f"Invalid voltage/DG capacity for bus {s_bus_id!r}")

    return (
        l_bus_ids,
        d_bus_index_by_id,
        l_bus_type,
        l_bus_uknom_kv,
        l_bus_has_dg,
        l_bus_dg_capacity,
        l_bus_has_xnet,
    )


def extract_directed_edge_features(rows, bus_idx):
    """
    Extracts directed edge arrays from flat corridor rows.

    Also builds a physical undirected edge dictionary used later for Y-bus
    stamping.
    """

    l_rows = rows
    d_bus_index_by_id = bus_idx

    d_directed_edge_arrays: dict[str, list[Any]] = {
        s_key: []
        for s_key in [
            "directed_edge_relay_id",
            "directed_edge_from",
            "directed_edge_to",
            "directed_edge_from_index",
            "directed_edge_to_index",
            "directed_edge_id",
            "directed_edge_canonical_id",
            "directed_edge_line_is_in_service",
            "directed_edge_length_km",
            "directed_edge_r_ohm",
            "directed_edge_x_ohm",
            "directed_edge_hop_count",
            "directed_edge_is_parallel",
            "directed_edge_parallel_count",
            "directed_edge_parallel_id",
            "directed_edge_target_valid",
            "directed_edge_source_row_number",
        ]
    }

    d_physical_edges = {}

    for i_source_row_number, d_row in enumerate(l_rows, 1):
        s_source_bus_id = clean_string(d_row["relay_node_id"])
        s_target_bus_id = clean_string(d_row["subsequent_node_id"])

        i_source_bus_index = d_bus_index_by_id[s_source_bus_id]
        i_target_bus_index = d_bus_index_by_id[s_target_bus_id]

        s_corridor_id = clean_string(
            d_row["protected_corridor_id"]
        )

        s_canonical_corridor_id = canonical_corridor_id(
            s_corridor_id
        )

        f_corridor_length_km = to_float(
            d_row.get("protected_corridor_length_km"),
            0.0,
        )

        f_corridor_r_ohm = to_float(
            d_row.get("protected_corridor_r_ohm"),
            0.0,
        )

        f_corridor_x_ohm = to_float(
            d_row.get("protected_corridor_x_ohm"),
            0.0,
        )

        i_line_is_in_service = to_int(
            d_row.get("line_is_in_service"),
            1,
        )

        d_directed_edge_arrays["directed_edge_relay_id"].append(
            clean_string(d_row.get("relay_id"))
        )
        d_directed_edge_arrays["directed_edge_from"].append(
            s_source_bus_id
        )
        d_directed_edge_arrays["directed_edge_to"].append(
            s_target_bus_id
        )
        d_directed_edge_arrays["directed_edge_from_index"].append(
            i_source_bus_index
        )
        d_directed_edge_arrays["directed_edge_to_index"].append(
            i_target_bus_index
        )
        d_directed_edge_arrays["directed_edge_id"].append(
            s_corridor_id
        )
        d_directed_edge_arrays["directed_edge_canonical_id"].append(
            s_canonical_corridor_id
        )
        d_directed_edge_arrays["directed_edge_line_is_in_service"].append(
            i_line_is_in_service
        )
        d_directed_edge_arrays["directed_edge_length_km"].append(
            f_corridor_length_km
        )
        d_directed_edge_arrays["directed_edge_r_ohm"].append(
            f_corridor_r_ohm
        )
        d_directed_edge_arrays["directed_edge_x_ohm"].append(
            f_corridor_x_ohm
        )
        d_directed_edge_arrays["directed_edge_hop_count"].append(
            to_int(d_row.get("corridor_hop_count"), 1)
        )
        d_directed_edge_arrays["directed_edge_is_parallel"].append(
            to_int(d_row.get("protected_corridor_is_parallel"), 0)
        )
        d_directed_edge_arrays["directed_edge_parallel_count"].append(
            to_int(d_row.get("protected_corridor_parallel_count"), 1)
        )
        d_directed_edge_arrays["directed_edge_parallel_id"].append(
            clean_string(d_row.get("protected_corridor_parallel_id"))
        )
        # Same decision as the flat export. zone3_applicable supplies the
        # existing per-target mask for otherwise valid terminal relays.
        b_target_valid, _s_invalid_reason = validate_case(d_row)
        d_directed_edge_arrays["directed_edge_target_valid"].append(int(b_target_valid))
        d_directed_edge_arrays["directed_edge_source_row_number"].append(
            i_source_row_number
        )

        # --------------------------------------------------------------
        # Build section-level physical graph edges.
        #
        # Important:
        # The directed relay/query edge above remains relay busbar ->
        # subsequent busbar and therefore remains aligned with the
        # protection targets.
        #
        # Physical message-passing edges, however, follow the actual
        # PowerFactory terminal/line topology including junction nodes.
        # --------------------------------------------------------------

        l_terminal_path_ids = parse_json_list(
            d_row.get(
                "protected_corridor_terminal_path_json"
            ),
            field_name="protected_corridor_terminal_path_json",
        )

        l_line_section_ids = parse_json_list(
            d_row.get(
                "protected_corridor_line_section_id_json"
            ),
            field_name="protected_corridor_line_section_id_json",
        )

        l_line_section_length_km = parse_json_list(
            d_row.get(
                "protected_corridor_line_section_length_km_json"
            ),
            field_name="protected_corridor_line_section_length_km_json",
        )

        l_line_section_r_ohm = parse_json_list(
            d_row.get(
                "protected_corridor_line_section_r_ohm_json"
            ),
            field_name="protected_corridor_line_section_r_ohm_json",
        )

        l_line_section_x_ohm = parse_json_list(
            d_row.get(
                "protected_corridor_line_section_x_ohm_json"
            ),
            field_name="protected_corridor_line_section_x_ohm_json",
        )

        # --------------------------------------------------------------
        # Structural validation
        # --------------------------------------------------------------

        i_terminal_count = len(l_terminal_path_ids)
        i_section_count = len(l_line_section_ids)

        if i_terminal_count != i_section_count + 1:
            raise ValueError(
                "Invalid physical corridor path while building graph: "
                f"corridor={s_corridor_id!r}, "
                f"terminal_count={i_terminal_count}, "
                f"section_count={i_section_count}"
            )

        if not (
                len(l_line_section_length_km)
                == i_section_count
                == len(l_line_section_r_ohm)
                == len(l_line_section_x_ohm)
        ):
            raise ValueError(
                "Physical corridor section-array length mismatch: "
                f"corridor={s_corridor_id!r}, "
                f"ids={len(l_line_section_ids)}, "
                f"length={len(l_line_section_length_km)}, "
                f"r={len(l_line_section_r_ohm)}, "
                f"x={len(l_line_section_x_ohm)}"
            )

        # --------------------------------------------------------------
        # Verify that the physical line sections reconstruct the
        # original aggregate protected corridor.
        # --------------------------------------------------------------

        for s_field, l_values in [
            ("length", [d_row.get("protected_corridor_length_km")] + l_line_section_length_km),
            ("R", [d_row.get("protected_corridor_r_ohm")] + l_line_section_r_ohm),
            ("X", [d_row.get("protected_corridor_x_ohm")] + l_line_section_x_ohm),
        ]:
            try:
                b_physical_valid = all(
                    math.isfinite(float(value)) and float(value) >= 0.0
                    for value in l_values
                )
            except (TypeError, ValueError):
                b_physical_valid = False
            if not b_physical_valid:
                raise ValueError(f"Invalid physical {s_field} in corridor {s_corridor_id!r}")

        f_section_length_sum_km = sum(
            to_float(f_value, 0.0)
            for f_value in l_line_section_length_km
        )

        f_section_r_sum_ohm = sum(
            to_float(f_value, 0.0)
            for f_value in l_line_section_r_ohm
        )

        f_section_x_sum_ohm = sum(
            to_float(f_value, 0.0)
            for f_value in l_line_section_x_ohm
        )

        # Aggregate corridor R/X/length are stored at 3 decimal places,
        # while individual physical sections are stored at 6 decimal places.
        # Allow the maximum aggregate rounding error (0.0005) plus the
        # accumulated per-section 6-decimal rounding error.
        f_physical_reconstruction_tolerance = (
                5e-4
                + i_section_count * 5e-7
                + 1e-12
        )

        if (
                abs(
                    f_section_length_sum_km
                    - f_corridor_length_km
                )
                > f_physical_reconstruction_tolerance
        ):
            raise ValueError(
                "Physical corridor length reconstruction mismatch: "
                f"corridor={s_corridor_id!r}, "
                f"sections={f_section_length_sum_km:.9f} km, "
                f"aggregate={f_corridor_length_km:.9f} km"
            )

        if (
                abs(
                    f_section_r_sum_ohm
                    - f_corridor_r_ohm
                )
                > f_physical_reconstruction_tolerance
        ):
            raise ValueError(
                "Physical corridor R reconstruction mismatch: "
                f"corridor={s_corridor_id!r}, "
                f"sections={f_section_r_sum_ohm:.9f} ohm, "
                f"aggregate={f_corridor_r_ohm:.9f} ohm"
            )

        if (
                abs(
                    f_section_x_sum_ohm
                    - f_corridor_x_ohm
                )
                > f_physical_reconstruction_tolerance
        ):
            raise ValueError(
                "Physical corridor X reconstruction mismatch: "
                f"corridor={s_corridor_id!r}, "
                f"sections={f_section_x_sum_ohm:.9f} ohm, "
                f"aggregate={f_corridor_x_ohm:.9f} ohm"
            )

        # --------------------------------------------------------------
        # Create one physical graph edge per actual line section.
        #
        # terminal[i] -- line[i] -- terminal[i + 1]
        # --------------------------------------------------------------

        for i_section_index in range(i_section_count):

            s_physical_from_bus_id = clean_string(
                l_terminal_path_ids[i_section_index]
            )

            s_physical_to_bus_id = clean_string(
                l_terminal_path_ids[i_section_index + 1]
            )

            s_line_section_id = clean_string(
                l_line_section_ids[i_section_index]
            )

            if not s_physical_from_bus_id:
                raise ValueError(
                    f"Blank physical-edge source terminal in "
                    f"corridor {s_corridor_id!r}, "
                    f"section_index={i_section_index}"
                )

            if not s_physical_to_bus_id:
                raise ValueError(
                    f"Blank physical-edge target terminal in "
                    f"corridor {s_corridor_id!r}, "
                    f"section_index={i_section_index}"
                )

            if not s_line_section_id:
                raise ValueError(
                    f"Blank physical line-section ID in "
                    f"corridor {s_corridor_id!r}, "
                    f"section_index={i_section_index}"
                )

            if s_physical_from_bus_id not in d_bus_index_by_id:
                raise ValueError(
                    f"Physical source terminal "
                    f"{s_physical_from_bus_id!r} "
                    "is missing from graph bus index."
                )

            if s_physical_to_bus_id not in d_bus_index_by_id:
                raise ValueError(
                    f"Physical target terminal "
                    f"{s_physical_to_bus_id!r} "
                    "is missing from graph bus index."
                )

            i_section_from_index = d_bus_index_by_id[s_physical_from_bus_id]

            i_section_to_index = d_bus_index_by_id[s_physical_to_bus_id]

            (
                i_physical_from_index,
                i_physical_to_index,
            ) = sorted(
                (
                    i_section_from_index,
                    i_section_to_index,
                )
            )

            f_section_length_km = to_float(l_line_section_length_km[i_section_index], 0.0)

            f_section_r_ohm = to_float(l_line_section_r_ohm[i_section_index], 0.0)

            f_section_x_ohm = to_float(l_line_section_x_ohm[i_section_index], 0.0)

            s_canonical_line_section_id = canonical_corridor_id(s_line_section_id)

            t_physical_edge_key = (i_physical_from_index, i_physical_to_index, s_canonical_line_section_id)

            if t_physical_edge_key not in d_physical_edges:

                d_physical_edges[t_physical_edge_key] = PhysicalEdge(
                    i_physical_from_index,
                    i_physical_to_index,
                    s_canonical_line_section_id,
                    s_line_section_id,
                    f_section_length_km,
                    f_section_r_ohm,
                    f_section_x_ohm,
                    i_line_is_in_service,
                )

            else:
                o_existing_physical_edge = d_physical_edges[t_physical_edge_key]

                f_duplicate_tolerance = 1e-6

                if abs(float(o_existing_physical_edge.length_km) - f_section_length_km) > f_duplicate_tolerance:
                    raise ValueError(
                        "Duplicate physical-edge length mismatch: "
                        f"line={s_line_section_id!r}, "
                        f"existing={float(o_existing_physical_edge.length_km):.9f}, "
                        f"current={f_section_length_km:.9f}"
                    )

                if abs(float(o_existing_physical_edge.r_ohm) - f_section_r_ohm) > f_duplicate_tolerance:
                    raise ValueError(
                        "Duplicate physical-edge R mismatch: "
                        f"line={s_line_section_id!r}, "
                        f"existing={float(o_existing_physical_edge.r_ohm):.9f}, "
                        f"current={f_section_r_ohm:.9f}"
                    )

                if abs(float(o_existing_physical_edge.x_ohm) - f_section_x_ohm) > f_duplicate_tolerance:
                    raise ValueError(
                        "Duplicate physical-edge X mismatch: "
                        f"line={s_line_section_id!r}, "
                        f"existing={float(o_existing_physical_edge.x_ohm):.9f}, "
                        f"current={f_section_x_ohm:.9f}"
                    )

                o_existing_physical_edge.is_in_service = min(
                    int(o_existing_physical_edge.is_in_service),
                    i_line_is_in_service,
                )

    d_directed_edge_metadata = {
        "directed_edge_count": len(
            d_directed_edge_arrays["directed_edge_id"]
        ),
        "directed_edge_target_valid_count": sum(
            d_directed_edge_arrays["directed_edge_target_valid"]
        ),
    }

    return (
        d_directed_edge_arrays,
        d_physical_edges,
        d_directed_edge_metadata,
    )


def stamp_switched_ybus(phys_edges, n):
    """
    Builds switched physical-edge arrays and Y-bus arrays from physical edges.

    Only in-service physical edges contribute to the Y-bus matrix.
    """

    d_physical_edges = phys_edges
    i_bus_count = n

    i_physical_pair_count = i_bus_count * (i_bus_count - 1) // 2

    l_lines_connected = [0] * i_physical_pair_count
    l_lines_connected_count = [0] * i_physical_pair_count
    l_y_lines = [complex(0, 0)] * i_physical_pair_count

    l_y_matrix = [
        [
            complex(0, 0)
            for _i_column_index in range(i_bus_count)
        ]
        for _i_row_index in range(i_bus_count)
    ]

    d_physical_edge_arrays = {
        s_key: []
        for s_key in [
            "physical_edge_from_index",
            "physical_edge_to_index",
            "physical_edge_id",
            "physical_edge_length_km",
            "physical_edge_r_ohm",
            "physical_edge_x_ohm",
            "physical_edge_y_real",
            "physical_edge_y_imag",
            "physical_edge_is_in_service",
        ]
    }

    for d_physical_edge in d_physical_edges.values():
        if not d_physical_edge.is_in_service:
            continue

        i_from_bus_index = d_physical_edge.a
        i_to_bus_index = d_physical_edge.b

        f_edge_r_ohm = float(d_physical_edge.r_ohm)
        f_edge_x_ohm = float(d_physical_edge.x_ohm)

        c_edge_impedance_ohm = complex(
            f_edge_r_ohm,
            f_edge_x_ohm,
        )

        c_edge_admittance = (
            complex(0, 0)
            if abs(c_edge_impedance_ohm) <= 1e-12
            else 1.0 / c_edge_impedance_ohm
        )

        i_upper_triangle_index = upper_triangle_index(
            i_from_bus_index,
            i_to_bus_index,
            i_bus_count,
        )

        l_lines_connected[i_upper_triangle_index] = 1
        l_lines_connected_count[i_upper_triangle_index] += 1
        l_y_lines[i_upper_triangle_index] += c_edge_admittance

        l_y_matrix[i_from_bus_index][i_from_bus_index] += c_edge_admittance
        l_y_matrix[i_to_bus_index][i_to_bus_index] += c_edge_admittance
        l_y_matrix[i_from_bus_index][i_to_bus_index] -= c_edge_admittance
        l_y_matrix[i_to_bus_index][i_from_bus_index] -= c_edge_admittance

        d_physical_edge_arrays["physical_edge_from_index"].append(
            i_from_bus_index
        )
        d_physical_edge_arrays["physical_edge_to_index"].append(
            i_to_bus_index
        )
        d_physical_edge_arrays["physical_edge_id"].append(
            clean_string(d_physical_edge.canonical_id)
        )
        d_physical_edge_arrays["physical_edge_length_km"].append(
            float(d_physical_edge.length_km)
        )
        d_physical_edge_arrays["physical_edge_r_ohm"].append(
            f_edge_r_ohm
        )
        d_physical_edge_arrays["physical_edge_x_ohm"].append(
            f_edge_x_ohm
        )
        d_physical_edge_arrays["physical_edge_y_real"].append(
            float(c_edge_admittance.real)
        )
        d_physical_edge_arrays["physical_edge_y_imag"].append(
            float(c_edge_admittance.imag)
        )
        d_physical_edge_arrays["physical_edge_is_in_service"].append(1)

    return {
        "Lines_connected": l_lines_connected,
        "Lines_connected_count": l_lines_connected_count,
        "Y_Lines_real": [
            float(c_y_value.real)
            for c_y_value in l_y_lines
        ],
        "Y_Lines_imag": [
            float(c_y_value.imag)
            for c_y_value in l_y_lines
        ],
        "Y_matrix_real": [
            float(c_y_value.real)
            for c_y_value in flatten_row_major(l_y_matrix)
        ],
        "Y_matrix_imag": [
            float(c_y_value.imag)
            for c_y_value in flatten_row_major(l_y_matrix)
        ],
        "Y_matrix_real_2d": [
            [
                float(c_y_value.real)
                for c_y_value in l_matrix_row
            ]
            for l_matrix_row in l_y_matrix
        ],
        "Y_matrix_imag_2d": [
            [
                float(c_y_value.imag)
                for c_y_value in l_matrix_row
            ]
            for l_matrix_row in l_y_matrix
        ],
        "physical_edge_candidate_count": len(d_physical_edges),
        "physical_edge_count": len(
            d_physical_edge_arrays["physical_edge_id"]
        ),
        **d_physical_edge_arrays,
    }


def convert_scenario_to_graph_row(rows, meta, switch_status, bus_attributes=None):
    """
    Converts all flat rows from one scenario into one graph-array row.
    """

    l_rows = rows
    d_metadata = meta
    l_switch_status = switch_status

    (
        l_bus_ids,
        d_bus_index_by_id,
        l_bus_type,
        l_bus_uknom_kv,
        l_bus_has_dg,
        l_bus_dg_capacity_mva,
        l_bus_has_xnet,
    ) = build_bus_index(l_rows, bus_attributes)

    (
        d_directed_edge_data,
        d_physical_edges,
        d_directed_edge_metadata,
    ) = extract_directed_edge_features(
        l_rows,
        d_bus_index_by_id,
    )

    d_ybus_data = stamp_switched_ybus(
        d_physical_edges,
        len(l_bus_ids),
    )

    d_ml_array_data = {}

    d_ml_array_data.update(
        {
            s_column: collect_numeric_array(
                l_rows,
                s_column,
                1.0 if s_column == "zone3_applicable" else 0.0,
            )
            for s_column in directed_numeric_context_columns
        }
    )

    d_ml_array_data.update(
        {
            s_column: collect_string_array(
                l_rows,
                s_column,
                "",
            )
            for s_column in directed_string_context_columns
        }
    )

    d_ml_array_data.update(
        {
            s_column: collect_numeric_array(
                l_rows,
                s_column,
                math.nan,
            )
            for s_column in base_reach_columns
        }
    )

    d_ml_array_data.update(
        {
            s_column: collect_numeric_array(
                l_rows,
                s_column,
                math.nan,
            )
            for s_column in reach_infeed_correction_columns
        }
    )

    d_ml_array_data.update(
        {
            s_column: collect_numeric_array(
                l_rows,
                s_column,
                math.nan,
            )
            for s_column in target_reach_columns
        }
    )

    return {
        **d_metadata,
        **d_directed_edge_metadata,

        "switch_count": len(l_switch_status),
        "switch_status": l_switch_status,
        "switch_open_count_from_vector": (
            len(l_switch_status) - sum(l_switch_status)
        ),
        "switch_closed_count_from_vector": sum(l_switch_status),

        "bus_number": len(l_bus_ids),
        "bus_id": l_bus_ids,
        "bus_typ": l_bus_type,
        "bus_uknom_kv": l_bus_uknom_kv,
        "bus_has_dg": l_bus_has_dg,
        "bus_dg_capacity_mva": l_bus_dg_capacity_mva,
        "bus_has_xnet": l_bus_has_xnet,

        **d_ybus_data,
        **d_directed_edge_data,
        **d_ml_array_data,
    }

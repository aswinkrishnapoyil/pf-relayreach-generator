# graph_array_utils.py
from __future__ import annotations

import math
from typing import Any


def is_blank(value: Any) -> bool:
    """
    Checks whether a value should be treated as blank/missing.
    Blank values include:
    - None
    - NaN float
    - empty string
    - string values: 'nan', 'none', 'null'
    """

    o_value = value

    if o_value is None:
        return True

    if isinstance(o_value, float) and math.isnan(o_value):
        return True

    s_value_text = str(o_value).strip()

    return s_value_text == "" or s_value_text.lower() in {
        "nan",
        "none",
        "null",
    }


def clean_string(value: Any, default: str = "") -> str:
    """
    Converts a value to a clean string.
    If the value is blank, the configured default string is returned.
    """

    o_value = value
    s_default = default

    if is_blank(o_value):
        return s_default

    return str(o_value).strip()


def to_float(value: Any, default: float = math.nan) -> float:
    """
    Safely converts a value to float.
    If the value is blank or cannot be converted, the configured default float
    value is returned.
    """

    o_value = value
    f_default = default

    if is_blank(o_value):
        return f_default

    try:
        return float(o_value)

    except Exception:
        return f_default


def to_int(value: Any, default: int = 0) -> int:
    """
    Safely converts a value to int.
    The conversion first passes through float so values such as '1.0' can be
    converted to integer 1.
    """

    o_value = value
    i_default = default

    if is_blank(o_value):
        return i_default

    try:
        return int(float(o_value))

    except Exception:
        return i_default


def upper_triangle_index(i: int, j: int, n: int) -> int:
    """
    Returns the compact upper-triangle array index for matrix position (i, j).
    Self-loops are not stored, so i == j is invalid.
    The function accepts node indices i and j and the total node count n.
    If i > j, the indices are swapped so the upper-triangle position is used.
    """

    i_first_node_index = i
    i_second_node_index = j
    i_node_count = n

    if i_first_node_index == i_second_node_index:
        raise ValueError(
            "Self-loops are not stored in the compact upper triangle."
        )

    if i_first_node_index > i_second_node_index:
        i_first_node_index, i_second_node_index = (
            i_second_node_index,
            i_first_node_index,
        )

    return int(
        i_first_node_index * i_node_count
        - (i_first_node_index * (i_first_node_index + 1)) // 2
        + (i_second_node_index - i_first_node_index - 1)
    )


def flatten_row_major(matrix: list[list[complex]]) -> list[complex]:
    """
    Flattens a 2D matrix into a 1D list using row-major order.
    Row-major order means:
        row 0 columns left-to-right,
        then row 1 column left-to-right,
        and so on.
    """

    l_matrix = matrix
    i_row_count = len(l_matrix)

    return [
        l_matrix[i_row_index][i_column_index]
        for i_row_index in range(i_row_count)
        for i_column_index in range(len(l_matrix[i_row_index]))
    ]


def canonical_corridor_id(corridor_id: Any) -> str:
    """
    Builds a canonical corridor ID string.
    The function:
    - accepts corridor IDs separated by ';' or '|'
    - strips whitespace
    - removes empty parts
    - sorts the parts
    - joins them using ' | '
    This makes equivalent corridor IDs stable and comparable.
    """

    o_corridor_id = corridor_id

    s_corridor_id = str(o_corridor_id).replace(";", "|")

    l_corridor_id_parts = [
        s_part.strip()
        for s_part in s_corridor_id.split("|")
        if s_part.strip()
    ]

    if not l_corridor_id_parts:
        return ""

    return " | ".join(sorted(l_corridor_id_parts))

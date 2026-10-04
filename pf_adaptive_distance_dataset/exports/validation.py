# validation.py
from __future__ import annotations

import math
import logging
from typing import Optional, Tuple

import pandas as pd

from ..core.dataset_schema import (
    base_reach_columns, reach_infeed_correction_columns,
    target_reach_columns, directed_numeric_context_columns,
)

logger = logging.getLogger(__name__)

CASE_NUMERIC_COLUMNS = (
    base_reach_columns + reach_infeed_correction_columns
    + target_reach_columns + directed_numeric_context_columns
)


def validate_case(case: dict, numeric_columns=None) -> Tuple[bool, Optional[str]]:
    """Validates an individual flat row for data integrity before accumulating."""

    d_case_row = case
    l_numeric_columns = CASE_NUMERIC_COLUMNS if numeric_columns is None else numeric_columns

    i_zone3_applicable = d_case_row.get("zone3_applicable", 1)
    if i_zone3_applicable not in (0, 1):
        return False, "invalid_zone3_applicable"

    for i_target, s_column in enumerate(target_reach_columns):
        value = d_case_row.get(s_column)
        try:
            f_value = float(value) if value is not None else math.nan
        except (TypeError, ValueError):
            return False, f"nonnumeric_target_{s_column}"
        if i_target >= 4 and not i_zone3_applicable:
            if not math.isnan(f_value):
                return False, f"nonmissing_inapplicable_target_{s_column}"
        elif not math.isfinite(f_value):
            return False, f"missing_or_nonfinite_target_{s_column}"
        elif f_value < -1e-6:
            return False, f"negative_zone_reach_{s_column}_{f_value}"
        elif f_value > 10000:
            return False, f"zone_reach_out_of_range_{s_column}_{f_value}"

    i_nan_count = sum(
        1
        for s_column in l_numeric_columns
        if s_column in d_case_row
        and (
            d_case_row[s_column] is None
            or (
                isinstance(d_case_row[s_column], float)
                and math.isnan(d_case_row[s_column])
            )
        )
    )

    if i_nan_count > len(l_numeric_columns) * 0.5:
        return False, f"excessive_nan_values_{i_nan_count}"

    for s_column, value in d_case_row.items():
        if s_column in l_numeric_columns and isinstance(value, (int, float)) and math.isinf(value):
            return False, f"infinite_numeric_value_{s_column}"
        if (
            "reach" in s_column
            and "ohm" in s_column
            and isinstance(value, (int, float))
            and not math.isnan(value)
        ):

            if value < -1e-6:
                return False, f"negative_zone_reach_{s_column}_{value}"

            if value > 10000:
                return False, f"zone_reach_out_of_range_{s_column}_{value}"

        if (
            ("r_ohm" in s_column or "x_ohm" in s_column)
            and "correction" not in s_column
            and "reach" not in s_column
        ):
            if (
                isinstance(value, (int, float))
                and not math.isnan(value)
                and value < -1.0
            ):
                return False, f"negative_impedance_{s_column}"

    return True, None


def calculate_numeric_statistics(
    df: pd.DataFrame,
    numeric_columns: list,
) -> dict:
    """Calculate mean, std, min, max, median, and quartiles for numeric columns."""

    df_rows = df
    l_numeric_columns = numeric_columns

    d_numeric_statistics = {}

    for s_column in l_numeric_columns:
        if s_column not in df_rows.columns:
            continue

        try:
            sr_column_data = pd.to_numeric(
                df_rows[s_column],
                errors="coerce",
            ).dropna()

            if len(sr_column_data) == 0:
                continue

            d_numeric_statistics[s_column] = {
                "mean": float(sr_column_data.mean()),
                "std": float(sr_column_data.std()),
                "min": float(sr_column_data.min()),
                "max": float(sr_column_data.max()),
                "median": float(sr_column_data.median()),
                "q25": float(sr_column_data.quantile(0.25)),
                "q75": float(sr_column_data.quantile(0.75)),
            }

        except Exception as o_error:
            logger.warning(
                f"Could not calculate stats for {s_column}: {o_error}"
            )

    return d_numeric_statistics


def calculate_zone_reach_ranges(df: pd.DataFrame) -> dict:
    """Calculate specific statistics for critical zone reach columns."""

    df_rows = df

    l_reach_columns = [
        s_column
        for s_column in df_rows.columns
        if "reach" in s_column and "ohm" in s_column
    ]

    d_reach_ranges = {}

    for s_column in l_reach_columns:
        try:
            sr_column_data = pd.to_numeric(
                df_rows[s_column],
                errors="coerce",
            )

            sr_valid_data = sr_column_data.dropna()

            if len(sr_valid_data) > 0:
                d_reach_ranges[s_column] = {
                    "min": float(sr_valid_data.min()),
                    "max": float(sr_valid_data.max()),
                    "mean": float(sr_valid_data.mean()),
                    "std": float(sr_valid_data.std()),
                    "median": float(sr_valid_data.median()),
                    "count_valid": int(len(sr_valid_data)),
                    "count_missing": int(
                        len(sr_column_data) - len(sr_valid_data)
                    ),
                }

        except Exception as o_error:
            logger.warning(
                f"Could not calculate reach ranges for {s_column}: {o_error}"
            )

    return d_reach_ranges

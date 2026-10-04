# export.py
from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

import pandas as pd

from ..core.config import OUTPUT_DIR
from ..core.models import DatasetMetadata, DatasetStatistics, ExportPayload

from ..core.dataset_schema import (
    l_case_feature_columns,
)

from .validation import (
    CASE_NUMERIC_COLUMNS,
    validate_case,
    calculate_numeric_statistics,
    calculate_zone_reach_ranges,
)


logger = logging.getLogger(__name__)

FLAT_PROVENANCE_COLUMNS = [
    "switch_state_short_id",
    "switch_state_config_id",
    "switch_state_row_index",
    "switch_state_open_switch_count",
    "switch_state_closed_switch_count",
    "scenario_id",
    "scenario_uid",
    "case_uid",
    "line_length_random_seed",
    "line_length_scale_min",
    "line_length_scale_max",
    "dg_capacity_random_seed",
    "dg_capacity_scale_min",
    "dg_capacity_scale_max",
]

FLAT_EXPORT_COLUMNS = FLAT_PROVENANCE_COLUMNS + l_case_feature_columns


def _csv_has_content(path: Path) -> bool:
    """
    Returns True if a CSV/file path exists and contains at least one byte.
    """

    p_file_path = path

    return p_file_path.exists() and p_file_path.stat().st_size > 0


def _read_csv_header(path: Path) -> list[str]:
    """
    Reads the first row/header from a CSV file.
    Returns an empty list if the file does not exist, is empty, or cannot be read.
    """

    p_csv_path = path

    if not _csv_has_content(p_csv_path):
        return []

    try:
        with p_csv_path.open("r", newline="", encoding="utf-8") as o_file:
            o_csv_reader = csv.reader(o_file)
            return next(o_csv_reader, [])

    except Exception:
        return []


def _json_default(obj):
    """
    JSON serializer fallback for pathlib/numpy-like objects.
    """

    o_object = obj

    if isinstance(o_object, Path):
        return str(o_object)

    try:
        return o_object.item()

    except Exception:
        pass

    try:
        return str(o_object)

    except Exception:
        return None


def _write_json(path: Path, data: dict) -> None:
    """
    Writes a dictionary to a JSON file.
    """

    p_json_path = path
    d_data = data

    p_json_path.parent.mkdir(parents=True, exist_ok=True)

    with p_json_path.open("w", encoding="utf-8") as o_file:
        json.dump(
            d_data,
            o_file,
            indent=4,
            default=_json_default,
        )


def _write_dict_rows_csv(path: Path, rows: list[dict]) -> None:
    """
    Writes a list of dictionaries to CSV.
    Handles empty row lists safely.
    """

    p_csv_path = path
    l_rows = rows

    p_csv_path.parent.mkdir(parents=True, exist_ok=True)

    if not l_rows:
        return

    l_fieldnames = sorted(
        {
            s_key
            for d_row in l_rows
            for s_key in d_row.keys()
        }
    )

    b_file_exists = _csv_has_content(p_csv_path)
    l_existing_header = _read_csv_header(p_csv_path)

    if b_file_exists and l_existing_header and l_existing_header != l_fieldnames:
        raise RuntimeError(
            f"CSV field mismatch in '{p_csv_path.name}'. "
            f"Cannot append: existing header has {len(l_existing_header)} columns, "
            f"new batch has {len(l_fieldnames)} columns. "
            f"Delete the file or start a new output directory."
        )

    with p_csv_path.open("a", newline="", encoding="utf-8") as o_file:
        o_csv_writer = csv.DictWriter(
            o_file,
            fieldnames=l_fieldnames,
            extrasaction="ignore",
        )

        if not b_file_exists:
            o_csv_writer.writeheader()

        for d_row in l_rows:
            o_csv_writer.writerow(d_row)


def _normalise_flat_row(row: dict) -> dict:
    """
    Ensures flat rows retain scenario provenance and follow the configured
    feature schema.
    """

    l_missing_provenance = [
        s_column
        for s_column in FLAT_PROVENANCE_COLUMNS
        if s_column not in row
    ]

    if l_missing_provenance:
        raise ValueError(
            "Flat row is missing required scenario provenance columns: "
            + ", ".join(l_missing_provenance)
        )

    return {
        s_column: row.get(s_column, None)
        for s_column in FLAT_EXPORT_COLUMNS
    }


def _write_flat_rows_csv(path: Path, rows: list[dict]) -> None:
    """
    Appends valid flat case rows to the flat CSV export.
    The exported columns contain scenario provenance followed by case features.
    """

    p_flat_csv_path = path
    l_rows = rows

    p_flat_csv_path.parent.mkdir(parents=True, exist_ok=True)

    b_file_exists = _csv_has_content(p_flat_csv_path)

    with p_flat_csv_path.open("a", newline="", encoding="utf-8") as o_file:
        o_csv_writer = csv.DictWriter(
            o_file,
            fieldnames=FLAT_EXPORT_COLUMNS,
            extrasaction="ignore",
        )

        if not b_file_exists:
            o_csv_writer.writeheader()

        for d_row in l_rows:
            o_csv_writer.writerow(
                _normalise_flat_row(d_row)
            )


def _write_graph_rows_parquet(path: Path, graph_rows: list[dict]) -> None:
    """
    Writes graph-array scenario rows to a Parquet file.
    Always receives the full in-memory accumulation (l_all_graph_rows), so it
    simply overwrites the file. This avoids the read-concat-rewrite pattern
    that was O(n²) in I/O and silently duplicated rows on every checkpoint call.
    """

    p_graph_parquet_path = path
    l_graph_rows = graph_rows

    p_graph_parquet_path.parent.mkdir(parents=True, exist_ok=True)

    df_graph_rows = (
        pd.DataFrame(l_graph_rows)
        if l_graph_rows
        else pd.DataFrame({
            "scenario_uid": pd.Series(dtype="string"),
            "switch_state_row_index": pd.Series(dtype="int64"),
        })
    )
    df_graph_rows.to_parquet(
        p_graph_parquet_path,
        engine="pyarrow",
        index=False,
    )


def _append_audit_rows(
    path: Path,
    rows: list[dict],
    sheet_name: str = "audit",
) -> None:
    """
    Writes audit rows to Excel.
    If the file already exists, this rewrites the target sheet with accumulated
    rows from the existing sheet plus new rows.
    """

    p_audit_xlsx_path = path
    l_rows = rows
    s_sheet_name = sheet_name

    p_audit_xlsx_path.parent.mkdir(parents=True, exist_ok=True)

    if not l_rows:
        return

    df_new_audit_rows = pd.DataFrame(l_rows)

    if p_audit_xlsx_path.exists():
        try:
            df_existing_audit_rows = pd.read_excel(
                p_audit_xlsx_path,
                sheet_name=s_sheet_name,
            )

            df_output_audit_rows = pd.concat(
                [
                    df_existing_audit_rows,
                    df_new_audit_rows,
                ],
                ignore_index=True,
            )

        except Exception:
            df_output_audit_rows = df_new_audit_rows

    else:
        df_output_audit_rows = df_new_audit_rows

    with pd.ExcelWriter(
        p_audit_xlsx_path,
        engine="openpyxl",
        mode="w",
    ) as o_excel_writer:
        df_output_audit_rows.to_excel(
            o_excel_writer,
            sheet_name=s_sheet_name,
            index=False,
        )


def _extract_payload_rows(payload: ExportPayload) -> tuple[list[dict], list[dict]]:
    """
    Extracts flat rows and graph rows from an ExportPayload.
    Current generator format:
    - ExportPayload(kind="flat_row", data=flat_row)
    - ExportPayload(kind="graph_row", data=graph_row)
    """

    o_payload = payload
    d_payload_data = o_payload.data or {}

    if o_payload.kind == "flat_row":
        return [d_payload_data], []

    if o_payload.kind == "graph_row":
        return [], [d_payload_data]

    l_flat_rows = (
        d_payload_data.get("flat_rows")
        or d_payload_data.get("rows")
        or d_payload_data.get("case_rows")
        or []
    )

    l_graph_rows = []

    d_graph_row = (
        d_payload_data.get("graph_row")
        or d_payload_data.get("graph_array_row")
        or d_payload_data.get("scenario_graph_row")
    )

    if d_graph_row is not None:
        l_graph_rows.append(d_graph_row)

    if d_payload_data.get("graph_rows"):
        l_graph_rows.extend(
            d_payload_data.get("graph_rows")
        )

    return l_flat_rows, l_graph_rows


def stream_export_and_audit(
    payloads: Generator[ExportPayload, None, None],
    metadata: DatasetMetadata,
    output_dir: Optional[Path] = None,
) -> DatasetStatistics:
    """
    Streams generated scenario payloads to disk.
    Writes:
    - flat valid case rows CSV
    - graph-array Parquet
    - line randomization logs CSV
    - DG randomization logs CSV
    - audit Excel
    - metadata JSON
    - statistics JSON
    """

    g_payloads = payloads
    d_metadata = metadata
    p_output_dir = output_dir

    if p_output_dir is None:
        p_output_dir = OUTPUT_DIR

    p_output_dir = Path(p_output_dir)
    p_output_dir.mkdir(parents=True, exist_ok=True)

    s_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    p_flat_csv_path = (
        p_output_dir
        / f"distance_protection_flat_rows_{s_timestamp}.csv"
    )

    p_graph_parquet_path = (
        p_output_dir
        / f"distance_protection_graph_array_{s_timestamp}.parquet"
    )

    p_audit_xlsx_path = (
        p_output_dir
        / f"distance_protection_audit_{s_timestamp}.xlsx"
    )

    p_line_log_csv_path = (
        p_output_dir
        / f"line_randomization_log_{s_timestamp}.csv"
    )

    p_dg_log_csv_path = (
        p_output_dir
        / f"dg_randomization_log_{s_timestamp}.csv"
    )

    p_metadata_json_path = (
        p_output_dir
        / f"metadata_{s_timestamp}.json"
    )

    p_statistics_json_path = (
        p_output_dir
        / f"statistics_{s_timestamp}.json"
    )

    d_statistics = DatasetStatistics()

    i_global_case_id = 0
    i_graph_rows_since_last_checkpoint = 0

    l_valid_rows_for_statistics: list[dict] = []
    l_audit_rows: list[dict] = []
    l_all_graph_rows: list[dict] = []
    l_skipped_scenarios: list[dict] = []

    l_numeric_columns = CASE_NUMERIC_COLUMNS

    logger.info("Starting streaming export...")

    for o_payload in g_payloads:
        if o_payload is None:
            continue

        if o_payload.kind == "skipped_scenario":
            l_skipped_scenarios.append(dict(o_payload.data))
            s_reason = str(o_payload.data["reason"])
            d_statistics.invalid_case_reasons[s_reason] = (
                d_statistics.invalid_case_reasons.get(s_reason, 0) + 1
            )
            continue

        l_flat_rows, l_graph_rows = _extract_payload_rows(o_payload)

        l_valid_flat_rows: list[dict] = []

        for d_row in l_flat_rows:
            d_statistics.total_cases_generated += 1

            b_is_valid, s_invalid_reason = validate_case(
                d_row,
                l_numeric_columns,
            )

            if b_is_valid:
                i_global_case_id += 1

                d_export_row = dict(d_row)
                d_export_row["scenario_case_id"] = int(d_export_row.get("case_id", 0))
                d_export_row["case_id"] = i_global_case_id

                d_statistics.total_cases_valid += 1
                l_valid_flat_rows.append(d_export_row)
                l_valid_rows_for_statistics.append(d_export_row)

            else:
                d_export_row = dict(d_row)

                d_statistics.total_cases_invalid += 1

                d_statistics.invalid_case_reasons[s_invalid_reason] = (
                    d_statistics.invalid_case_reasons.get(
                        s_invalid_reason,
                        0,
                    )
                    + 1
                )

            l_audit_rows.append(
                {
                    "kind": o_payload.kind,
                    "scenario_uid": d_export_row.get("scenario_uid", ""),
                    "case_id": d_export_row.get("case_id", ""),
                    "scenario_case_id": d_export_row.get("scenario_case_id", d_row.get("case_id", "")),
                    "relay_id": d_export_row.get("relay_id", ""),
                    "relay_node_id": d_export_row.get("relay_node_id", ""),
                    "protected_corridor_id": d_export_row.get(
                        "protected_corridor_id",
                        "",
                    ),
                    "subsequent_node_id": d_export_row.get(
                        "subsequent_node_id",
                        "",
                    ),
                    "is_valid": int(b_is_valid),
                    "invalid_reason": s_invalid_reason or "",
                }
            )

        if l_valid_flat_rows:
            _write_flat_rows_csv(
                p_flat_csv_path,
                l_valid_flat_rows,
            )

        if l_graph_rows:
            l_all_graph_rows.extend(l_graph_rows)

            # Checkpoint flush every 50 scenarios for crash recovery
            i_graph_rows_since_last_checkpoint += len(l_graph_rows)
            if i_graph_rows_since_last_checkpoint >= 50:
                i_graph_rows_since_last_checkpoint = 0
                _write_graph_rows_parquet(
                    p_graph_parquet_path,
                    l_all_graph_rows
                )

        if o_payload.line_logs:
            _write_dict_rows_csv(
                p_line_log_csv_path,
                o_payload.line_logs,
            )

        if o_payload.dg_logs:
            _write_dict_rows_csv(
                p_dg_log_csv_path,
                o_payload.dg_logs,
            )

    _write_graph_rows_parquet(
        p_graph_parquet_path,
        l_all_graph_rows,
    )

    if l_audit_rows:
        _append_audit_rows(
            p_audit_xlsx_path,
            l_audit_rows,
            sheet_name="audit",
        )

    if l_valid_rows_for_statistics:
        df_valid_rows_for_statistics = pd.DataFrame(
            l_valid_rows_for_statistics
        )

        d_statistics.missing_value_counts = {
            s_column: int(df_valid_rows_for_statistics[s_column].isna().sum())
            for s_column in df_valid_rows_for_statistics.columns
        }

        d_statistics.numeric_column_stats = calculate_numeric_statistics(
            df_valid_rows_for_statistics,
            l_numeric_columns,
        )

        d_statistics.zone_reach_ranges = calculate_zone_reach_ranges(
            df_valid_rows_for_statistics
        )

    _write_json(
        p_metadata_json_path,
        asdict(d_metadata),
    )

    _write_json(
        p_statistics_json_path,
        asdict(d_statistics),
    )

    if l_skipped_scenarios:
        _write_json(
            p_output_dir / "skipped_scenarios.json",
            {"scenarios": l_skipped_scenarios},
        )

    logger.info(f"Flat rows written to: {p_flat_csv_path}")
    logger.info(f"Graph array Parquet written to: {p_graph_parquet_path}")
    logger.info(f"Audit written to: {p_audit_xlsx_path}")
    logger.info(f"Metadata written to: {p_metadata_json_path}")
    logger.info(f"Statistics written to: {p_statistics_json_path}")

    logger.info(
        "Streaming export complete. "
        f"Generated={d_statistics.total_cases_generated}, "
        f"Valid={d_statistics.total_cases_valid}, "
        f"Invalid={d_statistics.total_cases_invalid}"
    )

    return d_statistics

# models.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class DatasetIntegrityError(RuntimeError):
    """A scenario cannot be trusted; stop the worker and retain its diagnostics."""


class PowerFactoryStateError(DatasetIntegrityError):
    """A required PowerFactory setting could not be read or verified."""


class ShortCircuitCalculationError(DatasetIntegrityError):
    """A required short-circuit calculation or current reading failed."""


class ShortCircuitScenarioError(ShortCircuitCalculationError):
    """A scenario has no trustworthy short-circuit result and may be skipped."""


class ShortCircuitExecutionError(ShortCircuitScenarioError):
    """PowerFactory did not execute a required short-circuit calculation."""


class ShortCircuitCurrentError(ShortCircuitScenarioError):
    """PowerFactory did not expose a valid required current for a scenario."""


@dataclass
class DatasetMetadata:
    """
    Stores high-level metadata for one dataset export.
    These field names are public export keys and should not be renamed unless
    all dependent code and output schemas are updated.
    """

    dataset_version: str
    grid_name: str
    project_name: str
    generation_timestamp: str

    random_seed_base: int

    line_randomization_enabled: bool
    dg_randomization_enabled: bool

    line_length_scale_range: tuple[float, float]
    dg_capacity_scale_range: tuple[float, float]

    notes: str


@dataclass
class DatasetStatistics:
    """
    Stores final dataset generation and validation statistics.
    These field names are serialized to statistics JSON by export.py, so they
    should remain stable.
    """

    total_cases_generated: int = 0
    total_cases_valid: int = 0
    total_cases_invalid: int = 0

    invalid_case_reasons: dict[str, int] = field(default_factory=dict)
    missing_value_counts: dict[str, int] = field(default_factory=dict)

    numeric_column_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    zone_reach_ranges: dict[str, dict[str, float | int]] = field(default_factory=dict)

    generation_time_seconds: float = 0.0


@dataclass
class ExportPayload:
    """
    Container passed from dataset generation to streaming export.
    kind:
        Payload type, for example 'flat_row' or 'graph_row'.
    data:
        Main payload dictionary.
    line_logs:
        Optional line randomization log rows.
    dg_logs:
        Optional DG capacity randomization log rows.
    """

    kind: str
    data: dict[str, Any]
    line_logs: list[dict[str, Any]] = field(default_factory=list)
    dg_logs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PhysicalEdge:
    """
    Represents one undirected physical edge used for graph/Y-bus construction.
    Field names are intentionally short because graph_arrays.py accesses them
    directly as edge.a, edge.b, edge.r_ohm, etc.
    """

    a: int
    b: int

    canonical_id: str
    representative_id: str

    length_km: float
    r_ohm: float
    x_ohm: float

    is_in_service: int

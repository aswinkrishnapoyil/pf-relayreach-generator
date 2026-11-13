# switch_state_islands.py
"""
Offline islanding check for a switch-state library.

Uses a switch inventory written by ``export_switch_inventory.py`` (v3.4.1 or
later) and a switch-state CSV. No PowerFactory is needed. For every switch
state, the closed lines are joined into a graph and each part of the grid is
checked for an external grid (ElmXnet).

Limits of the offline check (the generator's own check during a run has
none of these):
- Only objects with at least one switched cubicle in the selected grid
  appear in the inventory. A line without any switch is missing, which can
  make a supplied part look like an island.
- Elements outside the selected grid are not listed, so an external grid
  that feeds the grid through a transformer from outside is not seen.
- The state of couplers (ElmCoup) themselves is taken as closed; only their
  cubicle switches are evaluated.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..core.models import PowerFactoryStateError
from ..domain.islanding import find_unsupplied_islands

TERMINAL_SEPARATOR = "|"

LINE_CLASSES = {"ElmLne"}
COUPLER_CLASSES = {"ElmCoup"}
TRANSFORMER_CLASSES = {"ElmTr2", "ElmTr3"}
SOURCE_CLASSES = {"ElmXnet"}


@dataclass
class InventoryObject:
    full_name: str
    class_name: str
    out_of_service: bool
    terminals: list[str]
    # terminal full name -> inventory row positions of switches at that end
    switch_rows_by_terminal: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class SwitchInventory:
    rows: list[dict]
    objects: dict[str, InventoryObject]
    row_by_switch_id: dict[str, int]
    terminal_names: dict[str, str]


def _clean_id(value) -> str:
    return str(value or "").lstrip("_").strip()


def _parse_binary(value, default: int) -> int:
    s_value = str(value if value is not None else "").strip()
    if s_value == "":
        return default
    return 1 if int(float(s_value)) == 1 else 0


def read_switch_inventory(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8-sig") as o_file:
        return list(csv.DictReader(o_file))


def build_switch_inventory(rows: list[dict]) -> SwitchInventory:
    """
    Groups inventory rows by connected object and indexes the switch IDs.
    """

    d_objects: dict[str, InventoryObject] = {}
    d_row_by_switch_id: dict[str, int] = {}
    d_terminal_names: dict[str, str] = {}

    for i_row, d_row in enumerate(rows):
        s_terminal = str(d_row.get("terminal_full_name") or "").strip()
        d_terminal_names.setdefault(
            s_terminal,
            str(d_row.get("terminal_name") or "").strip() or s_terminal,
        )

        for s_id_column in (
                "user_independent_switch_id",
                "generated_switch_id",
                "switch_cim_id",
                "cubicle_cim_id",
        ):
            s_id = _clean_id(d_row.get(s_id_column))
            if not s_id:
                continue
            if s_id in d_row_by_switch_id and d_row_by_switch_id[s_id] != i_row:
                raise PowerFactoryStateError(f"Ambiguous switch identifier {s_id!r} in inventory")
            d_row_by_switch_id[s_id] = i_row

        s_object = str(d_row.get("connected_object_full_name") or "").strip()
        if not s_object:
            continue

        o_object = d_objects.get(s_object)
        if o_object is None:
            s_terminals = str(d_row.get("connected_object_terminal_full_names") or "")
            o_object = InventoryObject(
                full_name=s_object,
                class_name=str(d_row.get("connected_object_class") or "").strip(),
                out_of_service=_parse_binary(d_row.get("connected_object_outserv"), 0) == 1,
                terminals=[s for s in s_terminals.split(TERMINAL_SEPARATOR) if s],
            )
            d_objects[s_object] = o_object

        o_object.switch_rows_by_terminal.setdefault(s_terminal, []).append(i_row)

    # Older inventories (before v3.4.1) do not list the terminals; fall back
    # to the switched ends.
    for o_object in d_objects.values():
        if not o_object.terminals:
            o_object.terminals = list(o_object.switch_rows_by_terminal)

    l_incomplete = sorted(
        o_object.full_name
        for o_object in d_objects.values()
        if o_object.class_name in LINE_CLASSES | COUPLER_CLASSES
        and len(set(o_object.terminals)) < 2
    )
    if l_incomplete:
        raise PowerFactoryStateError(
            "The inventory does not give both ends of these branches: "
            f"{l_incomplete[:5]}. Export the inventory again with "
            "export_switch_inventory.py v3.4.1 or later."
        )

    if not any(o.class_name in SOURCE_CLASSES for o in d_objects.values()):
        raise PowerFactoryStateError(
            "The inventory lists no external grid (ElmXnet) inside the grid, "
            "so the offline islanding check cannot be used for this grid."
        )

    return SwitchInventory(
        rows=list(rows),
        objects=d_objects,
        row_by_switch_id=d_row_by_switch_id,
        terminal_names=d_terminal_names,
    )


def map_library_columns(inventory: SwitchInventory, switch_columns: list[str]) -> dict[str, int]:
    """
    Maps each switch_<id> column of the library to an inventory row.
    """

    d_mapping = {}
    l_unmapped = []
    for s_column in switch_columns:
        s_id = _clean_id(str(s_column).split("_", 1)[1])
        if s_id in inventory.row_by_switch_id:
            d_mapping[s_column] = inventory.row_by_switch_id[s_id]
        else:
            l_unmapped.append(s_column)
    if l_unmapped:
        raise PowerFactoryStateError(
            f"{len(l_unmapped)} library switch columns are not in the inventory, "
            f"for example {l_unmapped[:3]}"
        )
    return d_mapping


def find_islands_for_switch_state(
        inventory: SwitchInventory,
        switch_values: dict[str, int],
        column_rows: dict[str, int],
) -> list[list[str]]:
    """
    Returns the unsupplied islands (terminal names) for one switch state.
    """

    d_state_by_row = {
        i_row: _parse_binary(d_row.get("current_switch_state"), 1)
        for i_row, d_row in enumerate(inventory.rows)
    }
    for s_column, i_row in column_rows.items():
        d_state_by_row[i_row] = int(switch_values[s_column])

    def _end_closed(o_object: InventoryObject, s_terminal: str) -> bool:
        return all(
            d_state_by_row[i_row] == 1
            for i_row in o_object.switch_rows_by_terminal.get(s_terminal, [])
        )

    l_edges = []
    s_sources = set()
    s_checked = set()

    for o_object in inventory.objects.values():
        if o_object.out_of_service:
            continue

        if o_object.class_name in LINE_CLASSES | COUPLER_CLASSES:
            s_from, s_to = o_object.terminals[0], o_object.terminals[1]
            # Like the generator: a line is available only if both ends
            # are closed.
            if _end_closed(o_object, s_from) and _end_closed(o_object, s_to):
                l_edges.append((s_from, s_to))
                if o_object.class_name in LINE_CLASSES:
                    s_checked.update((s_from, s_to))

        elif o_object.class_name in TRANSFORMER_CLASSES:
            l_closed = [s for s in o_object.terminals if _end_closed(o_object, s)]
            for s_terminal in l_closed[1:]:
                l_edges.append((l_closed[0], s_terminal))

        elif o_object.class_name in SOURCE_CLASSES:
            for s_terminal in o_object.terminals:
                if _end_closed(o_object, s_terminal):
                    s_sources.add(s_terminal)

    l_islands = find_unsupplied_islands(l_edges, s_sources, s_checked)

    def _name(s_terminal: str) -> str:
        if s_terminal in inventory.terminal_names:
            return inventory.terminal_names[s_terminal]
        s_last = s_terminal.rsplit("\\", 1)[-1]
        return s_last.rsplit(".", 1)[0] if s_last.endswith(".ElmTerm") else s_last

    return [sorted(_name(s) for s in l_island) for l_island in l_islands]


def read_switch_library(path: Path) -> tuple[pd.DataFrame, list[str]]:
    df_library = pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig")
    l_switch_columns = [
        s_column for s_column in df_library.columns
        if str(s_column).startswith("switch_")
    ]
    if not l_switch_columns:
        raise PowerFactoryStateError("Switch-state CSV has no switch_ columns.")
    for s_column in l_switch_columns:
        sr_values = pd.to_numeric(df_library[s_column], errors="coerce")
        if not sr_values.isin([0, 1]).all():
            raise PowerFactoryStateError(
                f"Switch-state CSV requires 0/1 values: column={s_column!r}"
            )
        df_library[s_column] = sr_values.astype(int)
    return df_library, l_switch_columns


def find_islanded_switch_states(
        inventory_rows: list[dict],
        df_library: pd.DataFrame,
        switch_columns: list[str],
) -> list[dict]:
    """
    Returns one record per switch state that leaves busbars without an
    external grid. Row numbers and short IDs match the generator
    (switch_state_row_index = position + 1, ss_<position + 1>).
    """

    o_inventory = build_switch_inventory(inventory_rows)
    d_column_rows = map_library_columns(o_inventory, switch_columns)

    l_records = []
    for i_position, (_, sr_row) in enumerate(df_library.iterrows()):
        l_islands = find_islands_for_switch_state(
            o_inventory,
            {s_column: int(sr_row[s_column]) for s_column in switch_columns},
            d_column_rows,
        )
        if not l_islands:
            continue
        raw_config_id = sr_row.get("ConfigID", "")
        l_records.append({
            "switch_state_row_index": i_position + 1,
            "switch_state_short_id": f"ss_{i_position + 1:04d}",
            "switch_state_config_id": "" if pd.isna(raw_config_id) else str(raw_config_id),
            "island_count": len(l_islands),
            "island_terminals": l_islands,
        })
    return l_records

"""
Offline regression tests for the islanding check (v3.4.1).

The 14-bus fixtures mirror the two switch states of the 14-bus library that
separated part of the grid in v3.4.0:
- ss_0180: lines 4-7 and 7-9 open -> Terminal_HV_7 and Terminal_HV_8
  (busbar 8 has a DG unit, which is not a source for this check).
- ss_0192: lines 9-10 and 6-11 open -> Terminal_HV_10 and Terminal_HV_11.
"""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from pf_adaptive_distance_dataset.core.config import Config, PFAttr
from pf_adaptive_distance_dataset.core.models import (
    DatasetStatistics,
    PowerFactoryStateError,
)
from pf_adaptive_distance_dataset.domain import islanding
from pf_adaptive_distance_dataset.domain.islanding import (
    ISLANDED_SWITCH_STATE_REASON,
    find_live_unsupplied_islands,
    find_unsupplied_islands,
)
from pf_adaptive_distance_dataset.pipeline import batch_orchestrator
from pf_adaptive_distance_dataset.pipeline import dataset_generator as generator
from pf_adaptive_distance_dataset.pipeline.switch_state_islands import (
    find_islanded_switch_states,
)


IEEE14_LINES = [
    (1, 2), (1, 5), (2, 3), (2, 4), (2, 5), (3, 4), (4, 5), (4, 7), (4, 9),
    (5, 6), (6, 11), (6, 12), (6, 13), (7, 8), (7, 9), (9, 10), (9, 14),
    (10, 11), (12, 13), (13, 14),
]
XNET_BUS = 1
DG_BUS = 8
SS_0180_OPEN = {(4, 7), (7, 9)}
SS_0192_OPEN = {(9, 10), (6, 11)}


def bus(i):
    return f"Terminal_HV_{i}"


def line_name(a, b):
    return f"Line_{a}_{b}"


# ---------------------------------------------------------------------------
# Pure graph check
# ---------------------------------------------------------------------------

class FindUnsuppliedIslandsTest(unittest.TestCase):
    def _islands(self, open_lines):
        l_edges = [
            (bus(a), bus(b)) for a, b in IEEE14_LINES if (a, b) not in open_lines
        ]
        return find_unsupplied_islands(l_edges, {bus(XNET_BUS)})

    def test_intact_grid_has_no_island(self):
        self.assertEqual(self._islands(set()), [])

    def test_ss_0180_isolates_busbars_7_and_8(self):
        self.assertEqual(self._islands(SS_0180_OPEN), [[bus(7), bus(8)]])

    def test_ss_0192_isolates_busbars_10_and_11(self):
        self.assertEqual(self._islands(SS_0192_OPEN), [[bus(10), bus(11)]])

    def test_two_islands_are_reported_separately(self):
        self.assertEqual(
            self._islands(SS_0180_OPEN | SS_0192_OPEN),
            [[bus(10), bus(11)], [bus(7), bus(8)]],
        )

    def test_dg_bus_is_not_a_source(self):
        # Only external grids count; a DG unit cannot hold the island.
        l_edges = [
            (bus(a), bus(b)) for a, b in IEEE14_LINES if (a, b) not in SS_0180_OPEN
        ]
        self.assertEqual(
            find_unsupplied_islands(l_edges, {bus(XNET_BUS)}),
            [[bus(7), bus(8)]],
        )
        self.assertEqual(
            find_unsupplied_islands(l_edges, {bus(XNET_BUS), bus(DG_BUS)}),
            [],
        )

    def test_any_external_grid_supplies_its_part(self):
        l_edges = [("A", "B"), ("C", "D")]
        self.assertEqual(find_unsupplied_islands(l_edges, {"A", "D"}), [])
        self.assertEqual(find_unsupplied_islands(l_edges, set()), [["A", "B"], ["C", "D"]])

    def test_only_checked_nodes_are_reported(self):
        l_edges = [("A", "B"), ("C", "MV_1")]
        self.assertEqual(
            find_unsupplied_islands(l_edges, {"A"}, checked_nodes={"A", "B", "C"}),
            [["C"]],
        )
        self.assertEqual(
            find_unsupplied_islands(l_edges, {"A"}, checked_nodes={"A", "B"}),
            [],
        )

    def test_long_chain_does_not_hit_recursion_limit(self):
        l_edges = [(i, i + 1) for i in range(5000)]
        self.assertEqual(find_unsupplied_islands(l_edges, {0}), [])
        self.assertEqual(len(find_unsupplied_islands(l_edges, set())[0]), 5001)


# ---------------------------------------------------------------------------
# Live PowerFactory adapter with fake objects
# ---------------------------------------------------------------------------

class FakeObject:
    def __init__(self, name, class_name, parent=None, attrs=None, children=None):
        self.loc_name = name
        self._class_name = class_name
        self._parent = parent
        self._attrs = dict(attrs or {})
        self._children = list(children or [])

    def GetClassName(self):
        return self._class_name

    def GetFullName(self):
        s_parent = self._parent.GetFullName() + "\\" if self._parent else ""
        return f"{s_parent}{self.loc_name}.{self._class_name}"

    def GetParent(self):
        return self._parent

    def GetAttribute(self, name):
        return self._attrs.get(name)

    def SetAttribute(self, name, value):
        self._attrs[name] = value

    def GetChildren(self, _recursive, pattern):
        if pattern == "*.StaSwitch":
            return [o for o in self._children if o.GetClassName() == "StaSwitch"]
        return []


class FakeGrid:
    """A 14-bus 110 kV grid with one external grid and a DG unit at bus 8."""

    def __init__(self):
        self.project = FakeObject("Project", "IntPrj")
        self.grid = FakeObject("Grid_110kV", "ElmNet", parent=self.project)
        self.other_grid = FakeObject("Grid_380kV", "ElmNet", parent=self.project)
        self.terminals = {
            i: FakeObject(bus(i), "ElmTerm", parent=self.grid) for i in range(1, 15)
        }
        self.objects = {}
        self.switches = {}
        for a, b in IEEE14_LINES:
            self.add_branch("ElmLne", line_name(a, b), self.terminals[a], self.terminals[b])
        self.add_single("ElmXnet", "Xnet", self.terminals[XNET_BUS])
        self.add_single("ElmGenstat", "DG_8", self.terminals[DG_BUS])

    def _cubicle(self, name, terminal):
        o_switch = FakeObject("Switch", "StaSwitch", attrs={PFAttr.SWITCH_STATE: 1})
        o_cubicle = FakeObject(
            f"Cub_{name}", "StaCubic", parent=terminal,
            attrs={PFAttr.CTERM: terminal}, children=[o_switch],
        )
        return o_cubicle, o_switch

    def add_branch(self, class_name, name, term_a, term_b, attrs=(PFAttr.BUS1, PFAttr.BUS2), parent=None):
        d_attrs = {PFAttr.OUTSERV: 0}
        l_switches = []
        for s_attr, o_terminal in zip(attrs, (term_a, term_b)):
            o_cubicle, o_switch = self._cubicle(f"{name}_{s_attr}", o_terminal)
            d_attrs[s_attr] = o_cubicle
            l_switches.append(o_switch)
        if class_name == "ElmCoup":
            d_attrs[PFAttr.SWITCH_STATE] = 1
        o_object = FakeObject(name, class_name, parent=parent or self.grid, attrs=d_attrs)
        self.objects[name] = o_object
        self.switches[name] = l_switches
        return o_object

    def add_single(self, class_name, name, terminal):
        o_cubicle, o_switch = self._cubicle(name, terminal)
        o_object = FakeObject(name, class_name, parent=self.grid,
                              attrs={PFAttr.OUTSERV: 0, PFAttr.BUS1: o_cubicle})
        self.objects[name] = o_object
        self.switches[name] = [o_switch]
        return o_object

    def open_line(self, a, b, end=0):
        self.switches[line_name(a, b)][end].SetAttribute(PFAttr.SWITCH_STATE, 0)

    @property
    def app(self):
        d_by_class = {}
        for o_object in self.objects.values():
            d_by_class.setdefault("*." + o_object.GetClassName(), []).append(o_object)
        return SimpleNamespace(
            GetCalcRelevantObjects=lambda pattern: list(d_by_class.get(pattern, []))
        )


class LiveIslandDetectionTest(unittest.TestCase):
    def test_intact_grid_is_supplied(self):
        o_net = FakeGrid()
        self.assertEqual(find_live_unsupplied_islands(o_net.grid, o_net.app), [])

    def test_open_switches_of_ss_0180_create_island(self):
        o_net = FakeGrid()
        o_net.open_line(4, 7, end=0)
        o_net.open_line(7, 9, end=1)
        self.assertEqual(
            find_live_unsupplied_islands(o_net.grid, o_net.app),
            [[bus(7), bus(8)]],
        )

    def test_out_of_service_lines_of_ss_0192_create_island(self):
        o_net = FakeGrid()
        for a, b in SS_0192_OPEN:
            o_net.objects[line_name(a, b)].SetAttribute(PFAttr.OUTSERV, 1)
        self.assertEqual(
            find_live_unsupplied_islands(o_net.grid, o_net.app),
            [[bus(10), bus(11)]],
        )

    def test_external_grid_behind_open_switch_supplies_nothing(self):
        o_net = FakeGrid()
        o_net.switches["Xnet"][0].SetAttribute(PFAttr.SWITCH_STATE, 0)
        l_islands = find_live_unsupplied_islands(o_net.grid, o_net.app)
        self.assertEqual(l_islands, [sorted(bus(i) for i in range(1, 15))])

    def test_closed_coupler_joins_split_busbar(self):
        o_net = FakeGrid()
        o_net.open_line(4, 7)
        o_net.open_line(7, 9)
        o_net.add_branch("ElmCoup", "Coupler_4_7", o_net.terminals[4], o_net.terminals[7])
        self.assertEqual(find_live_unsupplied_islands(o_net.grid, o_net.app), [])
        o_net.objects["Coupler_4_7"].SetAttribute(PFAttr.SWITCH_STATE, 0)
        self.assertEqual(
            find_live_unsupplied_islands(o_net.grid, o_net.app),
            [[bus(7), bus(8)]],
        )

    def test_external_grid_behind_transformer_outside_grid_counts(self):
        o_net = FakeGrid()
        o_net.objects.pop("Xnet")
        o_ehv = FakeObject("Terminal_EHV", "ElmTerm", parent=o_net.other_grid)
        o_net.add_single("ElmXnet", "Xnet_EHV", o_ehv)
        o_net.objects["Xnet_EHV"]._parent = o_net.other_grid
        o_net.add_branch(
            "ElmTr2", "Tr_EHV_HV", o_ehv, o_net.terminals[1],
            attrs=(PFAttr.BUSHV, PFAttr.BUSLV), parent=o_net.other_grid,
        )
        self.assertEqual(find_live_unsupplied_islands(o_net.grid, o_net.app), [])
        # The outside terminal itself is never reported.
        o_net.objects["Tr_EHV_HV"].SetAttribute(PFAttr.OUTSERV, 1)
        l_islands = find_live_unsupplied_islands(o_net.grid, o_net.app)
        self.assertEqual(l_islands, [sorted(bus(i) for i in range(1, 15))])


# ---------------------------------------------------------------------------
# Generator: an islanded switch state is omitted completely
# ---------------------------------------------------------------------------

class IslandedSwitchStateOmissionTest(unittest.TestCase):
    def _run(self, islands, include_base=True):
        o_process = None
        with ExitStack() as o_stack:
            o_stack.enter_context(patch.multiple(
                Config,
                INCLUDE_ORIGINAL_BASE_CASE=include_base,
                RANDOMIZED_SCENARIO_COUNT=3,
                PERSISTENT_ZONE2_FAILURE_MIN_RANDOMIZATIONS=3,
            ))
            o_stack.enter_context(patch.object(generator, "_get_master_case_pair", return_value=(object(), object())))
            o_stack.enter_context(patch.object(generator, "delete_existing_slave_cases"))
            o_stack.enter_context(patch.object(
                generator, "_create_and_activate_slave_for_switch_state",
                return_value=(object(), object(), object()),
            ))
            o_stack.enter_context(patch.object(
                generator, "capture_original_state_from_active_slave",
                return_value=({}, {}, {}, {}, {}, [], [], {}),
            ))
            o_stack.enter_context(patch.object(
                generator, "_find_switch_state_islands",
                side_effect=lambda **kw: islands.get(kw["cfg"], []),
            ))

            def fake_process(**d_kwargs):
                d_kwargs["scenario_yields"].append(
                    generator.ExportPayload(kind="graph_row", data={"scenario_uid": d_kwargs["scenario_uid"]})
                )

            o_process = o_stack.enter_context(patch.object(
                generator, "_process_single_scenario", side_effect=fake_process,
            ))
            o_stack.enter_context(patch.object(generator, "_cleanup_after_scenario"))
            o_delete = o_stack.enter_context(patch.object(generator, "delete_slave_case_pair"))
            o_stack.enter_context(patch.object(generator, "_reactivate_master_and_delete_slaves"))

            df_states = pd.DataFrame([
                {"ConfigID": "cfg-supplied", "switch_1": 1},
                {"ConfigID": "cfg-island", "switch_1": 0},
            ])
            l_payloads = list(generator.generate_dataset_cases(
                SimpleNamespace(app=object(), project=object()),
                DatasetStatistics(), df_states, ["switch_1"], row_offset=179,
            ))
            return l_payloads, o_process, o_delete

    def test_islanded_state_is_omitted_with_one_record_per_scenario(self):
        l_islands = [[bus(7), bus(8)]]
        l_payloads, o_process, o_delete = self._run({"cfg-island": l_islands})

        l_graph = [o for o in l_payloads if o.kind == "graph_row"]
        l_skipped = [o for o in l_payloads if o.kind == "skipped_scenario"]
        self.assertEqual(len(l_graph), 4)
        self.assertTrue(all(o.data["scenario_uid"].startswith("ss_0180_") for o in l_graph))
        # The islanded state never reaches scenario processing.
        self.assertEqual(o_process.call_count, 4)
        self.assertEqual(o_delete.call_count, 2)

        self.assertEqual(
            [o.data["scenario_uid"] for o in l_skipped],
            ["ss_0181_base_0000", "ss_0181_rand_0001", "ss_0181_rand_0002", "ss_0181_rand_0003"],
        )
        for o_payload in l_skipped:
            self.assertEqual(o_payload.data["reason"], ISLANDED_SWITCH_STATE_REASON)
            self.assertEqual(o_payload.data["switch_state_row_index"], 181)
            self.assertEqual(o_payload.data["switch_state_config_id"], "cfg-island")
            self.assertEqual(o_payload.data["island_terminals"], l_islands)
            self.assertIn("Terminal_HV_7, Terminal_HV_8", o_payload.data["detail"])

    def test_records_pass_batch_coverage_validation(self):
        l_payloads, _, _ = self._run({"cfg-island": [[bus(10), bus(11)]]})
        with patch.multiple(Config, INCLUDE_ORIGINAL_BASE_CASE=True, RANDOMIZED_SCENARIO_COUNT=3):
            batch_orchestrator._validate_scenario_coverage(
                batch_orchestrator._expected_scenario_ids(180, 181),
                [o.data["scenario_uid"] for o in l_payloads if o.kind == "graph_row"],
                [180] * 4,
                [o.data for o in l_payloads if o.kind == "skipped_scenario"],
            )

    def test_without_base_case_only_randomised_scenarios_are_recorded(self):
        l_payloads, _, _ = self._run({"cfg-island": [[bus(7)]]}, include_base=False)
        self.assertEqual(
            [o.data["scenario_uid"] for o in l_payloads if o.kind == "skipped_scenario"],
            ["ss_0181_rand_0001", "ss_0181_rand_0002", "ss_0181_rand_0003"],
        )

    def test_island_check_restores_slave_state(self):
        with ExitStack() as o_stack:
            o_apply = o_stack.enter_context(patch.object(generator, "_restore_and_apply_switch_state"))
            o_restore = o_stack.enter_context(patch.object(generator, "restore_grid_state"))
            o_stack.enter_context(patch.object(
                generator, "find_live_unsupplied_islands", side_effect=RuntimeError("PF failed"),
            ))
            with self.assertRaises(RuntimeError):
                generator._find_switch_state_islands(
                    grid=None, app=None, row={}, cfg="c", cub_lookup={}, out_objs=[],
                    orig_lines={}, orig_dgs={}, orig_out={}, orig_sw={},
                )
            self.assertEqual(o_apply.call_count, 1)
            self.assertEqual(o_restore.call_count, 1)


# ---------------------------------------------------------------------------
# Offline library check (inventory + switch-state CSV)
# ---------------------------------------------------------------------------

INVENTORY_FIELDS = [
    "generated_switch_id", "switch_cim_id", "cubicle_cim_id", "terminal_name",
    "terminal_full_name", "connected_object_class", "connected_object_full_name",
    "current_switch_state", "connected_object_outserv",
    "connected_object_terminal_full_names",
]


def term_full(i):
    return f"\\Grid_110kV.ElmNet\\{bus(i)}.ElmTerm"


def build_inventory(with_terminal_column=True):
    l_rows = []
    for a, b in IEEE14_LINES:
        for i_end, i_bus in enumerate((a, b)):
            l_rows.append({
                "generated_switch_id": f"sw-{a}-{b}-{i_end}",
                "switch_cim_id": "",
                "cubicle_cim_id": f"cub-{a}-{b}-{i_end}",
                "terminal_name": bus(i_bus),
                "terminal_full_name": term_full(i_bus),
                "connected_object_class": "ElmLne",
                "connected_object_full_name": f"\\Grid_110kV.ElmNet\\{line_name(a, b)}.ElmLne",
                "current_switch_state": "1",
                "connected_object_outserv": "0",
                "connected_object_terminal_full_names": (
                    f"{term_full(a)}|{term_full(b)}" if with_terminal_column else ""
                ),
            })
    for s_name, s_class, i_bus in (("Xnet", "ElmXnet", XNET_BUS), ("DG_8", "ElmGenstat", DG_BUS)):
        l_rows.append({
            "generated_switch_id": f"sw-{s_name}",
            "switch_cim_id": "",
            "cubicle_cim_id": "",
            "terminal_name": bus(i_bus),
            "terminal_full_name": term_full(i_bus),
            "connected_object_class": s_class,
            "connected_object_full_name": f"\\Grid_110kV.ElmNet\\{s_name}.{s_class}",
            "current_switch_state": "1",
            "connected_object_outserv": "0",
            "connected_object_terminal_full_names": term_full(i_bus) if with_terminal_column else "",
        })
    return l_rows


def build_library(open_sets):
    l_columns = [f"switch_sw-{a}-{b}-{i}" for a, b in IEEE14_LINES for i in (0, 1)]
    l_rows = []
    for i_state, s_open in enumerate(open_sets):
        d_row = {"ConfigID": f"cfg-{i_state}"}
        for a, b in IEEE14_LINES:
            for i in (0, 1):
                # Open one end only: the line is still unavailable.
                d_row[f"switch_sw-{a}-{b}-{i}"] = 0 if ((a, b) in s_open and i == 0) else 1
        l_rows.append(d_row)
    return pd.DataFrame(l_rows), l_columns


class OfflineLibraryCheckTest(unittest.TestCase):
    def test_library_check_lists_only_islanded_states(self):
        df_library, l_columns = build_library([set(), SS_0180_OPEN, {(1, 2)}, SS_0192_OPEN])
        l_records = find_islanded_switch_states(build_inventory(), df_library, l_columns)
        self.assertEqual(
            [(d["switch_state_short_id"], d["switch_state_row_index"], d["switch_state_config_id"])
             for d in l_records],
            [("ss_0002", 2, "cfg-1"), ("ss_0004", 4, "cfg-3")],
        )
        self.assertEqual(l_records[0]["island_terminals"], [[bus(7), bus(8)]])
        self.assertEqual(l_records[1]["island_terminals"], [[bus(10), bus(11)]])

    def test_out_of_service_line_in_inventory_is_respected(self):
        l_inventory = build_inventory()
        for d_row in l_inventory:
            if d_row["connected_object_full_name"].endswith(f"{line_name(9, 10)}.ElmLne"):
                d_row["connected_object_outserv"] = "1"
        df_library, l_columns = build_library([{(6, 11)}])
        l_records = find_islanded_switch_states(l_inventory, df_library, l_columns)
        self.assertEqual(l_records[0]["island_terminals"], [[bus(10), bus(11)]])

    def test_old_inventory_without_terminal_column_still_works_when_complete(self):
        df_library, l_columns = build_library([SS_0180_OPEN])
        l_records = find_islanded_switch_states(
            build_inventory(with_terminal_column=False), df_library, l_columns,
        )
        self.assertEqual(l_records[0]["island_terminals"], [[bus(7), bus(8)]])

    def test_user_independent_switch_ids_are_mapped(self):
        l_inventory = build_inventory()
        for d_row in l_inventory:
            d_row["user_independent_switch_id"] = "ui-" + d_row["generated_switch_id"]
        df_library, l_columns = build_library([SS_0192_OPEN])
        df_library = df_library.rename(columns={
            s: s.replace("switch_sw-", "switch_ui-sw-") for s in l_columns
        })
        l_columns = [s.replace("switch_sw-", "switch_ui-sw-") for s in l_columns]
        l_records = find_islanded_switch_states(l_inventory, df_library, l_columns)
        self.assertEqual(l_records[0]["island_terminals"], [[bus(10), bus(11)]])

    def test_incomplete_or_unusable_inventory_is_rejected(self):
        df_library, l_columns = build_library([set()])
        l_inventory = [
            d for d in build_inventory(with_terminal_column=False)
            if d["generated_switch_id"] != "sw-7-8-1"
        ]
        with self.assertRaisesRegex(PowerFactoryStateError, "both ends"):
            find_islanded_switch_states(l_inventory, df_library, l_columns[:-1])

        l_inventory = [d for d in build_inventory() if d["connected_object_class"] != "ElmXnet"]
        with self.assertRaisesRegex(PowerFactoryStateError, "no external grid"):
            find_islanded_switch_states(l_inventory, df_library, l_columns)

        df_library["switch_unknown"] = 1
        with self.assertRaisesRegex(PowerFactoryStateError, "not in the inventory"):
            find_islanded_switch_states(build_inventory(), df_library, l_columns + ["switch_unknown"])


if __name__ == "__main__":
    unittest.main()

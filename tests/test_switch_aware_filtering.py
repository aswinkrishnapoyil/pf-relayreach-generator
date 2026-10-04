import unittest
import uuid
import math
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from pf_adaptive_distance_dataset.core.config import PFAttr, Config
from pf_adaptive_distance_dataset.core.models import PowerFactoryStateError, ShortCircuitCalculationError
from pf_adaptive_distance_dataset.domain import infeed
from pf_adaptive_distance_dataset.pipeline import switch_states
from pf_adaptive_distance_dataset.domain.infeed import (
    calculate_zone_infeed_summary_for_turbines,
)
from pf_adaptive_distance_dataset.domain.topology import (
    get_terminal_connected_distributed_generators,
)
from pf_adaptive_distance_dataset.pipeline.switch_states import (
    apply_switch_state,
    build_cubicle_lookup,
)


class FakePowerFactoryObject:
    def __init__(
        self,
        name,
        class_name,
        full_name=None,
        parent=None,
        attrs=None,
        children=None,
        connected=None,
    ):
        self.loc_name = name
        self._class_name = class_name
        self._full_name = full_name or name
        self._parent = parent
        self._attrs = dict(attrs or {})
        self._children = list(children or [])
        self._connected = list(connected or [])

    def GetClassName(self):
        return self._class_name

    def GetFullName(self):
        return self._full_name

    def GetParent(self):
        return self._parent

    def GetAttribute(self, name):
        return self._attrs.get(name)

    def SetAttribute(self, name, value):
        self._attrs[name] = value

    def GetChildren(self, _recursive, pattern):
        if pattern == "*.StaSwitch":
            return [
                o_child
                for o_child in self._children
                if o_child.GetClassName() == "StaSwitch"
            ]
        return []

    def GetConnectedElements(self):
        return list(self._connected)


class FakeApplication:
    def __init__(self, project, cubicles):
        self._project = project
        self._cubicles = list(cubicles)

    def GetActiveProject(self):
        return self._project

    def GetCalcRelevantObjects(self, pattern):
        if pattern == "*.StaCubic":
            return list(self._cubicles)
        return []


def generated_switch_id(project_name, grid_name, switch, cubicle):
    identity = (
        "powerfactory://"
        f"{project_name}|"
        f"{grid_name}|"
        "switch="
        f"{switch.GetClassName()}:"
        f"{switch.GetFullName()}|"
        "reference="
        f"{cubicle.GetClassName()}:"
        f"{cubicle.GetFullName()}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


class SwitchAwareFilteringTest(unittest.TestCase):
    def test_invalid_switch_csv_values_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="pf_switch_csv_") as s_temp_dir:
            p_csv = Path(s_temp_dir) / "switch_states.csv"
            with patch.object(switch_states, "SWITCH_STATE_FILE", p_csv), patch.object(Config, "ENABLE_SWITCH_STATE_SCENARIOS", True):
                for s_value in ("", "abc", "0.5", "2", "-1", "inf"):
                    with self.subTest(value=s_value):
                        p_csv.write_text(f"ConfigID,switch_x\nss_1,{s_value}\n", encoding="utf-8")
                        with self.assertRaisesRegex(PowerFactoryStateError, "requires 0/1"):
                            switch_states.load_switch_state_dataframe()
                p_csv.write_text("ConfigID,switch_x\nss_1,0\nss_2,1\n", encoding="utf-8")
                with patch.object(Config, "MAX_SWITCH_STATE_CONFIG_COUNT", None):
                    df_states, _ = switch_states.load_switch_state_dataframe()
                self.assertEqual(df_states["switch_x"].tolist(), [0, 1])

    def test_switch_preflight_rejects_missing_and_conflicting_mappings_before_writes(self):
        o_switch = FakePowerFactoryObject("Switch", "StaSwitch", attrs={PFAttr.SWITCH_STATE: 1})
        o_cubicle = FakePowerFactoryObject("Cub", "StaCubic", children=[o_switch])
        with self.assertRaisesRegex(PowerFactoryStateError, "unmapped"):
            apply_switch_state({"switch_known": 0, "switch_missing": 0}, {"known": o_cubicle}, "test")
        self.assertEqual(o_switch.GetAttribute(PFAttr.SWITCH_STATE), 1)
        with self.assertRaisesRegex(PowerFactoryStateError, "conflicting aliases"):
            apply_switch_state({"switch_a": 0, "switch_b": 1}, {"a": o_cubicle, "b": o_cubicle}, "test")
        self.assertEqual(o_switch.GetAttribute(PFAttr.SWITCH_STATE), 1)
        with self.assertRaisesRegex(PowerFactoryStateError, "Ambiguous switch identifier"):
            switch_states._add_cubicle_lookup_key({"id": o_cubicle}, "id", FakePowerFactoryObject("Other", "StaCubic"))

    def test_ignored_switch_write_is_detected_by_readback(self):
        o_switch = FakePowerFactoryObject("Switch", "StaSwitch", attrs={PFAttr.SWITCH_STATE: 1})
        o_cubicle = FakePowerFactoryObject("Cub", "StaCubic", children=[o_switch])
        with patch.object(o_switch, "SetAttribute", return_value=None):
            with self.assertRaisesRegex(PowerFactoryStateError, "Read-back mismatch"):
                apply_switch_state({"switch_x": 0}, {"x": o_cubicle}, "test")

    def _calculate_test_infeed(self, l_success, f_dg_current=2.0, f_reference_current=1.0, b_separate_wrappers=False):
        o_terminal = FakePowerFactoryObject("Bus", "ElmTerm")
        o_cubicle = FakePowerFactoryObject("Cub", "StaCubic", attrs={PFAttr.CTERM: o_terminal})
        o_line = FakePowerFactoryObject("Line", "ElmLne")
        self.l_test_dgs = [
            FakePowerFactoryObject(f"DG_{i}", "ElmGenstat", attrs={PFAttr.BUS1: o_cubicle, PFAttr.OUTSERV: 0, "sgn": 5.0})
            for i in range(len(l_success))
        ]
        l_candidates = self.l_test_dgs
        if b_separate_wrappers:
            l_candidates = []
            for o_dg in self.l_test_dgs:
                o_wrapper = FakePowerFactoryObject(
                    o_dg.loc_name, o_dg.GetClassName(), full_name=o_dg.GetFullName(),
                )
                # Separate Python wrappers, shared underlying PowerFactory state.
                o_wrapper._attrs = o_dg._attrs
                self.assertIsNot(o_wrapper, o_dg)
                l_candidates.extend([o_wrapper, o_wrapper])
        self.l_test_isolation_states = []

        def execute_test_fault(*_args):
            self.l_test_isolation_states.append([
                o_dg.GetAttribute(PFAttr.OUTSERV) for o_dg in self.l_test_dgs
            ])
            return l_success[len(self.l_test_isolation_states) - 1]

        d_fault = {"fault_line": o_line, "fault_percent": 50.0, "local_fault_distance_km": 1.0, "fault_distance_km": 1.0}
        d_returns = {
            "build_ordered_path_segments": [{}],
            "select_fault_location_by_reach_impedance": (None, 0.0, 0.0, 2.0),
            "get_terminal_distance_on_ordered_path": 0.5,
            "select_shc_fault_location_for_dg_context": d_fault,
            "get_ikss_value": f_dg_current,
            "read_relay_reference_ikss": (f_reference_current, "line.current"),
            "calculate_path_impedance_between_distances": (2.0, 3.0),
        }
        with ExitStack() as o_stack:
            for s_name, value in d_returns.items():
                o_stack.enter_context(patch.object(infeed, s_name, return_value=value))
            o_stack.enter_context(patch.object(infeed, "execute_short_circuit_at_line_fault", side_effect=execute_test_fault))
            return calculate_zone_infeed_summary_for_turbines(
                None, None, None, o_terminal, o_cubicle, o_line, PFAttr.IKSS_BUS1,
                l_candidates, [o_line], 2.0, 3.0, 99.0, "zone2", l_all_grid_dg=self.l_test_dgs,
            )

    def test_distinct_dg_wrappers_are_counted_once_and_isolated_correctly(self):
        d_summary = self._calculate_test_infeed([True, True], b_separate_wrappers=True)
        self.assertEqual(d_summary["turbines_candidate_count"], 2)
        self.assertEqual(d_summary["turbines_considered_count"], 2)
        self.assertEqual(d_summary["infeed_correction_r_ohm"], 8.0)
        self.assertEqual(d_summary["infeed_correction_x_ohm"], 12.0)
        self.assertEqual(self.l_test_isolation_states, [[0, 1], [1, 0]])
        self.assertEqual([o_dg.GetAttribute(PFAttr.OUTSERV) for o_dg in self.l_test_dgs], [0, 0])

        with self.assertRaisesRegex(ShortCircuitCalculationError, "DG=DG_1.*execution failed"):
            self._calculate_test_infeed([True, False], b_separate_wrappers=True)
        self.assertEqual([o_dg.GetAttribute(PFAttr.OUTSERV) for o_dg in self.l_test_dgs], [0, 0])

    def test_wrapper_matching_keeps_open_out_of_service_and_other_grid_dgs_excluded(self):
        for s_case in ("open_switch", "out_of_service", "other_grid"):
            with self.subTest(case=s_case):
                o_switch = FakePowerFactoryObject("Switch", "StaSwitch", attrs={PFAttr.SWITCH_STATE: int(s_case != "open_switch")})
                o_terminal = FakePowerFactoryObject("Bus", "ElmTerm")
                o_cubicle = FakePowerFactoryObject("Cub", "StaCubic", attrs={PFAttr.CTERM: o_terminal}, children=[o_switch])
                o_cached = FakePowerFactoryObject("Wind", "ElmGenstat", full_name="Project/GridA/Wind.ElmGenstat", attrs={PFAttr.BUS1: o_cubicle, PFAttr.OUTSERV: int(s_case == "out_of_service")})
                o_candidate = FakePowerFactoryObject("Wind", "ElmGenstat", full_name=("Project/GridB/Wind.ElmGenstat" if s_case == "other_grid" else o_cached.GetFullName()))
                o_candidate._attrs = o_cached._attrs
                with patch.object(infeed, "execute_short_circuit_at_line_fault") as o_execute, patch.object(infeed, "build_ordered_path_segments") as o_path:
                    d_summary = calculate_zone_infeed_summary_for_turbines(
                        None, None, None, None, None, None, "", [o_candidate], [],
                        0.0, 0.0, 0.0, "zone2", l_all_grid_dg=[o_cached],
                    )
                self.assertEqual(d_summary["turbines_candidate_count"], 0)
                o_execute.assert_not_called()
                o_path.assert_not_called()
                self.assertEqual(o_cached.GetAttribute(PFAttr.OUTSERV), int(s_case == "out_of_service"))

    def test_dg_activation_matches_full_path_and_rejects_unknown_before_writes(self):
        o_cached = FakePowerFactoryObject("Wind", "ElmGenstat", full_name="Project/GridA/Wind.ElmGenstat", attrs={PFAttr.OUTSERV: 0})
        o_other = FakePowerFactoryObject("Wind", "ElmGenstat", full_name="Project/GridB/Wind.ElmGenstat", attrs={PFAttr.OUTSERV: 0})
        o_wrapper = FakePowerFactoryObject("Wind", "ElmGenstat", full_name=o_cached.GetFullName())
        infeed.activate_only_one_distributed_generator([o_cached, o_other], o_wrapper)
        self.assertEqual([o_cached.GetAttribute(PFAttr.OUTSERV), o_other.GetAttribute(PFAttr.OUTSERV)], [0, 1])
        o_unknown = FakePowerFactoryObject("Wind", "ElmGenstat", full_name="Project/GridC/Wind.ElmGenstat")
        with self.assertRaisesRegex(PowerFactoryStateError, "not in the DG registry"):
            infeed.activate_only_one_distributed_generator([o_cached, o_other], o_unknown)
        self.assertEqual([o_cached.GetAttribute(PFAttr.OUTSERV), o_other.GetAttribute(PFAttr.OUTSERV)], [0, 1])

    def test_missing_dg_full_name_fails_instead_of_using_display_name(self):
        o_dg = FakePowerFactoryObject("Wind", "ElmGenstat", attrs={PFAttr.OUTSERV: 0})
        for value in (None, "", "   "):
            with self.subTest(full_name=value), patch.object(o_dg, "GetFullName", return_value=value):
                with self.assertRaisesRegex(PowerFactoryStateError, "stable DG identity"):
                    infeed.activate_only_one_distributed_generator([o_dg], o_dg)
        with patch.object(o_dg, "GetFullName", side_effect=RuntimeError("unavailable")):
            with self.assertRaisesRegex(PowerFactoryStateError, "stable DG identity"):
                infeed.activate_only_one_distributed_generator([o_dg], o_dg)

    def test_failed_second_short_circuit_discards_partial_sum_and_restores_all_dgs(self):
        with self.assertRaisesRegex(ShortCircuitCalculationError, "DG=DG_1.*execution failed"):
            self._calculate_test_infeed([True, False])
        self.assertEqual([o_dg.GetAttribute(PFAttr.OUTSERV) for o_dg in self.l_test_dgs], [0, 0])

    def test_missing_current_never_uses_the_old_reference_as_fallback(self):
        for f_current in (0.0, -1.0, math.nan, math.inf):
            with self.subTest(reference=f_current):
                with self.assertRaisesRegex(ShortCircuitCalculationError, "current from this fault"):
                    self._calculate_test_infeed([True], f_reference_current=f_current)
                self.assertEqual(self.l_test_dgs[0].GetAttribute(PFAttr.OUTSERV), 0)
        with self.assertRaisesRegex(ShortCircuitCalculationError, "DG current"):
            self._calculate_test_infeed([True], f_dg_current=None)

    def test_successful_current_ratio_and_true_zero_keep_the_existing_formula(self):
        d_summary = self._calculate_test_infeed([True])
        self.assertEqual(d_summary["infeed_correction_r_ohm"], 4.0)
        self.assertEqual(d_summary["infeed_correction_x_ohm"], 6.0)
        d_zero = self._calculate_test_infeed([True], f_dg_current=0.0)
        self.assertEqual(d_zero["infeed_correction_r_ohm"], 0.0)
        o_dg = FakePowerFactoryObject("DG", "ElmGenstat")
        self.assertIsNone(infeed.get_ikss_value(o_dg))
        o_dg.SetAttribute(PFAttr.IKSS_BUS1, 0.0)
        self.assertEqual(infeed.get_ikss_value(o_dg), 0.0)

    def test_fault_setup_is_verified_and_missing_location_prevents_execution(self):
        o_line = FakePowerFactoryObject("Line", "ElmLne")
        o_command = FakePowerFactoryObject("SHC", "ComShc", attrs={"ppro": 0.0})
        with patch.object(infeed, "get_short_circuit_command", return_value=o_command), patch.object(
            o_command, "Execute", return_value=0, create=True,
        ) as o_execute:
            self.assertTrue(infeed.execute_short_circuit_at_line_fault(None, None, o_line, 50.0))
            self.assertIs(o_command.GetAttribute("shcobj"), o_line)
            self.assertEqual(o_command.GetAttribute("ppro"), 50.0)
            o_execute.assert_called_once()
            o_execute.reset_mock()
            del o_command._attrs["ppro"]
            with self.assertRaisesRegex(ShortCircuitCalculationError, "fault-location"):
                infeed.execute_short_circuit_at_line_fault(None, None, o_line, 50.0)
            o_execute.assert_not_called()

    def test_zero_relay_side_current_is_not_replaced_by_generic_line_current(self):
        o_line = FakePowerFactoryObject("Line", "ElmLne", attrs={PFAttr.IKSS_BUS1: 0.0, PFAttr.IKSS: 100.0})
        self.assertEqual(infeed.read_relay_reference_ikss(None, o_line, PFAttr.IKSS_BUS1), (0.0, f"line.{PFAttr.IKSS_BUS1}"))

    def test_generated_uuid_switch_state_column_maps_to_cubicle(self):
        project = FakePowerFactoryObject("ExampleProject", "IntPrj")
        grid = FakePowerFactoryObject(
            "Grid_110kV",
            "ElmNet",
            full_name="Project\\Grid_110kV.ElmNet",
        )
        terminal = FakePowerFactoryObject(
            "Terminal_HV_2",
            "ElmTerm",
            full_name="Project\\Grid_110kV.ElmNet\\Terminal_HV_2.ElmTerm",
            parent=grid,
        )
        switch = FakePowerFactoryObject(
            "Switch",
            "StaSwitch",
            full_name=(
                "Project\\Grid_110kV.ElmNet\\Terminal_HV_2.ElmTerm"
                "\\Cub_1.StaCubic\\Switch.StaSwitch"
            ),
            attrs={PFAttr.SWITCH_STATE: 1},
        )
        cubicle = FakePowerFactoryObject(
            "Cub_1",
            "StaCubic",
            full_name="Project\\Grid_110kV.ElmNet\\Terminal_HV_2.ElmTerm\\Cub_1.StaCubic",
            attrs={PFAttr.CTERM: terminal},
            children=[switch],
        )
        app = FakeApplication(project, [cubicle])

        lookup = build_cubicle_lookup(app, grid)
        switch_id = generated_switch_id(
            "ExampleProject",
            "Grid_110kV",
            switch,
            cubicle,
        )

        self.assertIs(lookup[switch_id], cubicle)

        apply_switch_state(
            {f"switch_{switch_id}": 0},
            lookup,
            "ss_test",
        )

        self.assertEqual(switch.GetAttribute(PFAttr.SWITCH_STATE), 0)

    def test_open_dg_cubicle_is_not_connected_or_used_for_infeed(self):
        terminal = FakePowerFactoryObject("Terminal_HV_2", "ElmTerm")
        switch = FakePowerFactoryObject(
            "Switch",
            "StaSwitch",
            attrs={PFAttr.SWITCH_STATE: 0},
        )
        cubicle = FakePowerFactoryObject(
            "Cub_1",
            "StaCubic",
            attrs={PFAttr.CTERM: terminal},
            children=[switch],
        )
        dg = FakePowerFactoryObject(
            "Wind_HV_2",
            "ElmGenstat",
            attrs={
                PFAttr.BUS1: cubicle,
                PFAttr.OUTSERV: 0,
            },
        )
        terminal._connected = [dg]

        self.assertEqual(
            get_terminal_connected_distributed_generators(terminal),
            [],
        )

        summary = calculate_zone_infeed_summary_for_turbines(
            None,
            None,
            None,
            None,
            None,
            None,
            "",
            [dg],
            [],
            0.0,
            0.0,
            0.0,
            "zone2",
            l_all_grid_dg=[dg],
        )

        self.assertEqual(summary["turbines_candidate_count"], 0)
        self.assertEqual(summary["turbines_considered_count"], 0)
        self.assertEqual(summary["turbines_candidate_id"], "")


if __name__ == "__main__":
    unittest.main()

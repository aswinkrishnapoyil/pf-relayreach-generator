import struct
import unittest
from unittest.mock import Mock, patch

from pf_adaptive_distance_dataset.core.config import PFAttr
from pf_adaptive_distance_dataset.core.models import PowerFactoryStateError
from pf_adaptive_distance_dataset.pf_api.grid_state import restore_grid_state
from pf_adaptive_distance_dataset.pf_api.pf_utils import (
    set_pf_attribute_verified,
    verify_pf_attribute,
)
from pf_adaptive_distance_dataset.pipeline.dataset_generator import _cleanup_after_scenario
from pf_adaptive_distance_dataset.pipeline.randomization import (
    apply_random_line_parameter_scenario,
)


class FakePowerFactoryObject:
    def __init__(self, name, class_name, parent=None, attrs=None):
        self.loc_name = name
        self._class_name = class_name
        self._parent = parent
        self._attrs = dict(attrs or {})
        self.deleted = False

    def GetClassName(self):
        return self._class_name

    def GetFullName(self):
        return self.loc_name

    def GetParent(self):
        return self._parent

    def GetAttribute(self, name):
        return self._attrs.get(name)

    def SetAttribute(self, name, value):
        if name == "loc_name":
            self.loc_name = value
        self._attrs[name] = value

    def Delete(self):
        self.deleted = True


class FakeTypeLibrary:
    def __init__(self):
        self.copies = []

    def AddCopy(self, obj, name=None):
        copy = FakePowerFactoryObject(
            name or f"{obj.loc_name}_copy",
            obj.GetClassName(),
            parent=self,
            attrs=dict(obj._attrs),
        )
        self.copies.append(copy)
        return copy


class LineRandomizationTest(unittest.TestCase):
    def test_failed_type_restoration_retains_clone_and_attempts_other_settings(self):
        o_original = FakePowerFactoryObject("Original", "TypLne")
        o_clone = FakePowerFactoryObject("Clone", "TypLne")
        o_line = FakePowerFactoryObject("Line", "ElmLne", attrs={PFAttr.LINE_TYPE: o_clone, PFAttr.LINE_LENGTH: 12.0})
        o_dg = FakePowerFactoryObject("DG", "ElmGenstat", attrs={"sgn": 9.0})
        d_state = {"obj": o_line, "typ_id": o_original, "l": 10.0, "temporary_line_types": [o_clone]}
        o_original_set = o_line.SetAttribute

        def ignore_type_write(s_attr, value):
            if s_attr != PFAttr.LINE_TYPE:
                o_original_set(s_attr, value)

        with patch.object(o_line, "SetAttribute", side_effect=ignore_type_write):
            with self.assertRaisesRegex(PowerFactoryStateError, "Grid restoration failed"):
                restore_grid_state({"line": d_state}, {"dg": {"obj": o_dg, "attr": "sgn", "cap": 5.0}}, {}, {})
        self.assertFalse(o_clone.deleted)
        self.assertEqual(d_state["temporary_line_types"], [o_clone])
        self.assertEqual(o_line.GetAttribute(PFAttr.LINE_LENGTH), 10.0)
        self.assertEqual(o_dg.GetAttribute("sgn"), 5.0)

    def test_failed_clone_deletion_is_reported_and_retained_for_cleanup(self):
        o_original = FakePowerFactoryObject("Original", "TypLne")
        o_clone = FakePowerFactoryObject("Clone", "TypLne")
        o_line = FakePowerFactoryObject("Line", "ElmLne", attrs={PFAttr.LINE_TYPE: o_clone, PFAttr.LINE_LENGTH: 12.0})
        d_state = {"obj": o_line, "typ_id": o_original, "l": 10.0, "temporary_line_types": [o_clone]}
        with patch.object(o_clone, "Delete", return_value=1):
            with self.assertRaisesRegex(PowerFactoryStateError, "Cannot delete temporary type"):
                restore_grid_state({"line": d_state}, {}, {}, {})
        self.assertEqual(d_state["temporary_line_types"], [o_clone])
        self.assertIs(o_line.GetAttribute(PFAttr.LINE_TYPE), o_original)

    def test_restoration_failure_propagates_out_of_scenario_cleanup(self):
        o_app = Mock()
        with patch("pf_adaptive_distance_dataset.pipeline.dataset_generator.restore_grid_state", side_effect=PowerFactoryStateError("test failure")):
            with self.assertRaisesRegex(PowerFactoryStateError, "ss_test"):
                _cleanup_after_scenario(o_app, "ss_test", {}, {}, {}, {})
        o_app.ResetCalculation.assert_not_called()

    def test_float_readback_allows_roundoff_but_rejects_a_different_setting(self):
        o_line = FakePowerFactoryObject("Line", "ElmLne", attrs={PFAttr.LINE_LENGTH: 10.0 + 1e-10})
        verify_pf_attribute(o_line, PFAttr.LINE_LENGTH, 10.0)
        o_line.SetAttribute(PFAttr.LINE_LENGTH, 10.1)
        with self.assertRaises(PowerFactoryStateError):
            verify_pf_attribute(o_line, PFAttr.LINE_LENGTH, 10.0)

    def test_float32_readback_accepts_live_rounding_but_not_wrong_values(self):
        o_object = FakePowerFactoryObject("Readback", "ElmLne")
        o_original_set = o_object.SetAttribute

        def set_float32(s_attribute, f_value):
            f_stored = struct.unpack("f", struct.pack("f", f_value))[0]
            o_original_set(s_attribute, f_stored)

        # First pair reproduces the live 2026-09-15 dline failure exactly.
        l_settings = [
            (PFAttr.LINE_LENGTH, 18.497309827819105),
            ("rline", 0.06123456789),
            ("sgn", 47.123456789),
            ("ppro", 83.123456789),
            (PFAttr.LINE_LENGTH, 0.00123456789),
        ]
        with patch.object(o_object, "SetAttribute", side_effect=set_float32):
            for s_attribute, f_expected in l_settings:
                with self.subTest(attribute=s_attribute, expected=f_expected):
                    self.assertTrue(set_pf_attribute_verified(o_object, s_attribute, f_expected))
                    if f_expected == 18.497309827819105:
                        self.assertEqual(o_object.GetAttribute(s_attribute), 18.497310638427734)
                    with self.assertRaises(PowerFactoryStateError):
                        verify_pf_attribute(o_object, s_attribute, f_expected * 1.00001)

        for f_invalid in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(invalid=f_invalid):
                o_original_set(PFAttr.LINE_LENGTH, f_invalid)
                with self.assertRaises(PowerFactoryStateError):
                    verify_pf_attribute(o_object, PFAttr.LINE_LENGTH, 18.497309827819105)

        o_original_set(PFAttr.SWITCH_STATE, 0)
        with self.assertRaises(PowerFactoryStateError):
            verify_pf_attribute(o_object, PFAttr.SWITCH_STATE, 1)
        o_original_set(PFAttr.LINE_TYPE, object())
        with self.assertRaises(PowerFactoryStateError):
            verify_pf_attribute(o_object, PFAttr.LINE_TYPE, object())

    def test_typ_lne_randomizes_length_and_rx_ratio_using_temporary_type(self):
        library = FakeTypeLibrary()
        line_type = FakePowerFactoryObject(
            "LineType",
            "TypLne",
            parent=library,
            attrs={
                "rline": 0.12,
                "xline": 0.10,
            },
        )
        line = FakePowerFactoryObject(
            "Line_1",
            "ElmLne",
            attrs={
                PFAttr.LINE_TYPE: line_type,
                PFAttr.LINE_LENGTH: 10.0,
                PFAttr.LINE_R: 1.2,
                PFAttr.LINE_X: 1.0,
            },
        )
        line_state = {
            "obj": line,
            "l": 10.0,
            "r": 1.2,
            "x": 1.0,
            "typ_id": line_type,
            "temporary_line_types": [],
        }

        rows = apply_random_line_parameter_scenario(
            {"Line_1": line_state},
            "rand_0001",
            123,
            0.8,
            1.2,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rx_ratio_randomization_applied"], 1)
        self.assertLessEqual(rows[0]["randomized_rx_ratio"], 0.60)
        self.assertIsNot(line.GetAttribute(PFAttr.LINE_TYPE), line_type)
        self.assertEqual(len(library.copies), 1)

        temporary_type = library.copies[0]

        restore_grid_state(
            {"Line_1": line_state},
            {},
            {},
            {},
        )

        self.assertIs(line.GetAttribute(PFAttr.LINE_TYPE), line_type)
        self.assertEqual(line.GetAttribute(PFAttr.LINE_LENGTH), 10.0)
        self.assertTrue(temporary_type.deleted)
        self.assertEqual(line_state["temporary_line_types"], [])

    def test_typ_tow_keeps_length_only_randomization(self):
        library = FakeTypeLibrary()
        line_type = FakePowerFactoryObject(
            "TowerType",
            "TypTow",
            parent=library,
            attrs={
                "rline": 0.06,
                "xline": 0.29,
            },
        )
        line = FakePowerFactoryObject(
            "Line_1",
            "ElmLne",
            attrs={
                PFAttr.LINE_TYPE: line_type,
                PFAttr.LINE_LENGTH: 10.0,
                PFAttr.LINE_R: 0.6,
                PFAttr.LINE_X: 2.9,
            },
        )
        line_state = {
            "obj": line,
            "l": 10.0,
            "r": 0.6,
            "x": 2.9,
            "typ_id": line_type,
            "temporary_line_types": [],
        }

        rows = apply_random_line_parameter_scenario(
            {"Line_1": line_state},
            "rand_0001",
            123,
            0.8,
            1.2,
        )

        self.assertEqual(rows[0]["rx_ratio_randomization_applied"], 0)
        self.assertEqual(
            rows[0]["rx_ratio_randomization_skipped_reason"],
            "TypTow",
        )
        self.assertIs(line.GetAttribute(PFAttr.LINE_TYPE), line_type)
        self.assertEqual(library.copies, [])


if __name__ == "__main__":
    unittest.main()

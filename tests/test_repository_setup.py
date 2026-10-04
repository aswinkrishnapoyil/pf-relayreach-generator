import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pf_adaptive_distance_dataset.core.config import Config, _load_local_powerfactory_settings
from pf_adaptive_distance_dataset.pf_api import slave_cases


class RepositorySetupTest(unittest.TestCase):
    def test_local_connection_overrides_are_validated_before_application(self):
        with tempfile.TemporaryDirectory() as s_temp_dir:
            p_settings = Path(s_temp_dir) / "powerfactory.local.json"
            with patch.object(Config, "PROJECT_NAME", "original"):
                _load_local_powerfactory_settings(p_settings)  # Missing file is optional.
                self.assertEqual(Config.PROJECT_NAME, "original")
                for d_invalid in (
                    {"PROJECT_NAME": "new", "ZONE2_TARGET_TO_BASE_RATIO_LIMIT": 3},
                    {"PROJECT_NAME": "new", "GRID_NAME": ""},
                    [],
                ):
                    p_settings.write_text(json.dumps(d_invalid), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        _load_local_powerfactory_settings(p_settings)
                    self.assertEqual(Config.PROJECT_NAME, "original")
                p_settings.write_text(json.dumps({"PROJECT_NAME": "new"}), encoding="utf-8")
                _load_local_powerfactory_settings(p_settings)
                self.assertEqual(Config.PROJECT_NAME, "new")

    def test_cleanup_only_selects_generator_cases_and_does_not_empty_recycle_bin(self):
        o_project = Mock()
        o_app = Mock()
        o_generated_case = Mock(loc_name=f"{Config.SLAVE_STUDY_CASE_PREFIX}_ss_0001")
        o_generated_scenario = Mock(loc_name=f"{Config.SLAVE_OPERATION_SCENARIO_PREFIX}_ss_0001")
        o_unrelated = Mock(loc_name="My_Slave_Study")
        o_similar = Mock(loc_name=f"{Config.SLAVE_STUDY_CASE_PREFIX}_ss_0001_notes")
        o_project.GetContents.side_effect = [
            [o_generated_case, o_unrelated, o_similar], [o_generated_scenario],
        ]
        with patch.object(slave_cases, "get_safe_name", side_effect=lambda o: o.loc_name), \
                patch.object(slave_cases, "delete_powerfactory_object") as o_delete:
            slave_cases.delete_existing_slave_cases(o_project, o_app)
        self.assertEqual([o_call.args[0] for o_call in o_delete.call_args_list],
                         [o_generated_case, o_generated_scenario])
        o_app.ClearRecycleBin.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pf_adaptive_distance_dataset.pf_api.pf_session import PowerFactorySession


class FakeApplication:
    def __init__(self):
        self.show_calls = 0
        self.hide_calls = 0

    def Show(self):
        self.show_calls += 1

    def Hide(self):
        self.hide_calls += 1

    def ActivateProject(self, _project_name):
        return 0

    def GetActiveProject(self):
        return SimpleNamespace(GetContents=lambda *_args: [object()])

    def ResetCalculation(self):
        pass


class PowerFactorySessionTest(unittest.TestCase):
    def test_gui_is_only_shown_in_debug_mode(self):
        for debug, expected_show_calls, expected_hide_calls in (
            (False, 0, 1),
            (True, 1, 0),
        ):
            with self.subTest(debug=debug):
                app = FakeApplication()
                module = SimpleNamespace(GetApplicationExt=lambda: app)

                with patch.dict(sys.modules, {"powerfactory": module}):
                    with PowerFactorySession("project", "grid", debug=debug):
                        pass

                self.assertEqual(app.show_calls, expected_show_calls)
                self.assertEqual(app.hide_calls, expected_hide_calls)


if __name__ == "__main__":
    unittest.main()

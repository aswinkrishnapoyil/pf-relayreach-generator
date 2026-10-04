import unittest

from pf_adaptive_distance_dataset.domain.zone_reach import (
    calculate_distance_zone_reaches_for_corridor,
)


class TerminalZoneReachTest(unittest.TestCase):

    def test_terminal_line_uses_zone1_85_zone2_120_and_no_zone3(self):
        corridor = {
            "total_r_ohm": 10.0,
            "total_x_ohm": 20.0,
            "total_length_km": 5.0,
            "subsequent_busbar": None,
        }

        result = calculate_distance_zone_reaches_for_corridor(
            corridor,
            {},
            [],
            l_valid_branches=[],
        )

        self.assertEqual(result["zone1_r_reach_ohm"], 8.5)
        self.assertEqual(result["zone1_x_reach_ohm"], 17.0)

        self.assertEqual(result["zone2_r_reach_ohm"], 12.0)
        self.assertEqual(result["zone2_x_reach_ohm"], 24.0)

        self.assertEqual(result["zone3_applicable"], 0)
        self.assertIsNone(result["zone3_r_reach_ohm"])
        self.assertIsNone(result["zone3_x_reach_ohm"])


if __name__ == "__main__":
    unittest.main()

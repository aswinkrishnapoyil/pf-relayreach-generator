import unittest

from pf_adaptive_distance_dataset.domain.zone_reach import (
    calculate_distance_zone_reaches_for_corridor,
    select_zone3_longest_valid_downstream_branch,
)


class FakeObject:
    def __init__(self, name):
        self.loc_name = name

    def GetFullName(self):
        return f"Grid\\{self.loc_name}"


def make_branch(name, length_km, r_ohm, x_ohm, subsequent_busbar):
    return {
        "Branch Lines": [FakeObject(f"Line_{name}")],
        "Branch Terminals": [subsequent_busbar, FakeObject(f"Terminal_{name}")],
        "Branch Length": length_km,
        "Branch Resistance": r_ohm,
        "Branch Reactance": x_ohm,
    }


class ZoneBranchSelectionTest(unittest.TestCase):
    """Zone 2 uses the smallest-reactance branch, Zone 3 the largest-reactance branch."""

    def setUp(self):
        self.subsequent_busbar = FakeObject("Terminal_Subsequent")
        self.corridor = {
            "total_r_ohm": 1.0,
            "total_x_ohm": 4.0,
            "total_length_km": 10.0,
            "subsequent_busbar": self.subsequent_busbar,
        }
        # Longest in km but low reactance per km, e.g. a different line type.
        self.long_low_x = make_branch("A", 30.0, 3.0, 6.0, self.subsequent_busbar)
        # Largest reactance although shorter in km.
        self.largest_x = make_branch("B", 20.0, 2.0, 8.0, self.subsequent_busbar)
        # Smallest reactance.
        self.smallest_x = make_branch("C", 10.0, 1.0, 3.0, self.subsequent_busbar)
        self.branches = [self.long_low_x, self.largest_x, self.smallest_x]

    def test_zone3_selects_largest_reactance_not_largest_length(self):
        selected = select_zone3_longest_valid_downstream_branch(
            self.corridor, [], l_valid_branches=self.branches
        )
        self.assertIs(selected, self.largest_x)

    def test_zone2_and_zone3_reaches_use_consistent_reactance_rule(self):
        result = calculate_distance_zone_reaches_for_corridor(
            self.corridor, {}, [], l_valid_branches=self.branches
        )

        self.assertEqual(result["zone2_downstream_branch_id"], "Line_C")
        self.assertAlmostEqual(result["zone2_r_reach_ohm"], 0.85 * (1.0 + 0.85 * 1.0), places=3)
        self.assertAlmostEqual(result["zone2_x_reach_ohm"], 0.85 * (4.0 + 0.85 * 3.0), places=3)

        self.assertEqual(result["zone3_applicable"], 1)
        self.assertEqual(result["zone3_downstream_branch_id"], "Line_B")
        self.assertAlmostEqual(result["zone3_downstream_branch_x_ohm"], 8.0, places=3)
        self.assertAlmostEqual(result["zone3_r_reach_ohm"], 1.0 + 1.2 * 2.0, places=3)
        self.assertAlmostEqual(result["zone3_x_reach_ohm"], 4.0 + 1.2 * 8.0, places=3)

    def test_zone3_reactance_tie_is_broken_by_length(self):
        shorter = make_branch("D", 12.0, 1.5, 8.0, self.subsequent_busbar)
        longer = make_branch("E", 18.0, 1.5, 8.0, self.subsequent_busbar)

        selected = select_zone3_longest_valid_downstream_branch(
            self.corridor, [], l_valid_branches=[shorter, longer]
        )
        self.assertIs(selected, longer)

    def test_zone3_without_valid_branch_returns_empty_summary(self):
        selected = select_zone3_longest_valid_downstream_branch(
            self.corridor, [], l_valid_branches=[]
        )
        self.assertEqual(selected["Branch Lines"], [])
        self.assertEqual(selected["Branch Reactance"], 0.0)


if __name__ == "__main__":
    unittest.main()

import copy
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from pf_adaptive_distance_dataset.core.dataset_schema import target_reach_columns
from pf_adaptive_distance_dataset.graph.graph_arrays import (
    BUS_TYPE_CODE_BY_LABEL,
    build_bus_index,
    convert_scenario_to_graph_row,
    extract_directed_edge_features,
    stamp_switched_ybus,
)
from pf_adaptive_distance_dataset.pipeline.dataset_generator import (
    DatasetIntegrityError,
    _validate_graph_dg_capacity_consistency,
)
from pf_adaptive_distance_dataset.core.models import DatasetMetadata, ExportPayload
from pf_adaptive_distance_dataset.exports.export import (
    FLAT_PROVENANCE_COLUMNS, FLAT_EXPORT_COLUMNS, _write_flat_rows_csv, stream_export_and_audit,
)
from pf_adaptive_distance_dataset.exports.validation import validate_case


def corridor_row(
    relay_id,
    source,
    target,
    terminals,
    sections,
    lengths,
    resistances,
    reactances,
    targets=None,
):
    row = {
        "relay_id": relay_id,
        "relay_node_id": source,
        "subsequent_node_id": target,
        "protected_corridor_id": f"{source} -> {target}",
        "protected_corridor_terminal_path_json": json.dumps(terminals),
        "protected_corridor_line_section_id_json": json.dumps(sections),
        "protected_corridor_line_section_length_km_json": json.dumps(lengths),
        "protected_corridor_line_section_r_ohm_json": json.dumps(resistances),
        "protected_corridor_line_section_x_ohm_json": json.dumps(reactances),
        "protected_corridor_length_km": sum(lengths),
        "protected_corridor_r_ohm": sum(resistances),
        "protected_corridor_x_ohm": sum(reactances),
        "line_is_in_service": 1,
        "corridor_hop_count": len(sections),
        "protected_corridor_is_parallel": 0,
        "protected_corridor_parallel_count": 1,
        "protected_corridor_parallel_id": "",
    }
    row.update(zip(target_reach_columns, targets or [1, 2, 3, 4, 5, 6]))
    return row


class GraphContractTest(unittest.TestCase):
    def test_csv_retains_provenance_features_and_all_target_columns(self):
        d_row = {s_column: 1 for s_column in FLAT_PROVENANCE_COLUMNS}
        d_row.update(corridor_row("Relay_A", "A", "B", ["A", "B"], ["L_AB"], [1], [0.1], [0.2]))
        with tempfile.TemporaryDirectory(prefix="pf_flat_contract_") as s_temp_dir:
            p_csv = Path(s_temp_dir) / "flat.csv"
            _write_flat_rows_csv(p_csv, [d_row])
            with p_csv.open(newline="", encoding="utf-8") as o_file:
                o_reader = csv.DictReader(o_file)
                d_exported = next(o_reader)
                self.assertEqual(o_reader.fieldnames, FLAT_EXPORT_COLUMNS)
            self.assertEqual(d_exported["scenario_uid"], "1")
            self.assertEqual(float(d_exported["protected_corridor_r_ohm"]), 0.1)
            self.assertEqual(float(d_exported["target_zone2_r_reach_ohm"]), 3.0)

    def test_flat_audit_and_graph_agree_without_losing_terminal_or_physical_edges(self):
        d_valid = corridor_row("Relay_A", "A", "B", ["A", "B"], ["L_AB"], [1], [0.1], [0.2])
        d_terminal = copy.deepcopy(d_valid)
        d_terminal.update(zone3_applicable=0, target_zone3_r_reach_ohm=None, target_zone3_x_reach_ohm=None)
        d_invalid = copy.deepcopy(d_valid)
        d_invalid["target_zone2_r_reach_ohm"] = -1.0
        l_rows = [d_valid, d_terminal, d_invalid]
        d_graph = convert_scenario_to_graph_row(l_rows, {"scenario_uid": "test"}, [1])
        self.assertEqual(d_graph["directed_edge_target_valid"], [1, 1, 0])
        self.assertEqual(d_graph["directed_edge_target_valid_count"], 2)
        self.assertEqual(d_graph["physical_edge_count"], 1)
        self.assertEqual(d_graph["directed_edge_count"], 3)
        self.assertTrue(math.isnan(d_graph["target_zone3_r_reach_ohm"][1]))
        l_payloads = []
        for i_case, d_row in enumerate(l_rows, 1):
            d_flat = {s_column: 0 for s_column in FLAT_PROVENANCE_COLUMNS}
            d_flat.update(d_row, case_id=i_case, scenario_uid="test")
            l_payloads.append(ExportPayload(kind="flat_row", data=d_flat))
        l_payloads.append(ExportPayload(kind="graph_row", data=d_graph))
        o_metadata = DatasetMetadata("test", "test", "test", "test", 42, False, False, (1, 1), (1, 1), "test")
        with tempfile.TemporaryDirectory(prefix="pf_validity_contract_") as s_temp_dir:
            p_output = Path(s_temp_dir)
            o_stats = stream_export_and_audit(iter(l_payloads), o_metadata, p_output)
            self.assertEqual((o_stats.total_cases_valid, o_stats.total_cases_invalid), (2, 1))
            df_audit = pd.read_excel(next(p_output.glob("*.xlsx")))
            self.assertEqual(df_audit["is_valid"].tolist(), [1, 1, 0])
            self.assertEqual(df_audit["scenario_uid"].tolist(), ["test"] * 3)
            self.assertIn("negative_zone_reach", df_audit.iloc[2]["invalid_reason"])
            df_graph = pd.read_parquet(next(p_output.glob("*.parquet")))
            self.assertEqual(list(df_graph.iloc[0]["directed_edge_target_valid"]), [1, 1, 0])

    def test_missing_nonfinite_and_inapplicable_targets_are_not_valid_labels(self):
        d_row = corridor_row("Relay_A", "A", "B", ["A", "B"], ["L_AB"], [1], [0.1], [0.2])
        for value in (None, math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                d_bad = dict(d_row, target_zone2_r_reach_ohm=value)
                self.assertFalse(validate_case(d_bad)[0])
                d_graph = convert_scenario_to_graph_row([d_bad], {}, [1])
                self.assertEqual(d_graph["directed_edge_target_valid"], [0])
        self.assertFalse(validate_case(dict(d_row, zone3_applicable=0))[0])

    def test_nonfinite_physical_input_rejects_graph_instead_of_masking_labels(self):
        d_row = corridor_row("Relay_A", "A", "B", ["A", "B"], ["L_AB"], [1], [0.1], [0.2])
        d_row["protected_corridor_line_section_r_ohm_json"] = "[NaN]"
        with self.assertRaisesRegex(ValueError, "Invalid physical R"):
            convert_scenario_to_graph_row([d_row], {}, [1])

    def test_reversed_corridor_keeps_query_direction_and_one_physical_path(self):
        forward = corridor_row(
            "Relay_A", "A", "B", ["A", "J", "B"], ["L_AJ", "L_JB"],
            [1.0, 2.0], [0.1, 0.2], [0.3, 0.4],
        )
        reverse = corridor_row(
            "Relay_B", "B", "A", ["B", "J", "A"], ["L_JB", "L_AJ"],
            [2.0, 1.0], [0.2, 0.1], [0.4, 0.3],
        )

        bus_ids, bus_index, *_ = build_bus_index([forward, reverse])
        directed, physical, metadata = extract_directed_edge_features(
            [forward, reverse], bus_index
        )

        self.assertEqual(bus_ids, ["A", "B", "J"])
        self.assertEqual(directed["directed_edge_from"], ["A", "B"])
        self.assertEqual(directed["directed_edge_to"], ["B", "A"])
        self.assertEqual(metadata["directed_edge_count"], 2)
        self.assertEqual(len(physical), 2)

    def test_junction_and_section_order_stay_aligned(self):
        row = corridor_row(
            "Relay_A", "A", "B", ["A", "J", "B"], ["L_AJ", "L_JB"],
            [1.0, 2.0], [0.1, 0.2], [0.3, 0.4],
        )
        bus_ids, bus_index, *_ = build_bus_index([row])
        _, physical, _ = extract_directed_edge_features([row], bus_index)
        endpoints = {
            edge.representative_id: {bus_ids[edge.a], bus_ids[edge.b]}
            for edge in physical.values()
        }

        self.assertEqual(endpoints, {"L_AJ": {"A", "J"}, "L_JB": {"J", "B"}})

    def test_section_values_reconstruct_the_corridor_or_fail_closed(self):
        row = corridor_row(
            "Relay_A", "A", "B", ["A", "J", "B"], ["L_AJ", "L_JB"],
            [1.0, 2.0], [0.1, 0.2], [0.3, 0.4],
        )
        _, bus_index, *_ = build_bus_index([row])
        _, physical, _ = extract_directed_edge_features([row], bus_index)

        self.assertAlmostEqual(sum(edge.length_km for edge in physical.values()), 3.0)
        self.assertAlmostEqual(sum(edge.r_ohm for edge in physical.values()), 0.3)
        self.assertAlmostEqual(sum(edge.x_ohm for edge in physical.values()), 0.7)

        broken = copy.deepcopy(row)
        broken["protected_corridor_r_ohm"] = 0.4
        with self.assertRaisesRegex(ValueError, "R reconstruction mismatch"):
            extract_directed_edge_features([broken], bus_index)

    def test_parallel_lines_are_distinct_and_ybus_admittances_add(self):
        first = corridor_row(
            "Relay_1", "A", "B", ["A", "B"], ["Line_1"],
            [1.0], [1.0], [1.0],
        )
        second = corridor_row(
            "Relay_2", "B", "A", ["B", "A"], ["Line_2"],
            [1.0], [2.0], [2.0],
        )
        _, bus_index, *_ = build_bus_index([first, second])
        _, physical, _ = extract_directed_edge_features([first, second], bus_index)
        ybus = stamp_switched_ybus(physical, 2)

        self.assertEqual(ybus["physical_edge_count"], 2)
        self.assertEqual(ybus["Lines_connected"], [1])
        self.assertEqual(ybus["Lines_connected_count"], [2])
        self.assertAlmostEqual(ybus["Y_Lines_real"][0], 0.75)
        self.assertAlmostEqual(ybus["Y_Lines_imag"][0], -0.75)
        self.assertEqual(ybus["Y_matrix_real_2d"], [[0.75, -0.75], [-0.75, 0.75]])
        self.assertEqual(ybus["Y_matrix_imag_2d"], [[-0.75, 0.75], [0.75, -0.75]])

    def test_bus_dg_mapping_matches_flat_context_and_rejects_mismatch(self):
        row = corridor_row(
            "Relay_A", "A", "B", ["A", "B"], ["Line_AB"],
            [1.0], [0.1], [0.2],
        )
        row.update({
            "relay_busbar_distributed_generation_count": 0,
            "relay_busbar_distributed_generation_capacity_mva": 0.0,
            "subsequent_busbar_distributed_generation_count": 1,
            "subsequent_busbar_distributed_generation_capacity_mva": 50.0,
        })
        bus_attributes = {
            "A": {"uknom_kv": 110.0, "dg_capacity_mva": 0.0},
            "B": {
                "uknom_kv": 110.0,
                "has_pv_dg": 1,
                "dg_capacity_mva": 50.0,
            },
        }
        graph = convert_scenario_to_graph_row([row], {}, [], bus_attributes)

        self.assertEqual(graph["bus_id"], ["A", "B"])
        self.assertEqual(
            graph["bus_typ"],
            [
                BUS_TYPE_CODE_BY_LABEL["plain_or_tie_bus"],
                BUS_TYPE_CODE_BY_LABEL["pv_or_inverter_dg_bus"],
            ],
        )
        self.assertEqual(graph["bus_has_dg"], [0, 1])
        self.assertEqual(graph["bus_dg_capacity_mva"], [0.0, 50.0])
        _validate_graph_dg_capacity_consistency("scenario-1", [row], graph)

        broken = copy.deepcopy(graph)
        broken["bus_dg_capacity_mva"][1] = 49.0
        with self.assertRaisesRegex(DatasetIntegrityError, "DG capacity mismatch"):
            _validate_graph_dg_capacity_consistency("scenario-1", [row], broken)

    def test_bus_type_codes_are_sparse_categorical_codes(self):
        self.assertEqual(len(set(BUS_TYPE_CODE_BY_LABEL.values())), 8)
        self.assertTrue(
            set(BUS_TYPE_CODE_BY_LABEL.values()).isdisjoint(set(range(8)))
        )

    def test_targets_remain_aligned_with_their_directed_queries(self):
        first_targets = [11, 12, 13, 14, 15, 16]
        second_targets = [21, 22, 23, 24, 25, 26]
        rows = [
            corridor_row(
                "Relay_B", "B", "C", ["B", "C"], ["Line_BC"],
                [1.0], [0.1], [0.2], first_targets,
            ),
            corridor_row(
                "Relay_A", "A", "B", ["A", "B"], ["Line_AB"],
                [1.0], [0.1], [0.2], second_targets,
            ),
        ]
        graph = convert_scenario_to_graph_row(rows, {}, [1, 0])

        self.assertEqual(graph["directed_edge_relay_id"], ["Relay_B", "Relay_A"])
        self.assertEqual(graph["directed_edge_from"], ["B", "A"])
        self.assertEqual(graph["directed_edge_to"], ["C", "B"])
        for index, column in enumerate(target_reach_columns):
            self.assertEqual(graph[column], [first_targets[index], second_targets[index]])

    def test_malformed_structural_arrays_are_rejected(self):
        row = corridor_row(
            "Relay_A", "A", "B", ["A", "J", "B"], ["L_AJ", "L_JB"],
            [1.0, 2.0], [0.1, 0.2], [0.3, 0.4],
        )
        row["protected_corridor_terminal_path_json"] = json.dumps(["A", "B"])
        _, bus_index, *_ = build_bus_index([row])

        with self.assertRaisesRegex(ValueError, "Invalid physical corridor path"):
            extract_directed_edge_features([row], bus_index)


if __name__ == "__main__":
    unittest.main()

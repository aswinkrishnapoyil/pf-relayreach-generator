# dataset_schema.py
from __future__ import annotations

l_case_feature_columns = [
    "case_id", "scenario_case_id", "relay_id", "relay_node_id", "protected_corridor_id", "subsequent_node_id",
    "protected_corridor_terminal_path_json", "protected_corridor_line_section_id_json",
    "protected_corridor_line_section_length_km_json", "protected_corridor_line_section_r_ohm_json",
    "protected_corridor_line_section_x_ohm_json",
    "line_is_in_service", "corridor_hop_count", "protected_corridor_length_km",
    "protected_corridor_r_ohm", "protected_corridor_x_ohm", "protected_corridor_is_parallel",
    "protected_corridor_parallel_count", "protected_corridor_parallel_id",
    "next_node_busbar_count", "next_node_junction_node_count", "next_node_distributed_generation_count",
    "shortest_downstream_branch_id", "shortest_downstream_branch_remote_node_id",
    "shortest_downstream_branch_hop_count", "shortest_downstream_branch_length_km",
    "shortest_downstream_branch_r_ohm", "shortest_downstream_branch_x_ohm",
    "shortest_downstream_branch_has_parallel", "shortest_downstream_branch_parallel_count",
    "shortest_downstream_branch_parallel_id", "shortest_downstream_branch_junction_node_count",
    "shortest_downstream_branch_junction_node_id", "shortest_downstream_branch_distributed_generation_count",
    "shortest_downstream_branch_distributed_generation_id", "zone2_branch_selection_method",
    "zone2_selected_branch_id", "zone2_reach_calculation_method", "zone2_impedance_basis",
    "parallel_group_count_forward", "parallel_group_id", "zone2_parallel_branch_for_complex_length_km",
    "zone2_parallel_branch_for_complex_r_ohm", "zone2_parallel_branch_for_complex_x_ohm", "zone3_applicable",
    "zone3_branch_selection_method", "zone3_selected_branch_id", "longest_downstream_branch_hop_count",
    "longest_downstream_branch_length_km", "longest_downstream_branch_r_ohm", "longest_downstream_branch_x_ohm",
    "relay_busbar_distributed_generation_count", "relay_busbar_distributed_generation_id",
    "relay_busbar_distributed_generation_capacity_mva", "protected_corridor_distributed_generation_count",
    "protected_corridor_distributed_generation_id", "protected_corridor_distributed_generation_capacity_mva",
    "subsequent_busbar_distributed_generation_count", "subsequent_busbar_distributed_generation_id",
    "subsequent_busbar_distributed_generation_capacity_mva", "downstream_branch_distributed_generation_count",
    "downstream_branch_distributed_generation_id", "downstream_branch_distributed_generation_capacity_mva",
    "remote_busbar_distributed_generation_count", "remote_busbar_distributed_generation_id",
    "remote_busbar_distributed_generation_capacity_mva", "zone1_turbines_candidate_count",
    "zone1_turbines_candidate_id", "zone1_turbines_considered_count", "zone1_turbines_considered_id",
    "zone1_turbines_skipped_count", "zone1_turbines_skipped_id", "zone1_turbines_total_capacity_mva",
    "zone1_total_ikss_contribution_ratio", "zone1_max_single_ikss_contribution_ratio",
    "zone2_turbines_candidate_count", "zone2_turbines_candidate_id", "zone2_turbines_considered_count",
    "zone2_turbines_considered_id", "zone2_turbines_skipped_count", "zone2_turbines_skipped_id",
    "zone2_turbines_total_capacity_mva", "zone2_total_ikss_contribution_ratio",
    "zone2_max_single_ikss_contribution_ratio", "base_zone1_r_reach_ohm", "base_zone1_x_reach_ohm",
    "base_zone2_r_reach_ohm", "base_zone2_x_reach_ohm", "base_zone3_r_reach_ohm", "base_zone3_x_reach_ohm",
    "zone1_infeed_correction_r_ohm", "zone1_infeed_correction_x_ohm", "zone2_infeed_correction_r_ohm",
    "zone2_infeed_correction_x_ohm", "target_zone1_r_reach_ohm", "target_zone1_x_reach_ohm",
    "target_zone2_r_reach_ohm", "target_zone2_x_reach_ohm", "target_zone3_r_reach_ohm", "target_zone3_x_reach_ohm"
]

base_reach_columns = [
    "base_zone1_r_reach_ohm", "base_zone1_x_reach_ohm",
    "base_zone2_r_reach_ohm", "base_zone2_x_reach_ohm",
    "base_zone3_r_reach_ohm", "base_zone3_x_reach_ohm",
]

reach_infeed_correction_columns = [
    "zone1_infeed_correction_r_ohm", "zone1_infeed_correction_x_ohm",
    "zone2_infeed_correction_r_ohm", "zone2_infeed_correction_x_ohm",
]

target_reach_columns = [
    "target_zone1_r_reach_ohm", "target_zone1_x_reach_ohm",
    "target_zone2_r_reach_ohm", "target_zone2_x_reach_ohm",
    "target_zone3_r_reach_ohm", "target_zone3_x_reach_ohm",
]

downstream_numeric_columns = [
    "next_node_busbar_count", "next_node_junction_node_count",
    "next_node_distributed_generation_count", "shortest_downstream_branch_hop_count",
    "shortest_downstream_branch_length_km", "shortest_downstream_branch_r_ohm",
    "shortest_downstream_branch_x_ohm", "shortest_downstream_branch_has_parallel",
    "shortest_downstream_branch_parallel_count",
    "shortest_downstream_branch_junction_node_count",
    "shortest_downstream_branch_distributed_generation_count",
    "longest_downstream_branch_hop_count", "longest_downstream_branch_length_km",
    "longest_downstream_branch_r_ohm", "longest_downstream_branch_x_ohm",
]

downstream_string_columns = [
    "shortest_downstream_branch_id", "shortest_downstream_branch_remote_node_id",
    "shortest_downstream_branch_parallel_id", "shortest_downstream_branch_junction_node_id",
    "shortest_downstream_branch_distributed_generation_id", "zone3_selected_branch_id",
]

zone_method_numeric_columns = [
    "zone3_applicable", "parallel_group_count_forward", "zone2_parallel_branch_for_complex_length_km",
    "zone2_parallel_branch_for_complex_r_ohm", "zone2_parallel_branch_for_complex_x_ohm",
]

zone_method_string_columns = [
    "zone2_branch_selection_method", "zone2_selected_branch_id",
    "zone2_reach_calculation_method", "zone2_impedance_basis",
    "parallel_group_id", "zone3_branch_selection_method",
]

dg_numeric_columns = [
    "relay_busbar_distributed_generation_count",
    "relay_busbar_distributed_generation_capacity_mva",
    "protected_corridor_distributed_generation_count",
    "protected_corridor_distributed_generation_capacity_mva",
    "subsequent_busbar_distributed_generation_count",
    "subsequent_busbar_distributed_generation_capacity_mva",
    "downstream_branch_distributed_generation_count",
    "downstream_branch_distributed_generation_capacity_mva",
    "remote_busbar_distributed_generation_count",
    "remote_busbar_distributed_generation_capacity_mva",
]

dg_string_columns = [
    "relay_busbar_distributed_generation_id",
    "protected_corridor_distributed_generation_id",
    "subsequent_busbar_distributed_generation_id",
    "downstream_branch_distributed_generation_id",
    "remote_busbar_distributed_generation_id",
]

turbine_numeric_columns = [
    "zone1_turbines_candidate_count", "zone1_turbines_considered_count",
    "zone1_turbines_skipped_count", "zone1_turbines_total_capacity_mva",
    "zone2_turbines_candidate_count", "zone2_turbines_considered_count",
    "zone2_turbines_skipped_count", "zone2_turbines_total_capacity_mva",
]

ikss_ratio_columns = [
    "zone1_total_ikss_contribution_ratio", "zone1_max_single_ikss_contribution_ratio",
    "zone2_total_ikss_contribution_ratio", "zone2_max_single_ikss_contribution_ratio",
]

turbine_string_columns = [
    "zone1_turbines_candidate_id", "zone1_turbines_considered_id",
    "zone1_turbines_skipped_id", "zone2_turbines_candidate_id",
    "zone2_turbines_considered_id", "zone2_turbines_skipped_id",
]

directed_numeric_context_columns = (downstream_numeric_columns + zone_method_numeric_columns + dg_numeric_columns +
                                    turbine_numeric_columns + ikss_ratio_columns)

directed_string_context_columns = (downstream_string_columns + zone_method_string_columns + dg_string_columns +
                                   turbine_string_columns)

ybus_2d_columns = ["Y_matrix_real_2d", "Y_matrix_imag_2d"]

graph_edge_join_columns = [
    "directed_edge_scenario_case_ids",
]

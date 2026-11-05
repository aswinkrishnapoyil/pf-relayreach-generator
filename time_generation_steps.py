"""Time the steps of the reference generation per network state (case).

  python time_generation_steps.py --generator C:\\path\\to\\pf-relayreach-generator-v3.4.0

Runs a small generation with the unchanged generator (v3.4.0) in one
PowerFactory session and measures, for every case, how long its steps take.
The generator files are not modified: the timers are attached in memory, and
the run writes only to a new folder Results/step_timing_<time>/ inside the
generator folder (ignored by verify_release.py). Default: switch states 1-3,
the base case and 4 randomized cases each (15 cases, about 15-20 minutes).

Steps (inclusive wall-clock seconds per case):
  network_extraction       reading lines, busbars, switches, DERs from PowerFactory
  corridor_detection       tracing the directed protected corridors
  corridor_rows            all relay rows of the case (contains the next three)
  reference_base_reaches   Zone 1-3 base reaches from the line impedances
  reference_infeed         infeed correction: DER switching, short circuits, currents
  short_circuits           PowerFactory short-circuit runs (part of reference_infeed)
  bus_attributes           bus data for the graph export
  graph_conversion         building the graph arrays of the case
  setup_switch_state, setup_randomization, cleanup
                           creating the case (not needed for a given network state)
Derived:
  input_preparation = network_extraction + corridor_detection + bus_attributes
                      + graph_conversion + (corridor_rows - extraction - detection
                      - reference_base_reaches - reference_infeed)
      everything a model-based tool needs to build the MLP and GNN inputs;
      steps shared with the setting rules (e.g. selecting the downstream
      branch) are counted here, so the estimate is on the high side
  reference_specific = reference_base_reaches + reference_infeed

Output: step_times.csv (one row per case), step_summary.json (medians and
means over the cases; the first case of every switch state also contains the
creation of the PowerFactory study case and is reported separately).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

STEPS = ("network_extraction", "corridor_detection", "corridor_rows", "reference_base_reaches",
         "reference_infeed", "short_circuits", "bus_attributes", "graph_conversion",
         "setup_switch_state", "setup_randomization", "scenario_processing", "cleanup")

d_total = defaultdict(float)
d_count = defaultdict(int)
l_rows = []
d_state = {"last_end": None, "failed": False, "scenario": None, "switch_state_seen": set()}


def timed(f, s_label):
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return f(*args, **kwargs)
        finally:
            d_total[s_label] += time.perf_counter() - t0
            d_count[s_label] += 1
    return wrapper


def attach_timers():
    from pf_adaptive_distance_dataset.domain import infeed
    from pf_adaptive_distance_dataset.pipeline import case_features, dataset_generator

    for o_mod, s_attr, s_label in (
            (case_features, "extract_network_once", "network_extraction"),
            (case_features, "detect_protected_corridors_once", "corridor_detection"),
            (case_features, "calculate_distance_zone_reaches_for_corridor", "reference_base_reaches"),
            (case_features, "calculate_zone_infeed_summary_for_turbines", "reference_infeed"),
            (infeed, "execute_short_circuit_at_line_fault", "short_circuits"),
            (dataset_generator, "get_corridor_scenario_rows", "corridor_rows"),
            (dataset_generator, "capture_bus_attributes", "bus_attributes"),
            (dataset_generator, "convert_scenario_to_graph_row", "graph_conversion"),
            (dataset_generator, "_restore_and_apply_switch_state", "setup_switch_state"),
            (dataset_generator, "_apply_randomization_for_scenario", "setup_randomization"),
    ):
        setattr(o_mod, s_attr, timed(getattr(o_mod, s_attr), s_label))

    f_process = timed(dataset_generator._process_single_scenario, "scenario_processing")

    def process(*args, **kwargs):
        d_state["scenario"] = kwargs.get("scenario_uid", args[10] if len(args) > 10 else "")
        d_state["switch_state"] = kwargs.get("global_idx", args[6] if len(args) > 6 else None)
        d_state["failed"] = False
        try:
            return f_process(*args, **kwargs)
        except Exception:
            d_state["failed"] = True
            raise

    f_cleanup = timed(dataset_generator._cleanup_after_scenario, "cleanup")

    def cleanup(*args, **kwargs):
        try:
            return f_cleanup(*args, **kwargs)
        finally:
            record_case()

    dataset_generator._process_single_scenario = process
    dataset_generator._cleanup_after_scenario = cleanup


def record_case():
    t_now = time.perf_counter()
    d = {s: d_total[s] for s in STEPS}
    d_prev = l_rows[-1]["_cumulative"] if l_rows else {s: 0.0 for s in STEPS}
    i_sc_prev = l_rows[-1]["_short_circuit_count"] if l_rows else 0
    d_row = {s: d[s] - d_prev[s] for s in STEPS}
    d_row["corridor_features"] = (d_row["corridor_rows"] - d_row["network_extraction"] - d_row["corridor_detection"]
                                  - d_row["reference_base_reaches"] - d_row["reference_infeed"])
    d_row["input_preparation"] = (d_row["network_extraction"] + d_row["corridor_detection"] + d_row["corridor_features"]
                                  + d_row["bus_attributes"] + d_row["graph_conversion"])
    d_row["reference_specific"] = d_row["reference_base_reaches"] + d_row["reference_infeed"]
    d_row["wall_since_previous_case"] = t_now - d_state["last_end"]
    i_switch = d_state.get("switch_state")
    d_row.update({
        "case": len(l_rows) + 1, "scenario_uid": d_state["scenario"], "switch_state": i_switch,
        "first_case_of_switch_state": i_switch not in d_state["switch_state_seen"],
        "failed_or_omitted": d_state["failed"],
        "short_circuit_count": d_count["short_circuits"] - i_sc_prev,
        "_cumulative": d, "_short_circuit_count": d_count["short_circuits"],
    })
    d_state["switch_state_seen"].add(i_switch)
    d_state["last_end"] = t_now
    l_rows.append(d_row)
    print(f"[timing] case {d_row['case']} {d_row['scenario_uid']}: total {d_row['wall_since_previous_case']:.1f} s, "
          f"input preparation {d_row['input_preparation']:.2f} s, infeed {d_row['reference_infeed']:.1f} s "
          f"({d_row['short_circuit_count']} short circuits)", flush=True)


def write_results(p_out: Path, d_info: dict):
    import csv

    l_cols = ["case", "scenario_uid", "switch_state", "first_case_of_switch_state", "failed_or_omitted",
              "short_circuit_count", "wall_since_previous_case", "input_preparation", "reference_specific",
              "corridor_features", *STEPS]
    with (p_out / "step_times.csv").open("w", newline="", encoding="utf-8") as o_f:
        o_w = csv.DictWriter(o_f, fieldnames=l_cols, extrasaction="ignore")
        o_w.writeheader()
        o_w.writerows(l_rows)
    l_keys = ["wall_since_previous_case", "input_preparation", "reference_specific", "corridor_features",
              "short_circuit_count", *STEPS]
    d_summary = {"info": d_info, "cases": len(l_rows)}
    for s_name, l_sel in (("all_cases", l_rows),
                          ("without_first_case_of_switch_state", [r for r in l_rows if not r["first_case_of_switch_state"]])):
        if l_sel:
            d_summary[s_name] = {"n": len(l_sel), **{
                k: {"median": statistics.median(r[k] for r in l_sel), "mean": statistics.mean(r[k] for r in l_sel)}
                for k in l_keys}}
    (p_out / "step_summary.json").write_text(json.dumps(d_summary, indent=1, default=str), encoding="utf-8")
    d_s = d_summary.get("without_first_case_of_switch_state") or d_summary.get("all_cases")
    if d_s:
        print("\n[timing] medians per case (s):")
        for k in ("wall_since_previous_case", "input_preparation", "reference_specific", "reference_infeed",
                  "short_circuits", "network_extraction", "corridor_detection", "corridor_features",
                  "graph_conversion", "setup_switch_state", "setup_randomization", "cleanup"):
            print(f"  {k:26s} {d_s[k]['median']:9.3f}")
    print(f"[timing] written to {p_out}")


def main():
    o_p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    o_p.add_argument("--generator", type=Path, required=True, help="folder of pf-relayreach-generator-v3.4.0")
    o_p.add_argument("--start-switch-state", type=int, default=1)
    o_p.add_argument("--end-switch-state", type=int, default=3)
    o_p.add_argument("--randomized", type=int, default=4, help="randomized cases per switch state (run used 25)")
    o_p.add_argument("--allow-other-grid", action="store_true", help="do not require the 14-bus project")
    a = o_p.parse_args()

    p_gen = a.generator.resolve()
    if not (p_gen / "main_script.py").is_file():
        raise SystemExit(f"main_script.py not found in {p_gen}")
    sys.path.insert(0, str(p_gen))

    import main_script  # noqa: E402  (configures logging to logs/pipeline.log)
    from pf_adaptive_distance_dataset.core.config import Config, RESULTS_DIR

    print(f"[timing] project {Config.PROJECT_NAME}, grid {Config.GRID_NAME}", flush=True)
    if "14" not in Config.PROJECT_NAME and not a.allow_other_grid:
        raise SystemExit("core/config.py is not set to the 14-bus project; switch it or pass --allow-other-grid")
    Config.RANDOMIZED_SCENARIO_COUNT = int(a.randomized)  # in memory only; config.py is unchanged

    p_out = RESULTS_DIR / f"step_timing_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    p_out.mkdir(parents=True, exist_ok=False)
    (p_out / ".powerfactory_start").touch()  # the worker waits for this marker of the batch supervisor
    attach_timers()

    d_info = {"generator": str(p_gen), "project": Config.PROJECT_NAME, "grid": Config.GRID_NAME,
              "switch_states": [a.start_switch_state, a.end_switch_state], "randomized_per_state": a.randomized,
              "include_base_case": Config.INCLUDE_ORIGINAL_BASE_CASE, "started": datetime.now().isoformat()}
    d_state["last_end"] = time.perf_counter()
    t0 = time.perf_counter()
    try:
        main_script.run_generation_worker(
            start_switch_state=a.start_switch_state, end_switch_state=a.end_switch_state,
            output_dir=p_out, random_seed_base=Config.get_random_seed_base(), debug=False)
    finally:
        d_info["total_seconds"] = time.perf_counter() - t0
        d_info["finished"] = datetime.now().isoformat()
        write_results(p_out, d_info)


if __name__ == "__main__":
    main()

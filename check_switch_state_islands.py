"""
Lists the switch states of a library that leave busbars without an external
grid. Runs offline, without PowerFactory.

Usage:
  python -B .\\check_switch_state_islands.py --inventory switch_inventory.csv
      [--library switch_state\\switch_states.csv] [--output islands.csv]
      [--write-clean-library switch_state\\switch_states_clean.csv]

The inventory must come from export_switch_inventory.py v3.4.1 or later.
Exit code 0: no islands found. Exit code 1: islands found.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from pf_adaptive_distance_dataset.core.config import SWITCH_STATE_FILE
from pf_adaptive_distance_dataset.pipeline.switch_state_islands import (
    find_islanded_switch_states,
    read_switch_inventory,
    read_switch_library,
)
from pf_adaptive_distance_dataset.domain.islanding import format_islands


def main(argv=None) -> int:
    o_parser = argparse.ArgumentParser(
        description="List switch states that create islands without an external grid."
    )
    o_parser.add_argument("--inventory", type=Path, required=True)
    o_parser.add_argument("--library", type=Path, default=SWITCH_STATE_FILE)
    o_parser.add_argument("--output", type=Path, help="CSV report (must not exist)")
    o_parser.add_argument(
        "--write-clean-library",
        type=Path,
        help=(
            "Write a copy of the library without the islanded switch states "
            "(must not exist). Note: the ss_<n> numbering of later states shifts."
        ),
    )
    d_args = o_parser.parse_args(argv)

    for p_target in (d_args.output, d_args.write_clean_library):
        if p_target is not None and p_target.exists():
            raise FileExistsError(f"Output already exists: {p_target}")

    df_library, l_switch_columns = read_switch_library(d_args.library)
    l_records = find_islanded_switch_states(
        read_switch_inventory(d_args.inventory),
        df_library,
        l_switch_columns,
    )

    print(f"Library: {d_args.library}")
    print(f"Switch states checked: {len(df_library)}")
    print(f"Switch states with islands: {len(l_records)}")
    for d_record in l_records:
        print(
            f"  {d_record['switch_state_short_id']} "
            f"(row {d_record['switch_state_row_index']}, "
            f"ConfigID={d_record['switch_state_config_id']}): "
            f"{format_islands(d_record['island_terminals'])}"
        )

    if d_args.output is not None:
        d_args.output.parent.mkdir(parents=True, exist_ok=True)
        with d_args.output.open("x", newline="", encoding="utf-8-sig") as o_file:
            o_writer = csv.DictWriter(o_file, fieldnames=[
                "switch_state_row_index",
                "switch_state_short_id",
                "switch_state_config_id",
                "island_count",
                "island_terminals",
            ])
            o_writer.writeheader()
            for d_record in l_records:
                o_writer.writerow({
                    **d_record,
                    "island_terminals": json.dumps(d_record["island_terminals"]),
                })
        print(f"Report: {d_args.output.resolve()}")

    if d_args.write_clean_library is not None:
        s_drop = {d_record["switch_state_row_index"] - 1 for d_record in l_records}
        df_clean = df_library.iloc[
            [i for i in range(len(df_library)) if i not in s_drop]
        ]
        d_args.write_clean_library.parent.mkdir(parents=True, exist_ok=True)
        with d_args.write_clean_library.open("x", newline="", encoding="utf-8-sig") as o_file:
            df_clean.to_csv(o_file, sep=";", index=False)
        print(
            f"Clean library ({len(df_clean)} states): "
            f"{d_args.write_clean_library.resolve()}"
        )

    print("RESULT: ISLANDS FOUND" if l_records else "RESULT: PASSED")
    return 1 if l_records else 0


if __name__ == "__main__":
    raise SystemExit(main())

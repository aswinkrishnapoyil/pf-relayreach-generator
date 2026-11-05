from __future__ import annotations

import argparse
import csv
from pathlib import Path

from pf_adaptive_distance_dataset.core.config import Config, PFAttr
from pf_adaptive_distance_dataset.pf_api.pf_session import PowerFactorySession
from pf_adaptive_distance_dataset.pf_api.pf_utils import (
    get_pf_attribute,
    get_safe_class_name,
    get_safe_full_name,
    get_safe_name,
    is_object_inside_grid,
)
from pf_adaptive_distance_dataset.pipeline.switch_states import (
    _clean_cim_rdf_id,
    _get_generated_switch_identifier,
    get_first_cubicle_switch,
)


FIELDS = [
    "user_independent_switch_id",   # use this one in new switch-state libraries
    "generated_switch_id",          # user-bound form of libraries before v3.4.0
    "switch_cim_id",
    "cubicle_cim_id",
    "switch_name",
    "switch_full_name",
    "cubicle_name",
    "cubicle_full_name",
    "terminal_name",
    "terminal_full_name",
    "connected_object_class",
    "connected_object_name",
    "connected_object_full_name",
    "current_switch_state",
]


def main() -> int:
    o_parser = argparse.ArgumentParser(
        description="Export a read-only PowerFactory switch inventory."
    )
    o_parser.add_argument("--project-name", default=Config.PROJECT_NAME)
    o_parser.add_argument("--grid-name", default=Config.GRID_NAME)
    o_parser.add_argument("--output", type=Path, required=True)
    d_args = o_parser.parse_args()

    if d_args.output.exists():
        raise FileExistsError(f"Output already exists: {d_args.output}")

    l_rows = []
    with PowerFactorySession(
        d_args.project_name,
        d_args.grid_name,
        debug=False,
    ) as o_session:
        s_project_name = get_safe_name(o_session.project)
        s_grid_name = get_safe_name(o_session.grid)

        for o_cubicle in o_session.app.GetCalcRelevantObjects("*.StaCubic") or []:
            o_terminal = get_pf_attribute(o_cubicle, PFAttr.CTERM)
            if o_terminal is None or not is_object_inside_grid(
                o_terminal,
                o_session.grid,
            ):
                continue

            o_switch = get_first_cubicle_switch(o_cubicle)
            if o_switch is None:
                continue

            o_connected = get_pf_attribute(o_cubicle, "obj_id")
            l_rows.append({
                "user_independent_switch_id": _get_generated_switch_identifier(
                    o_switch,
                    o_cubicle,
                    s_project_name,
                    s_grid_name,
                    user_independent=True,
                ),
                "generated_switch_id": _get_generated_switch_identifier(
                    o_switch,
                    o_cubicle,
                    s_project_name,
                    s_grid_name,
                ),
                "switch_cim_id": _clean_cim_rdf_id(o_switch),
                "cubicle_cim_id": _clean_cim_rdf_id(o_cubicle),
                "switch_name": get_safe_name(o_switch),
                "switch_full_name": get_safe_full_name(o_switch),
                "cubicle_name": get_safe_name(o_cubicle),
                "cubicle_full_name": get_safe_full_name(o_cubicle),
                "terminal_name": get_safe_name(o_terminal),
                "terminal_full_name": get_safe_full_name(o_terminal),
                "connected_object_class": get_safe_class_name(o_connected),
                "connected_object_name": get_safe_name(o_connected),
                "connected_object_full_name": get_safe_full_name(o_connected),
                "current_switch_state": get_pf_attribute(
                    o_switch,
                    PFAttr.SWITCH_STATE,
                    "",
                ),
            })

    l_rows.sort(key=lambda d_row: (
        d_row["terminal_full_name"],
        d_row["cubicle_full_name"],
        d_row["switch_full_name"],
    ))
    d_args.output.parent.mkdir(parents=True, exist_ok=True)
    with d_args.output.open("x", newline="", encoding="utf-8-sig") as o_file:
        o_writer = csv.DictWriter(o_file, fieldnames=FIELDS)
        o_writer.writeheader()
        o_writer.writerows(l_rows)

    print(f"Switches exported: {len(l_rows)}")
    print(f"Output: {d_args.output.resolve()}")
    print("RESULT: PASSED" if l_rows else "RESULT: FAILED")
    return 0 if l_rows else 1


if __name__ == "__main__":
    raise SystemExit(main())

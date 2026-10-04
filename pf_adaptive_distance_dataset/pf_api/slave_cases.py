# slave_cases.py
from __future__ import annotations

import logging
import re

from ..core.config import Config
from .pf_utils import get_safe_name


logger = logging.getLogger(__name__)


def delete_powerfactory_object(obj, label: str = "object") -> None:
    """
    Safely deactivates and deletes a PowerFactory object.
    """

    o_object = obj
    s_label = label

    if o_object is None:
        return

    try:
        o_object.Deactivate()

    except Exception:
        pass

    try:
        o_object.Delete()

    except Exception as o_error:
        logger.warning(f"Could not delete {s_label}: {o_error}")


def delete_existing_slave_cases(project, app) -> None:
    """
    Deletes only generator-named study cases and operation scenarios.
    Unrelated objects and the PowerFactory recycle bin are left untouched.
    """

    o_project = project
    l_slave_objects = []
    for s_class, s_prefix in (
        ("IntCase", Config.SLAVE_STUDY_CASE_PREFIX),
        ("IntScenario", Config.SLAVE_OPERATION_SCENARIO_PREFIX),
    ):
        s_pattern = re.escape(s_prefix) + r"_ss_[0-9]{4,}"
        l_slave_objects.extend(
            o_object
            for o_object in (o_project.GetContents(f"*.{s_class}", 1) or [])
            if re.fullmatch(s_pattern, get_safe_name(o_object))
        )

    if not l_slave_objects:
        return

    logger.info(f"Deleting {len(l_slave_objects)} existing slave objects...")

    for o_slave_object in l_slave_objects:
        delete_powerfactory_object(
            o_slave_object,
            label=get_safe_name(o_slave_object),
        )

    logger.info("Existing slave objects deleted.")


def create_slave_case_pair(
    master_study_case,
    master_operation_scenario,
    slave_suffix: str,
):
    """
    Creates one slave study case and one slave operation scenario.
    Links the operation scenario to the study case using an IntRef.
    """

    o_master_study_case = master_study_case
    o_master_operation_scenario = master_operation_scenario
    s_slave_suffix = slave_suffix

    o_study_case_folder = o_master_study_case.GetParent()
    o_operation_scenario_folder = o_master_operation_scenario.GetParent()

    s_slave_study_case_name = (
        f"{Config.SLAVE_STUDY_CASE_PREFIX}_{s_slave_suffix}"
    )

    s_slave_operation_scenario_name = (
        f"{Config.SLAVE_OPERATION_SCENARIO_PREFIX}_{s_slave_suffix}"
    )

    o_slave_study_case = o_study_case_folder.AddCopy(
        o_master_study_case,
        s_slave_study_case_name,
    )

    o_slave_operation_scenario = o_operation_scenario_folder.AddCopy(
        o_master_operation_scenario,
        s_slave_operation_scenario_name,
    )

    o_operation_scenario_link = o_slave_study_case.CreateObject("IntRef")
    o_operation_scenario_link.obj_id = o_slave_operation_scenario

    logger.info(
        f"Created slave study case: {get_safe_name(o_slave_study_case)}"
    )

    logger.info(
        "Created slave operation scenario: "
        f"{get_safe_name(o_slave_operation_scenario)}"
    )

    return o_slave_study_case, o_slave_operation_scenario


def activate_slave_case_pair(slave_sc, slave_os) -> None:
    """
    Activates the slave study case and operation scenario.
    """

    o_slave_study_case = slave_sc
    o_slave_operation_scenario = slave_os

    if o_slave_study_case is None or o_slave_operation_scenario is None:
        raise RuntimeError("Cannot activate missing slave study case/scenario.")

    o_slave_study_case.Activate()
    o_slave_operation_scenario.Activate()


def delete_slave_case_pair(
    app,
    master_study_case,
    master_operation_scenario,
    slave_sc,
    slave_os,
) -> None:
    """
    Re-activates the master case/scenario, then deletes the slave case/scenario.
    """

    o_app = app
    o_master_study_case = master_study_case
    o_master_operation_scenario = master_operation_scenario
    o_slave_study_case = slave_sc
    o_slave_operation_scenario = slave_os

    try:
        if o_master_study_case:
            o_master_study_case.Activate()

        if o_master_operation_scenario:
            o_master_operation_scenario.Activate()

    except Exception as o_error:
        logger.warning(
            f"Could not reactivate master case/scenario: {o_error}"
        )

    delete_powerfactory_object(
        o_slave_study_case,
        label="slave study case",
    )

    delete_powerfactory_object(
        o_slave_operation_scenario,
        label="slave operation scenario",
    )

# pf_session.py
from __future__ import annotations

import sys
import logging

from ..core.config import Config
from .pf_utils import get_safe_name


logger = logging.getLogger(__name__)


class PowerFactorySession:
    """
    Context manager for PowerFactory initialization and cleanup.
    Public attributes intentionally kept unchanged:
    - self.app
    - self.project
    - self.grid
    - self.project_name
    - self.grid_name
    Other modules access these names directly.
    """

    def __init__(self, project_name: str, grid_name: str, debug: bool = False):
        """
        Stores the project and grid names needed to open the PowerFactory session.
        """

        s_project_name = project_name
        s_grid_name = grid_name

        self.project_name = s_project_name
        self.grid_name = s_grid_name
        self.debug = debug

        self.app = None
        self.project = None
        self.grid = None

    def __enter__(self):
        """
        Opens PowerFactory, activates the configured project, and selects the grid.
        Returns:
            PowerFactorySession:
                The active session object containing app, project, and grid.
        """

        sys.path.append(Config.PF_PYTHON_PATH)
        import powerfactory

        logger.info("Connecting to PowerFactory...")

        logger.info("Calling GetApplicationExt()...")
        try:
            self.app = powerfactory.GetApplicationExt()
            logger.info("GetApplicationExt() returned.")
        except Exception:
            logger.warning(
                "GetApplicationExt() failed; falling back to GetApplication().",
                exc_info=True,
            )
            logger.info("Calling GetApplication()...")
            self.app = powerfactory.GetApplication()
            logger.info("GetApplication() returned.")

        if self.app is None:
            raise RuntimeError("PowerFactory connection failed.")

        if self.debug:
            logger.info("Calling PowerFactory Show()...")
            self.app.Show()
            logger.info("PowerFactory Show() returned.")
        else:
            logger.info("Calling PowerFactory Hide()...")
            try:
                self.app.Hide()
                logger.info("PowerFactory Hide() returned.")
            except Exception:
                logger.warning(
                    "PowerFactory Hide() failed or is unavailable.",
                    exc_info=True,
                )
        logger.info("PowerFactory connected.")

        logger.info(f"Activating project: {self.project_name}")

        i_activation_status = self.app.ActivateProject(self.project_name)

        if i_activation_status != 0:
            raise RuntimeError(
                f"Could not activate project: {self.project_name}"
            )

        self.project = self.app.GetActiveProject()

        if self.project is None:
            raise RuntimeError("No active PowerFactory project found.")

        logger.info(f"Selecting target grid: {self.grid_name}")

        l_grid_objects = self.project.GetContents(
            self.grid_name,
            1,
        )

        if not l_grid_objects:
            raise RuntimeError(f"Grid not found: {self.grid_name}")

        self.grid = l_grid_objects[0]

        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        """
        Cleans up Python-side PowerFactory references when leaving the session.
        This does not close PowerFactory. It resets calculation state where
        possible, releases object references, and runs garbage collection.
        """

        if self.app:
            logger.info("Closing PowerFactory Session context.")

        try:
            if self.app:
                self.app.ResetCalculation()

        except Exception as o_error:
            logger.warning(
                "Could not reset PowerFactory calculation during cleanup: "
                f"{o_error}"
            )

        self.grid = None
        self.project = None
        self.app = None

        try:
            import gc

            gc.collect()

        except Exception:
            pass

        return False


def get_required_project_object_by_loc_name(
    project,
    loc_name: str,
    class_name: str,
    label: str,
):
    """
    Finds a required PowerFactory object by loc_name and class name.
    Examples:
    - loc_name='Study Case', class_name='IntCase'
    - loc_name='OS_Master', class_name='IntScenario'
    """

    o_project = project
    s_loc_name = loc_name
    s_class_name = class_name
    s_label = label

    l_project_objects = o_project.GetContents(
        f"*.{s_class_name}",
        1,
    ) or []

    for o_project_object in l_project_objects:
        if get_safe_name(o_project_object) == s_loc_name:
            return o_project_object

    l_available_object_names = [
        get_safe_name(o_project_object)
        for o_project_object in l_project_objects
    ]

    raise RuntimeError(
        f"Could not find {s_label}: {s_loc_name}. "
        f"Available {s_class_name} objects: {l_available_object_names}"
    )

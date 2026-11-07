"""Narrow compatibility bridge for the Fyers SDK's removed pkg_resources dependency."""

import importlib.util
import sys
import types
from importlib.resources import files


def install_pkg_resources_bridge() -> None:
    if importlib.util.find_spec("pkg_resources") is not None or "pkg_resources" in sys.modules:
        return
    module = types.ModuleType("pkg_resources")

    def resource_filename(package: str, resource: str) -> str:
        return str(files(package).joinpath(resource))

    module.resource_filename = resource_filename
    sys.modules["pkg_resources"] = module

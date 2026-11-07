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


def _finished_thread() -> "threading.Thread":
    import threading

    thread = threading.Thread(target=lambda: None, daemon=True)
    thread.start()
    thread.join()
    return thread


def prepare_order_socket_close(sock) -> None:
    """FyersOrderSocket.close_connection() closes the socket and then joins `running_thread`,
    `t` and `ping_thread` unconditionally. `running_thread` only exists if keep_running() was
    called (we never call it) and `ping_thread` only once the socket opened, so the SDK raised
    AttributeError and skipped the remaining joins. Fill the missing ones with an already
    finished thread so the SDK's own close runs to the end."""
    for name in ("running_thread", "t", "ping_thread"):
        if getattr(sock, name, None) is None:
            setattr(sock, name, _finished_thread())

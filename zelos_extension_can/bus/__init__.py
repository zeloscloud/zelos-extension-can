"""Bus configuration, construction and discovery, without DBC decode or tracing.

Importing this package pulls in python-can only: no `cantools`, no demo
simulator, no action registration.
"""

from .config import BUS_DEFAULTS, BusConfigError, bus_database_files, prepare_bus_config
from .discovery import list_interfaces, local_can_interfaces
from .factory import open_python_can_bus

__all__: list[str] = [
    "BUS_DEFAULTS",
    "BusConfigError",
    "bus_database_files",
    "list_interfaces",
    "local_can_interfaces",
    "open_python_can_bus",
    "prepare_bus_config",
]

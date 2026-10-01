"""Trace naming shared by every entry point: the default prefix, the
source/event layout, and which names are usable."""

from collections.abc import Collection

import zelos_sdk

#: Default leading trace-source name. One constant for every entry point:
#: the app's `advanced.prefix`, the `trace` and `convert` CLI commands.
DEFAULT_PREFIX = "CAN"


#: Trace name the extension's own logs take: the source when the prefix is
#: cleared, the event segment under it when set. Reserved as a bus name.
LOG_SOURCE_NAME = "can_log"


def trace_layout(prefix: str, bus: str) -> tuple[str, str | None, str]:
    """The one trace-naming rule, shared by every entry point.

    With a prefix, a single source carries every bus and each bus's events nest
    under it. Cleared, the bus owns the source and its events are unprefixed.

    :return: (source name, event prefix or None, raw-frame event name)
    """
    if prefix:
        return prefix, bus, f"{bus}/Frame"
    return bus, None, "Frame"


def name_error(value: str, label: str, reserved: Collection[str] = ()) -> str | None:
    """Why `value` is not usable as a trace name, or None if it is.

    Trace names are an allow-list — letters, digits, space, `_`, `-`. A prefix
    or bus name is user-typed and becomes a source name or an event segment, so
    a catalog separator (`/ . @ :`) in it would silently re-nest the tree. The
    SDK's sanitizer is the allow-list; anything it rewrites is rejected here
    rather than quietly renamed.
    """
    if not value:
        return None  # cleared prefix / unset bus name; the caller decides
    if value in reserved:
        return f"Invalid {label} {value!r}: reserved for the extension's own log source."
    clean = zelos_sdk.sanitize_name(value, kind="source")
    if clean == value:
        return None
    offender = next((c for c, ok in zip(value, clean, strict=False) if c != ok), value[-1])
    return (
        f"Invalid {label} {value!r}: {offender!r} is not allowed. "
        "Use letters, digits, space, '_' or '-'."
    )

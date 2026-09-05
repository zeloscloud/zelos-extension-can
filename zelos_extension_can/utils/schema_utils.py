"""Utilities for converting cantools types to zelos_sdk types."""

import math

import cantools.database
import zelos_sdk


def cantools_signal_to_trace_type(
    signal: cantools.database.can.signal.Signal,
) -> zelos_sdk.DataType:
    """Map a cantools signal to the DataType that holds its physical domain exactly.

    The conversion picks the domain, as cantools does (LinearIntegerConversion
    vs LinearConversion), and the width follows the physical range:

    - fractional or non-finite factor/offset: Float64, never Float32. fp32
      keeps 24 significant bits, so a 24-bit signal at factor 0.001 stores
      16777214 and 16777215 as one value.
    - IEEE float raw (SIG_VALTYPE_) with identity conversion: its own width.
    - integral factor/offset, identity included: the smallest int holding the
      physical range. A fixed 32-bit type saturated a 24-bit raw at factor
      1000 and typed ``(1,-125)`` unsigned.

    :param signal: cantools signal definition
    :return: Corresponding zelos_sdk DataType
    """
    scale = float(signal.scale if signal.scale is not None else 1)
    offset = float(signal.offset if signal.offset is not None else 0)
    if (
        not (math.isfinite(scale) and math.isfinite(offset))
        or scale != int(scale)
        or offset != int(offset)
    ):
        return zelos_sdk.DataType.Float64
    if signal.is_float:
        return zelos_sdk.DataType.Float64 if signal.length > 32 else zelos_sdk.DataType.Float32
    if signal.is_signed:
        raw_min, raw_max = -(1 << (signal.length - 1)), (1 << (signal.length - 1)) - 1
    else:
        raw_min, raw_max = 0, (1 << signal.length) - 1
    lo, hi = sorted((raw_min * int(scale) + int(offset), raw_max * int(scale) + int(offset)))
    return _int_type_for_range(lo, hi)


_UNSIGNED = ((255, "UInt8"), (65535, "UInt16"), (2**32 - 1, "UInt32"), (2**64 - 1, "UInt64"))
_SIGNED = (
    (-128, 127, "Int8"),
    (-32768, 32767, "Int16"),
    (-(2**31), 2**31 - 1, "Int32"),
    (-(2**63), 2**63 - 1, "Int64"),
)


def _int_type_for_range(lo: int, hi: int) -> zelos_sdk.DataType:
    """Smallest integer type holding ``[lo, hi]``; Float64 if none does."""
    if lo >= 0:
        for top, name in _UNSIGNED:
            if hi <= top:
                return getattr(zelos_sdk.DataType, name)
    else:
        for bottom, top, name in _SIGNED:
            if bottom <= lo and hi <= top:
                return getattr(zelos_sdk.DataType, name)
    return zelos_sdk.DataType.Float64


def cantools_signal_to_trace_metadata(
    signal: cantools.database.can.signal.Signal,
) -> zelos_sdk.TraceEventFieldMetadata:
    """Create TraceEventFieldMetadata from cantools signal.

    :param signal: cantools signal definition
    :return: TraceEventFieldMetadata for zelos_sdk
    """
    # Note: value_table is NOT included here - it's added separately via add_value_table()
    # to avoid sending enum mappings with every event
    return zelos_sdk.TraceEventFieldMetadata(
        name=signal.name,
        data_type=cantools_signal_to_trace_type(signal),
        unit=signal.unit if signal.unit else None,
    )

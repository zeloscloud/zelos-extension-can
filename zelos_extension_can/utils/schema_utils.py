"""Utilities for converting cantools types to zelos_sdk types."""

import cantools.database
import zelos_sdk


def cantools_signal_to_trace_type(
    signal: cantools.database.can.signal.Signal,
) -> zelos_sdk.DataType:
    """Map cantools signal type to zelos_sdk DataType.

    Adapted from zeloscloud.codecs.can.utils._cantools_signal_to_trace_type

    Float / scaled signals use Float64 unconditionally. fp32 can't faithfully
    store decimal-like physical values (e.g. a 12-bit signal with scale 0.001
    stores 4.095 as 4.09499979 because 0.001 has no exact binary representation).
    Float64 has enough decimal precision that `.10g`-formatted display cleanly
    shows the intended value AND string-matches the value-table keys emitted
    by describe_message. The 2x storage cost over Float32 is acceptable;
    per-sample fidelity is not.

    Identity-conversion integer signals (scale=1, offset=0) still pick the
    smallest int that fits the bit field.

    :param signal: cantools signal definition
    :return: Corresponding zelos_sdk DataType
    """
    # Signal is a float (has DBC attribute) or is float post-scaling.
    if signal.is_float or isinstance(signal.scale, float):
        return zelos_sdk.DataType.Float64

    # Identity conversion — map to the smallest int type that fits the bit field.
    if signal.scale == 1 and signal.offset == 0:
        if signal.length <= 8:
            return zelos_sdk.DataType.Int8 if signal.is_signed else zelos_sdk.DataType.UInt8
        if signal.length <= 16:
            return zelos_sdk.DataType.Int16 if signal.is_signed else zelos_sdk.DataType.UInt16
        # For identity conversions between 17-32 bits, use smallest type that fits
        if signal.length <= 32:
            return zelos_sdk.DataType.Int32 if signal.is_signed else zelos_sdk.DataType.UInt32

    # If our signal is greater than 32 bits long
    if signal.length > 32:
        return zelos_sdk.DataType.Int64 if signal.is_signed else zelos_sdk.DataType.UInt64

    # Default: use 32-bit for non-identity conversions (scaled/offset values)
    return zelos_sdk.DataType.Int32 if signal.is_signed else zelos_sdk.DataType.UInt32


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

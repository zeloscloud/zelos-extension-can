"""Utilities for converting cantools types to zelos_sdk types."""

import cantools.database
import zelos_sdk


def cantools_signal_to_trace_type(
    signal: cantools.database.can.signal.Signal,
) -> zelos_sdk.DataType:
    """Map cantools signal type to zelos_sdk DataType.

    Adapted from zeloscloud.codecs.can.utils._cantools_signal_to_trace_type

    :param signal: cantools signal definition
    :return: Corresponding zelos_sdk DataType
    """
    # Signal is a float (has DBC attribute) or is float post-scaling
    if signal.is_float or isinstance(signal.scale, float):
        if signal.length > 32:
            return zelos_sdk.DataType.Float64
        return zelos_sdk.DataType.Float32

    # If this is an identity conversion, map it to the corresponding bit type
    if signal.scale == 1 and signal.offset == 0:
        if signal.length <= 8:
            return zelos_sdk.DataType.Int8 if signal.is_signed else zelos_sdk.DataType.UInt8
        if signal.length <= 16:
            return zelos_sdk.DataType.Int16 if signal.is_signed else zelos_sdk.DataType.UInt16

    # If our signal is greater than 32 bits long
    if signal.length > 32:
        return zelos_sdk.DataType.Int64 if signal.is_signed else zelos_sdk.DataType.UInt64

    return zelos_sdk.DataType.Int32 if signal.is_signed else zelos_sdk.DataType.UInt32


def cantools_signal_to_trace_metadata(
    signal: cantools.database.can.signal.Signal,
) -> zelos_sdk.TraceEventFieldMetadata:
    """Create TraceEventFieldMetadata from cantools signal.

    :param signal: cantools signal definition
    :return: TraceEventFieldMetadata for zelos_sdk
    """
    return zelos_sdk.TraceEventFieldMetadata(
        name=signal.name,
        data_type=cantools_signal_to_trace_type(signal),
        unit=signal.unit if signal.unit else None,
    )

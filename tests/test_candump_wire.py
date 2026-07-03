"""Helper-seam tests for the pure candump/cansend wire helpers.

No ssh, no subprocess, no zelos_can: ``_candump`` is dependency-free and total,
so it is exercised directly. Covers per-kind parse/format, the TOTAL contract
(malformed input never raises, always ``None``), channel parsing, and a
format→parse round-trip matrix.
"""

from types import SimpleNamespace

import can.exceptions
import pytest

from zelos_extension_can._candump import (
    ParsedFrame,
    format_cansend_frame,
    parse_candump_line,
    parse_ssh_channel,
)


def _msg(
    arbitration_id,
    data=b"",
    *,
    is_extended_id=False,
    is_remote_frame=False,
    is_fd=False,
    bitrate_switch=False,
    error_state_indicator=False,
):
    """Duck-typed stand-in for a ``zelos_can.Message`` (next_tx output)."""
    return SimpleNamespace(
        arbitration_id=arbitration_id,
        data=data,
        is_extended_id=is_extended_id,
        is_remote_frame=is_remote_frame,
        is_fd=is_fd,
        bitrate_switch=bitrate_switch,
        error_state_indicator=error_state_indicator,
    )


# ── parse_ssh_channel ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "channel,expected",
    [
        ("host:can0", (None, "host", "can0")),
        ("user@host:can0", ("user", "host", "can0")),
        ("192.168.1.10:vcan0", (None, "192.168.1.10", "vcan0")),  # IPv4
        ("edge.example.com:can0", (None, "edge.example.com", "can0")),  # FQDN
        ("my_edge:can0", (None, "my_edge", "can0")),  # ~/.ssh/config alias
        ("zelos@zelosnuc:can1", ("zelos", "zelosnuc", "can1")),
    ],
)
def test_parse_ssh_channel_valid(channel, expected):
    assert parse_ssh_channel(channel) == expected


@pytest.mark.parametrize(
    "channel",
    [
        "hostcan0",  # no colon
        "can0",  # no colon
        ":can0",  # empty host
        "host:",  # empty iface
        "user@:can0",  # empty host with user
        "",  # empty string
    ],
)
def test_parse_ssh_channel_malformed_raises(channel):
    with pytest.raises(can.exceptions.CanInitializationError):
        parse_ssh_channel(channel)


@pytest.mark.parametrize(
    "channel",
    [
        # ssh option-injection RCE vectors: a host/user beginning with '-' is
        # parsed by ssh as an option, not a hostname.
        "-oProxyCommand=touch /tmp/pwned:can0",  # host is an ssh option
        "-x:can0",  # short host starting with dash
        "-x@host:can0",  # user starting with dash
        "ho st:can0",  # space in host
        "ho;st:can0",  # shell metacharacter in host
        "us@er@host:can0",  # '@' inside the user segment
    ],
)
def test_parse_ssh_channel_injection_rejected(channel):
    with pytest.raises(can.exceptions.CanInitializationError):
        parse_ssh_channel(channel)


# ── parse_candump_line: per-kind ─────────────────────────────────────────────


def test_parse_standard_frame():
    f = parse_candump_line(b"(1687531200.123456) can0 100#1122334455667788")
    assert f == ParsedFrame(
        arb_id=0x100,
        data=bytes.fromhex("1122334455667788"),
        timestamp=1687531200.123456,
        is_extended=False,
        is_fd=False,
        is_remote=False,
        is_error=False,
        brs=False,
        esi=False,
    )


def test_parse_extended_frame():
    f = parse_candump_line(b"(1.0) can0 12345678#AABB")
    assert f.arb_id == 0x12345678
    assert f.data == b"\xaa\xbb"
    assert f.is_extended is True
    assert f.is_error is False


def test_parse_extended_small_id_is_extended_by_width():
    # 8-hex-wide id with a small value is still extended (width, not magnitude).
    f = parse_candump_line(b"(1.0) can0 00000005#00")
    assert f.arb_id == 0x5
    assert f.is_extended is True


def test_parse_empty_data_frame():
    f = parse_candump_line(b"(1.0) can0 100#")
    assert f.data == b""
    assert f.is_fd is False
    assert f.is_remote is False


def test_parse_rtr_frame():
    f = parse_candump_line(b"(1.0) can0 100#R")
    assert f.is_remote is True
    assert f.data == b""
    assert f.is_extended is False


def test_parse_rtr_with_dlc_ignores_dlc():
    f = parse_candump_line(b"(1.0) can0 100#R8")
    assert f.is_remote is True
    assert f.data == b""


def test_parse_extended_rtr():
    f = parse_candump_line(b"(1.0) can0 12345678#R")
    assert f.is_remote is True
    assert f.is_extended is True
    assert f.arb_id == 0x12345678


def test_parse_error_frame():
    # CAN_ERR_FLAG (0x20000000) set → error frame; arb masked to 29 bits.
    f = parse_candump_line(b"(1.0) can0 20000004#0000000000000000")
    assert f.is_error is True
    assert f.arb_id == 0x4
    assert f.is_extended is False


def test_parse_fd_frame():
    f = parse_candump_line(b"(1.0) can0 100##0AABBCC")
    assert f.is_fd is True
    assert f.data == b"\xaa\xbb\xcc"
    assert f.brs is False
    assert f.esi is False


def test_parse_fd_frame_brs():
    f = parse_candump_line(b"(1.0) can0 100##1AABB")
    assert f.is_fd is True
    assert f.brs is True
    assert f.esi is False


def test_parse_fd_frame_esi():
    f = parse_candump_line(b"(1.0) can0 100##2AABB")
    assert f.is_fd is True
    assert f.brs is False
    assert f.esi is True


def test_parse_fd_frame_brs_and_esi():
    f = parse_candump_line(b"(1.0) can0 100##3AABB")
    assert f.brs is True
    assert f.esi is True


def test_parse_fd_extended():
    f = parse_candump_line(b"(1.0) can0 12345678##1AABB")
    assert f.is_fd is True
    assert f.is_extended is True
    assert f.brs is True


def test_parse_fd_empty_data():
    f = parse_candump_line(b"(1.0) can0 100##0")
    assert f.is_fd is True
    assert f.data == b""


def test_parse_nanosecond_precision_timestamp():
    f = parse_candump_line(b"(1234567890.123456789) can0 100#00")
    assert f.timestamp == pytest.approx(1234567890.123456789)


def test_parse_max_len_classic():
    f = parse_candump_line(b"(1.0) can0 100#0011223344556677")
    assert len(f.data) == 8


def test_parse_max_len_fd():
    payload = "AA" * 64
    f = parse_candump_line(f"(1.0) can0 100##0{payload}".encode())
    assert f.is_fd is True
    assert len(f.data) == 64


# ── parse_candump_line: malformed inputs are TOTAL (never raise, always None) ─


@pytest.mark.parametrize(
    "line",
    [
        b"",  # empty line
        b"   ",  # whitespace only
        b"garbage",  # single token
        b"(1.0)",  # bare timestamp
        b"(1.0) can0",  # missing frame token
        b"(1.0) can0 100",  # no '#'
        b"(1.0) can0 10#AA",  # id len 2 (not 3/8)
        b"(1.0) can0 10000#AA",  # id len 5
        b"(1.0) can0 1234567#AA",  # id len 7
        b"(1.0) can0 100#XYZ",  # bad hex payload
        b"(1.0) can0 100#0011223344556677AA",  # classic > 8 bytes
        b"(1.0) can0 100##0" + b"AA" * 65,  # FD > 64 bytes
        b"(1.0) can0 100##",  # FD missing flag nibble
        b"(1.0) can0 100##ZAABB",  # FD bad flag nibble
        b"(1.0) can0 GGG#AA",  # non-hex id
        b"(abc) can0 100#AA",  # non-float timestamp
        b"1.0 can0 100#AA",  # timestamp missing parens
        b"\xff\xfe\x00 not utf8",  # undecodable bytes
    ],
)
def test_parse_malformed_returns_none_never_raises(line):
    assert parse_candump_line(line) is None


# ── format_cansend_frame: per-kind ───────────────────────────────────────────


def test_format_standard():
    assert format_cansend_frame(_msg(0x100, b"\xaa\xbb")) == "100#AABB"


def test_format_extended_width_from_flag_not_magnitude():
    # Small extended id still renders 8 hex wide.
    assert format_cansend_frame(_msg(0x100, b"\xaa\xbb", is_extended_id=True)) == "00000100#AABB"


def test_format_extended_full():
    assert format_cansend_frame(_msg(0x12345678, b"\x01", is_extended_id=True)) == "12345678#01"


def test_format_empty_data():
    assert format_cansend_frame(_msg(0x100, b"")) == "100#"


def test_format_rtr():
    assert format_cansend_frame(_msg(0x100, b"", is_remote_frame=True)) == "100#R"


def test_format_extended_rtr():
    assert (
        format_cansend_frame(_msg(0x12345678, b"", is_extended_id=True, is_remote_frame=True))
        == "12345678#R"
    )


def test_format_fd_no_flags():
    assert format_cansend_frame(_msg(0x100, b"\xaa\xbb", is_fd=True)) == "100##0AABB"


def test_format_fd_brs():
    assert (
        format_cansend_frame(_msg(0x100, b"\xaa\xbb", is_fd=True, bitrate_switch=True))
        == "100##1AABB"
    )


def test_format_fd_esi():
    assert (
        format_cansend_frame(_msg(0x100, b"\xaa\xbb", is_fd=True, error_state_indicator=True))
        == "100##2AABB"
    )


def test_format_fd_brs_and_esi():
    assert (
        format_cansend_frame(
            _msg(0x100, b"\xaa", is_fd=True, bitrate_switch=True, error_state_indicator=True)
        )
        == "100##3AA"
    )


# ── Round-trip: format → wrap in log line → parse recovers id/data/flags ──────


@pytest.mark.parametrize(
    "msg",
    [
        _msg(0x123, b"\x11\x22"),
        _msg(0x7FF, b"\x00\x11\x22\x33\x44\x55\x66\x77"),
        _msg(0x123, b""),
        _msg(0x12345678, b"\xaa\xbb", is_extended_id=True),
        _msg(0x5, b"\x01", is_extended_id=True),
        _msg(0x100, b"", is_remote_frame=True),
        _msg(0x12345678, b"", is_extended_id=True, is_remote_frame=True),
        _msg(0x200, b"\x01\x02\x03", is_fd=True),
        _msg(0x200, b"\x01\x02", is_fd=True, bitrate_switch=True),
        _msg(0x200, b"\x01", is_fd=True, error_state_indicator=True),
        _msg(0x200, b"\xaa" * 64, is_fd=True, bitrate_switch=True, error_state_indicator=True),
        _msg(0x12345678, b"\xde\xad", is_extended_id=True, is_fd=True, bitrate_switch=True),
    ],
)
def test_format_parse_round_trip(msg):
    line = f"(0.0) can0 {format_cansend_frame(msg)}".encode()
    f = parse_candump_line(line)
    assert f is not None
    assert f.arb_id == msg.arbitration_id
    assert f.is_extended == msg.is_extended_id
    assert f.is_remote == msg.is_remote_frame
    assert f.is_fd == msg.is_fd
    if msg.is_remote_frame:
        assert f.data == b""
    else:
        assert f.data == msg.data
    if msg.is_fd:
        assert f.brs == msg.bitrate_switch
        assert f.esi == msg.error_state_indicator

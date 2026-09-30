"""Tests for the free-floating action surface in ``zelos_extension_can.actions``.

The action functions are thin shims over ``CanCodec`` methods, but the routing
layer they add (``CAN_CODECS`` dict lookup, error on unknown codec, the
discovery action) is its own thing and worth covering directly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from zelos_sdk.extensions.actions import get_standalone_actions

from zelos_extension_can import ACTION_PREFIX, actions
from zelos_extension_can.codec import CanCodec

DBC_PATH = Path(__file__).parent / "files" / "test.dbc"


def _make_codec(bus_name: str, channel: str) -> CanCodec:
    with patch("zelos_sdk.TraceSource"), patch("can.Bus"):
        cfg = {
            "interface": "virtual",
            "channel": channel,
            "bitrate": 500_000,
            "database_files": [str(DBC_PATH)],
        }
        codec = CanCodec(cfg, bus_name=bus_name)
        codec.start()
        return codec


@pytest.fixture
def two_codecs():
    """Two registered codecs ('busA', 'busB') in actions.CAN_CODECS; both
    cleaned up + the registry drained on teardown."""
    a = _make_codec("busA", "vcan0")
    b = _make_codec("busB", "vcan1")
    actions.CAN_CODECS["busA"] = a
    actions.CAN_CODECS["busB"] = b
    yield a, b
    actions.CAN_CODECS.pop("busA", None)
    actions.CAN_CODECS.pop("busB", None)
    a.stop()
    b.stop()


class TestRegistry:
    def test_list_codecs_reflects_registry(self, two_codecs):
        assert actions.list_codecs() == {"codecs": ["busA", "busB"]}

    def test_list_codecs_returns_sorted_output_regardless_of_insert_order(self):
        # Insert in reverse to prove the sort isn't an artifact of dict order.
        b = _make_codec("busB", "vcan1")
        a = _make_codec("busA", "vcan0")
        actions.CAN_CODECS["busB"] = b
        actions.CAN_CODECS["busA"] = a
        try:
            assert actions.list_codecs() == {"codecs": ["busA", "busB"]}
        finally:
            actions.CAN_CODECS.pop("busA", None)
            actions.CAN_CODECS.pop("busB", None)
            a.stop()
            b.stop()

    def test_list_codecs_empty_when_nothing_registered(self):
        # The fixture isn't applied here — CAN_CODECS should be empty between
        # tests. Defensive: assert it's empty so a leak from another test
        # fails loudly here instead of silently.
        assert actions.CAN_CODECS == {}
        assert actions.list_codecs() == {"codecs": []}

    def test_unknown_codec_raises_with_available_list(self, two_codecs):
        with pytest.raises(ValueError, match="Unknown CAN codec 'nope'"):
            actions.get_tx_state("nope")


class TestDispatch:
    def test_get_tx_state_routes_to_named_codec(self, two_codecs):
        a, b = two_codecs
        assert actions.get_tx_state("busA")["bus"]["name"] == "busA"
        assert actions.get_tx_state("busB")["bus"]["name"] == "busB"

    def test_list_messages_routes_to_named_codec(self, two_codecs):
        # Both codecs use the same DBC, so the count matches; the `bus` field
        # is what proves dispatch landed on the right instance.
        assert actions.list_messages("busA")["bus"] == "busA"
        assert actions.list_messages("busB")["bus"] == "busB"

    def test_describe_message_routes_to_named_codec(self, two_codecs):
        msg_name = actions.list_messages("busA")["messages"][0]["name"]
        desc = actions.describe_message("busA", msg_name)
        assert desc["bus"] == "busA"
        assert desc["message"]["name"] == msg_name

    def test_encode_preview_routes_to_named_codec(self, two_codecs):
        result = actions.encode_preview("busA", "DUT_Command", '{"state_request": 1}')
        assert "data_hex" in result
        assert "can_id" in result

    def test_send_raw_routes_to_named_codec(self, two_codecs):
        # Mutating action — confirms the dispatch landed on the right CanCodec
        # by inspecting which mocked bus saw the `send()` call.
        a, b = two_codecs
        actions.send_raw("busA", "0x100", "01 02 03 04")
        assert a.bus.send.call_count == 1  # type: ignore[attr-defined]
        assert b.bus.send.call_count == 0  # type: ignore[attr-defined]

    def test_stop_periodic_routes_to_named_codec(self, two_codecs):
        # stop_periodic on an unknown task_id is a no-op success — we only need
        # to confirm it executes through the named codec without raising.
        result = actions.stop_periodic("busB", "nonexistent")
        assert result == {"task_id": "nonexistent", "stopped": False}


class TestConverterDbcResolution:
    # Failures must *raise* — the actions protocol derives its error verdict
    # from a raised exception, not from a payload key.
    def test_no_database_converts_raw_only(self, two_codecs, tmp_path):
        # No database_path, no codec — raw frames only, like the CLI. Input
        # doesn't exist, so resolution getting as far as the file check proves
        # there is no "neither was given" guard left.
        with pytest.raises(FileNotFoundError, match="Input file not found"):
            actions.convert_trace_file(
                input_path=str(tmp_path / "missing.log"),
                database_path="",
                codec="",
            )

    def test_codec_fallback_uses_codecs_dbc(self, two_codecs, tmp_path):
        # Input doesn't exist — we only care that codec resolution gets past
        # the "neither was given" guard. The "Input file not found" branch
        # proves we successfully resolved a database from the codec.
        with pytest.raises(FileNotFoundError, match="Input file not found"):
            actions.convert_trace_file(
                input_path=str(tmp_path / "missing.log"),
                database_path="",
                codec="busA",
            )

    def test_unknown_codec_in_fallback_is_explicit_error(self, two_codecs, tmp_path):
        with pytest.raises(ValueError, match="Unknown CAN codec"):
            actions.convert_trace_file(
                input_path=str(tmp_path / "missing.log"),
                database_path="",
                codec="nope",
            )


class TestStandaloneConvert:
    """The at-rest ``convert`` action.

    Distinct from ``convert_trace_file`` above: this is the one declared
    ``standalone=True``, so it runs in a one-shot interpreter with no extension
    process, no bus, and nothing listening for its logs. Raising is the only
    failure signal a caller gets, because a plain return means "no verdict",
    which the wire maps to DONE.
    """

    def _log(self, tmp_path: Path) -> Path:
        src = tmp_path / "capture.log"
        src.write_text("(0.0) can0 100#0011223344556677\n")
        return src

    def test_missing_input_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Input file not found"):
            actions.convert(input_file=str(tmp_path / "missing.log"))

    def test_unsupported_format_raises(self, tmp_path):
        src = tmp_path / "capture.wat"
        src.write_text("nope")
        with pytest.raises(ValueError, match="Unsupported format"):
            actions.convert(input_file=str(src))

    def test_no_database_anywhere_converts_raw_only(self, tmp_path):
        src = self._log(tmp_path)
        with patch.object(actions, "_configured_database_files", return_value=[]):
            result = actions.convert(input_file=str(src))
        assert result["database_files"] == []
        assert Path(result["output_file"]).is_file()

    def test_missing_database_raises(self, tmp_path):
        src = self._log(tmp_path)
        with pytest.raises(FileNotFoundError, match="CAN database file not found"):
            actions.convert(input_file=str(src), database_file=str(tmp_path / "nope.dbc"))

    def test_existing_output_without_force_raises(self, tmp_path):
        src = self._log(tmp_path)
        dest = tmp_path / "capture.trz"
        dest.write_text("occupied")
        with pytest.raises(FileExistsError, match="Output exists"):
            actions.convert(input_file=str(src), database_file=str(DBC_PATH), output_file=str(dest))


class TestOpenInApp:
    """`_open_in_app`'s opener contract.

    Both Popen kwargs asserted here are load-bearing, not cosmetic. A standalone
    action runs in a setsid one-shot whose *process group* the supervisor kills
    on abnormal exit, and whose stdout/stderr it drains to EOF before considering
    the run finished. A child in that group dies with it; a child inheriting
    those pipes holds them open past the action's return, stalling the run and
    then tripping the terminate path.
    """

    def test_posix_spawn_is_detached_with_no_inherited_pipes(self, tmp_path):
        # Runs the darwin/linux branch — the win32 branch is covered below.
        import subprocess

        target = tmp_path / "out.trz"
        with patch("subprocess.Popen") as popen:
            actions._open_in_app(target)

        assert popen.call_count == 1
        args, kwargs = popen.call_args
        assert kwargs["start_new_session"] is True, "opener must escape the killed process group"
        for stream in ("stdin", "stdout", "stderr"):
            assert kwargs[stream] is subprocess.DEVNULL, f"{stream} must not hold the run's pipes"
        # execve'd argv, no shell: `open`/`xdg-open` get the path as one argument.
        assert isinstance(args[0], list)
        assert str(target) in args[0]

    def test_windows_hands_the_path_to_shell_execute_not_cmd(self, tmp_path):
        # `cmd /c start` re-parses its command line and list2cmdline quotes only
        # for whitespace, so this space-free path would select a command there.
        target = tmp_path / "out&calc.exe.trz"
        with (
            patch.object(actions.sys, "platform", "win32"),
            patch.object(actions.os, "startfile", create=True) as startfile,
            patch("subprocess.Popen") as popen,
        ):
            actions._open_in_app(target)

        startfile.assert_called_once_with(target)
        popen.assert_not_called()

    def test_open_failure_does_not_fail_a_finished_conversion(self, tmp_path):
        src = tmp_path / "capture.log"
        src.write_text("(0.0) can0 100#0011223344556677\n")
        dest = tmp_path / "capture.trz"

        class _Stats:
            def to_dict(self):
                return {}

        def _write_trace(_input, _database, output, **_kwargs):
            # The action publishes the file the converter wrote, so the stub has
            # to write one.
            output.write_text("trace")
            return _Stats()

        # `convert` imports convert_can_trace inside the function body, so it
        # must be patched where it is defined, not on the actions module.
        with (
            patch("zelos_extension_can.converter.convert_can_trace", _write_trace),
            patch.object(actions, "_open_in_app", side_effect=OSError("no opener")),
        ):
            result = actions.convert(
                input_file=str(src),
                database_file=str(DBC_PATH),
                output_file=str(dest),
                open_on_complete=True,
            )

        # The trace exists by this point. A failed open reported as a failed
        # conversion would send the caller to re-run work that already finished.
        assert result["status"] == "success"
        assert result["opened"] is False
        assert "no opener" in result["open_error"]
        assert dest.read_text() == "trace"  # staged output landed at the destination


def _sys_class_net(root: Path) -> Path:
    """A /sys/class/net tree: can0 (up) and can1 (down) on gs_usb, a vcan0, an eth0."""
    net = root / "net"
    gs_usb = root / "bus" / "usb" / "drivers" / "gs_usb"
    gs_usb.mkdir(parents=True)

    can0 = net / "can0"
    (can0 / "device").mkdir(parents=True)
    (can0 / "type").write_text("280\n")
    (can0 / "operstate").write_text("up\n")
    (can0 / "device" / "driver").symlink_to(gs_usb)  # sysfs: link INTO the driver

    can1 = net / "can1"  # a real adapter that is not up yet
    (can1 / "device").mkdir(parents=True)
    (can1 / "type").write_text("280\n")
    (can1 / "operstate").write_text("down\n")
    (can1 / "device" / "driver").symlink_to(gs_usb)

    vcan0 = net / "vcan0"  # virtual: no device, and operstate never leaves "unknown"
    vcan0.mkdir(parents=True)
    (vcan0 / "type").write_text("280\n")
    (vcan0 / "operstate").write_text("unknown\n")

    eth0 = net / "eth0"  # ARPHRD_ETHER — not a CAN interface
    eth0.mkdir(parents=True)
    (eth0 / "type").write_text("1\n")
    (eth0 / "operstate").write_text("up\n")
    return net


def _port(device: str, vid: int | None, pid: int | None, description: str = "") -> SimpleNamespace:
    """A pyserial ListPortInfo, reduced to the fields detection reads."""
    return SimpleNamespace(device=device, vid=vid, pid=pid, description=description)


#: A PCAN-USB Pro FD as python-can 4.6.1 reports it on Windows: two CAN channels.
_PCAN_PRO_FD = [
    {
        "interface": "pcan",
        "channel": f"PCAN_USBBUS{n}",
        "supports_fd": True,
        "device_name": "PCAN-USB Pro FD",
    }
    for n in (1, 2)
]


class TestConfigFormHooks:
    """The two schema hooks the app calls on a form that has never started."""

    @pytest.fixture(autouse=True)
    def _no_adapters(self, monkeypatch):
        """Hide this machine's own adapters and serial ports; a test adds the ones it needs."""
        monkeypatch.setattr(actions, "_vendor_adapters", list)
        monkeypatch.setattr(actions, "_slcan_adapters", list)

    def test_list_interfaces_offers_can_devices_only_hardware_first(self, monkeypatch, tmp_path):
        monkeypatch.setattr(actions.sys, "platform", "linux")
        monkeypatch.setattr(actions, "_SYS_CLASS_NET", _sys_class_net(tmp_path))

        assert actions.list_interfaces() == {
            "status": "success",
            "choices": [
                {"value": "can0", "detail": "up, gs_usb"},
                {"value": "can1", "detail": "down, gs_usb"},
                {"value": "vcan0", "detail": "virtual"},
            ],
        }

    def test_list_interfaces_is_empty_where_there_is_no_socketcan(self, monkeypatch, tmp_path):
        """macOS/Windows: an empty list, not an error — nothing to enumerate."""
        monkeypatch.setattr(actions.sys, "platform", "darwin")
        monkeypatch.setattr(actions, "_SYS_CLASS_NET", _sys_class_net(tmp_path))

        assert actions.list_interfaces() == {"status": "success", "choices": []}

    def test_auto_config_builds_one_native_bus_per_interface(self, monkeypatch, tmp_path):
        monkeypatch.setattr(actions.sys, "platform", "linux")
        monkeypatch.setattr(actions, "_SYS_CLASS_NET", _sys_class_net(tmp_path))

        # Only `buses`: the contract replaces the keys returned, so anything the
        # person set under Advanced survives. No `message` either — a down
        # interface is configured as it is and the picker already says `down`.
        assert actions.auto_config() == {
            "status": "success",
            "config": {
                "buses": [
                    {"interface": "socketcan", "channel": "can0", "database_files": []},
                    {"interface": "socketcan", "channel": "can1", "database_files": []},
                    {"interface": "socketcan", "channel": "vcan0", "database_files": []},
                ]
            },
        }

    @pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
    def test_auto_config_without_an_interface_is_a_demo_bus(self, monkeypatch, tmp_path, platform):
        monkeypatch.setattr(actions.sys, "platform", platform)
        monkeypatch.setattr(actions, "_SYS_CLASS_NET", tmp_path / "empty")

        result = actions.auto_config()

        assert result["config"] == {"buses": [{"name": "demo", "interface": "demo"}]}
        assert "ssh-socketcan" in result["message"]  # the way out on a laptop

    def test_auto_config_adds_one_bus_per_adapter_channel(self, monkeypatch, tmp_path):
        monkeypatch.setattr(actions.sys, "platform", "win32")
        monkeypatch.setattr(actions, "_SYS_CLASS_NET", tmp_path / "empty")
        monkeypatch.setattr(
            actions,
            "_vendor_adapters",
            lambda: [
                {"interface": "pcan", "channel": c["channel"], "name": c["device_name"]}
                for c in _PCAN_PRO_FD
            ],
        )

        assert actions.auto_config() == {
            "status": "success",
            "config": {
                "buses": [
                    {
                        "interface": "pcan",
                        "channel": "PCAN_USBBUS1",
                        "bitrate": 500_000,
                        "database_files": [],
                    },
                    {
                        "interface": "pcan",
                        "channel": "PCAN_USBBUS2",
                        "bitrate": 500_000,
                        "database_files": [],
                    },
                ]
            },
            "message": (
                "Found PCAN-USB Pro FD on PCAN_USBBUS1, PCAN-USB Pro FD on PCAN_USBBUS2. "
                "Each is set to 500 kbit/s. Change Bitrate to match your bus."
            ),
        }

    def test_auto_config_keeps_a_running_bus_detection_cannot_see(self, monkeypatch, tmp_path):
        """PCAN on macOS lists only free channels, so the one this extension holds drops out."""
        monkeypatch.setattr(actions.sys, "platform", "darwin")
        monkeypatch.setattr(actions, "_SYS_CLASS_NET", tmp_path / "empty")
        monkeypatch.setattr(
            actions,
            "_vendor_adapters",
            lambda: [{"interface": "pcan", "channel": "PCAN_USBBUS2", "name": "pcan"}],
        )
        running = SimpleNamespace(
            config={"interface": "pcan", "channel": "PCAN_USBBUS1", "bitrate": 250_000}
        )
        monkeypatch.setitem(actions.CAN_CODECS, "PCAN_USBBUS1", running)

        result = actions.auto_config()

        assert result["config"]["buses"] == [
            {
                "interface": "pcan",
                "channel": "PCAN_USBBUS2",
                "bitrate": 500_000,
                "database_files": [],
            },
            {
                "interface": "pcan",
                "channel": "PCAN_USBBUS1",
                "bitrate": 250_000,
                "database_files": [],
            },
        ]
        assert result["message"] == (
            "Found pcan on PCAN_USBBUS2, pcan on PCAN_USBBUS1. Running buses keep their "
            "bitrate; new ones are set to 500 kbit/s. Change Bitrate to match your bus."
        )

    def test_auto_config_keeps_the_bitrate_of_a_detected_running_bus(self, monkeypatch, tmp_path):
        """Windows lists an occupied PCAN channel too; its bitrate must not reset to 500k."""
        monkeypatch.setattr(actions.sys, "platform", "win32")
        monkeypatch.setattr(actions, "_SYS_CLASS_NET", tmp_path / "empty")
        monkeypatch.setattr(
            actions,
            "_vendor_adapters",
            lambda: [{"interface": "pcan", "channel": "PCAN_USBBUS1", "name": "PCAN-USB"}],
        )
        running = SimpleNamespace(
            config={"interface": "pcan", "channel": "PCAN_USBBUS1", "bitrate": 1_000_000}
        )
        monkeypatch.setitem(actions.CAN_CODECS, "PCAN_USBBUS1", running)

        buses = actions.auto_config()["config"]["buses"]

        assert buses == [
            {
                "interface": "pcan",
                "channel": "PCAN_USBBUS1",
                "bitrate": 1_000_000,
                "database_files": [],
            }
        ]

    def test_a_running_socketcan_or_demo_bus_adds_no_adapter(self, monkeypatch, tmp_path):
        monkeypatch.setattr(actions.sys, "platform", "win32")
        monkeypatch.setattr(actions, "_SYS_CLASS_NET", tmp_path / "empty")
        monkeypatch.setitem(
            actions.CAN_CODECS,
            "demo",
            SimpleNamespace(config={"interface": "demo", "name": "demo"}),
        )

        assert actions.auto_config()["config"] == {"buses": [{"name": "demo", "interface": "demo"}]}

    def test_auto_config_buses_are_valid_config(self, monkeypatch, tmp_path):
        """What the button writes must pass the schema the form saves against."""
        jsonschema = pytest.importorskip("jsonschema")
        monkeypatch.setattr(actions.sys, "platform", "linux")
        monkeypatch.setattr(actions, "_SYS_CLASS_NET", _sys_class_net(tmp_path))
        monkeypatch.setattr(
            actions,
            "_vendor_adapters",
            lambda: [{"interface": "kvaser", "channel": "0", "name": "Kvaser"}],
        )
        monkeypatch.setattr(
            actions,
            "_slcan_adapters",
            lambda: [{"interface": "slcan", "channel": "COM3", "name": "CANable"}],
        )
        schema = json.loads((Path(__file__).parents[1] / "config.schema.json").read_text())

        jsonschema.Draft7Validator(schema).validate(actions.auto_config()["config"])

    def test_auto_config_skips_serial_ports_slcand_already_attached(self, monkeypatch, tmp_path):
        net = _sys_class_net(tmp_path)
        slcan0 = net / "slcan0"
        slcan0.mkdir()
        (slcan0 / "type").write_text("280\n")
        (slcan0 / "operstate").write_text("up\n")
        monkeypatch.setattr(actions.sys, "platform", "linux")
        monkeypatch.setattr(actions, "_SYS_CLASS_NET", net)
        monkeypatch.setattr(
            actions,
            "_slcan_adapters",
            lambda: [{"interface": "slcan", "channel": "/dev/ttyACM0", "name": "CANable"}],
        )

        buses = actions.auto_config()["config"]["buses"]

        assert {bus["interface"] for bus in buses} == {"socketcan"}
        assert "slcan0" in {bus["channel"] for bus in buses}

    def test_schema_hooks_name_actions_that_exist(self):
        """Both hooks wire the form to an action by name, so a rename breaks the
        form silently. `get_standalone_actions` is the same index the packaged
        `actions.json` is dumped from, keyed without the prefix.
        """
        schema = json.loads((Path(__file__).parents[1] / "config.schema.json").read_text())
        branches = schema["properties"]["buses"]["items"]["dependencies"]["interface"]["oneOf"]
        channels = [b["properties"].get("channel", {}) for b in branches]
        named = {schema["ui:options"]["autoconfig"]} | {
            ch["ui:options"]["action"] for ch in channels if "action" in ch.get("ui:options", {})
        }

        assert named == {f"{ACTION_PREFIX}/auto_config", f"{ACTION_PREFIX}/list_interfaces"}
        assert {n.split("/", 1)[1] for n in named} <= set(get_standalone_actions())


class TestAdapterDetection:
    """What each detection source reports, with the vendor libraries and serial ports stubbed."""

    def test_vendor_adapters_list_each_channel_as_the_interface_enum_names_it(self, monkeypatch):
        answers = {
            "pcan": _PCAN_PRO_FD,
            "kvaser": [{"interface": "kvaser", "channel": 0}],
            "vector": [],
        }
        monkeypatch.setattr(actions.sys, "platform", "win32")

        with patch(
            "can.detect_available_configs", side_effect=lambda interfaces: answers[interfaces[0]]
        ):
            found = actions._vendor_adapters()

        assert found == [
            {"interface": "pcan", "channel": "PCAN_USBBUS1", "name": "PCAN-USB Pro FD"},
            {"interface": "pcan", "channel": "PCAN_USBBUS2", "name": "PCAN-USB Pro FD"},
            {"interface": "kvaser", "channel": "0", "name": "kvaser"},
        ]

    def test_a_failing_vendor_library_hides_only_its_own_adapters(self, monkeypatch):
        def detect(interfaces):
            if interfaces == ["kvaser"]:
                raise OSError("canlib32.dll is broken")
            return _PCAN_PRO_FD if interfaces == ["pcan"] else []

        monkeypatch.setattr(actions.sys, "platform", "win32")
        with patch("can.detect_available_configs", side_effect=detect):
            found = actions._vendor_adapters()

        assert [a["channel"] for a in found] == ["PCAN_USBBUS1", "PCAN_USBBUS2"]

    def test_detection_mutes_only_missing_library_warnings(self, monkeypatch, caplog):
        """A live extension's bus keeps logging while Auto-configure runs in its process."""

        def detect(interfaces):
            logging.getLogger("can.kvaser").warning("Kvaser canlib is unavailable.")
            logging.getLogger("can.interfaces.vector.canlib").warning("Could not import vxlapi")
            logging.getLogger("can.pcan").warning("Bus error on the running bus")
            return []

        monkeypatch.setattr(actions.sys, "platform", "win32")
        levels = {n: logging.getLogger(n).level for n in actions._MISSING_LIBRARY_LOGGERS}
        with (
            caplog.at_level(logging.WARNING),
            patch("can.detect_available_configs", side_effect=detect),
        ):
            actions._vendor_adapters()

        messages = [r.getMessage() for r in caplog.records]
        assert messages.count("Bus error on the running bus") == 3
        assert not any("Kvaser" in m or "vxlapi" in m for m in messages)
        assert {n: logging.getLogger(n).level for n in levels} == levels

    def test_linux_leaves_vendor_adapters_to_socketcan(self, monkeypatch):
        monkeypatch.setattr(actions.sys, "platform", "linux")
        with patch("can.detect_available_configs") as detect:
            assert actions._vendor_adapters() == []
        detect.assert_not_called()

    def test_slcan_adapters_are_serial_ports_with_an_slcan_usb_id(self):
        ports = [
            _port("COM1", None, None, "Communications Port (COM1)"),
            _port("COM3", 0x16D0, 0x117E, "CANable"),
            _port("COM4", 0x0403, 0xFFA8, "CANUSB"),
            _port("COM5", 0x0403, 0x6001, "USB Serial Port"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=ports):
            found = actions._slcan_adapters()

        assert found == [
            {"interface": "slcan", "channel": "COM3", "name": "CANable"},
            {"interface": "slcan", "channel": "COM4", "name": "CANUSB"},
        ]

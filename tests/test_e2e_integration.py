"""End-to-end integration tests for the CAN extension.

Exercises the full codec stack (init → start → receive → decode → emit → stop)
on a virtual CAN bus in each mode (standard DBC, J1939, J1939+DBC) to validate
no regressions and production readiness.

Run with Zelos tracing to generate real .trz artifacts:
    zelos test tests/test_e2e_integration.py --zelos-trace-file -v
"""

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import can
import pytest

from zelos_extension_can.codec import CanCodec

TEST_DBC = str(Path(__file__).parent / "files" / "test.dbc")
J1939_DBC = str(Path(__file__).parent / "files" / "test_j1939.dbc")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_codec_with_messages(config: dict, messages: list[can.Message], duration: float = 0.5):
    """Create a codec, feed it messages, and return metrics + codec."""
    with patch("zelos_sdk.TraceSource"):
        codec = CanCodec(config)

    # Feed messages directly via the listener interface (no real bus needed)
    for msg in messages:
        codec._handle_message(msg)

    return codec


def _build_j1939_frame(pgn: int, sa: int, data: bytes, priority: int = 6) -> can.Message:
    """Build a J1939 extended CAN message."""
    pdu_format = (pgn >> 8) & 0xFF
    pdu_specific = pgn & 0xFF
    arb_id = (priority << 26) | (pdu_format << 16) | (pdu_specific << 8) | sa
    return can.Message(arbitration_id=arb_id, data=data, is_extended_id=True)


def _build_bam_sequence(pgn: int, sa: int, payload: bytes) -> list[can.Message]:
    """Build a BAM TP sequence (CM + DT frames)."""
    total_bytes = len(payload)
    total_packets = (total_bytes + 6) // 7
    msgs = []
    # TP.CM BAM
    cm_arb = (6 << 26) | (0xEC << 16) | (0xFF << 8) | sa
    cm_data = bytes(
        [
            32,
            total_bytes & 0xFF,
            (total_bytes >> 8) & 0xFF,
            total_packets,
            0xFF,
            pgn & 0xFF,
            (pgn >> 8) & 0xFF,
            (pgn >> 16) & 0xFF,
        ]
    )
    msgs.append(can.Message(arbitration_id=cm_arb, data=cm_data, is_extended_id=True))
    # TP.DT frames
    for seq in range(1, total_packets + 1):
        start = (seq - 1) * 7
        chunk = payload[start : start + 7]
        if len(chunk) < 7:
            chunk = chunk + bytes([0xFF] * (7 - len(chunk)))
        dt_arb = (6 << 26) | (0xEB << 16) | (0xFF << 8) | sa
        dt_data = bytes([seq]) + chunk
        msgs.append(can.Message(arbitration_id=dt_arb, data=dt_data, is_extended_id=True))
    return msgs


# ===========================================================================
# Phase 3: EV Demo Baseline (standard DBC, no J1939)
# ===========================================================================


class TestEVDemoBaseline:
    """Validate existing DBC-based decode has zero regressions."""

    @pytest.fixture
    def ev_config(self):
        return {
            "interface": "virtual",
            "channel": "vcan0",
            "database_file": TEST_DBC,
        }

    def test_standard_decode_produces_signals(self, ev_config):
        """Standard 11-bit CAN messages decode via DBC."""
        codec = _run_codec_with_messages(
            ev_config,
            [
                can.Message(arbitration_id=0x64, data=bytes(8), timestamp=1.0),
                can.Message(arbitration_id=0x64, data=bytes(8), timestamp=2.0),
            ],
        )
        assert codec.metrics.messages_received == 2
        assert codec.metrics.messages_decoded == 2
        assert codec.metrics.unknown_messages == 0
        assert codec.metrics.decode_errors == 0

    def test_unknown_message_counted(self, ev_config):
        """Messages not in DBC are counted as unknown."""
        codec = _run_codec_with_messages(
            ev_config,
            [
                can.Message(arbitration_id=0xFFF, data=bytes(8), timestamp=1.0),
            ],
        )
        assert codec.metrics.unknown_messages == 1
        assert codec.metrics.messages_decoded == 0

    def test_no_protocol_handler_when_j1939_disabled(self, ev_config):
        """Protocol handler is None when J1939 not enabled."""
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(ev_config)
        assert codec._protocol_handler is None

    def test_extended_frames_without_j1939_count_as_unknown(self, ev_config):
        """29-bit extended frames without J1939 enabled → unknown (not in DBC)."""
        codec = _run_codec_with_messages(
            ev_config,
            [
                can.Message(arbitration_id=0x18FEF100, data=bytes(8), is_extended_id=True),
            ],
        )
        assert codec.metrics.unknown_messages == 1

    def test_emit_schemas_on_init(self):
        """emit_schemas_on_init generates all schemas at init time."""
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "database_file": TEST_DBC,
            "emit_schemas_on_init": True,
        }
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)
        assert len(codec._events) > 0

    def test_raw_frame_logging_config(self):
        """log_raw_frames creates raw trace source."""
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "database_file": TEST_DBC,
            "log_raw_frames": True,
        }
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)
        assert codec.raw_source is not None
        assert codec.raw_event is not None

    def test_multiplexed_message_decode(self):
        """Multiplexed messages produce both base and mux events."""
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "database_file": TEST_DBC,
        }
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)

        # DUT_Logging (0x12c = 300) from test.dbc — has multiplexer signal
        msg = can.Message(arbitration_id=0x12C, data=bytes(8), timestamp=1.0)
        codec._handle_message(msg)
        assert codec.metrics.messages_decoded >= 1


# ===========================================================================
# Phase 4: J1939 Demo — Protocol Handler Only (no DBC)
# ===========================================================================


class TestJ1939ProtocolOnly:
    """Validate J1939 protocol handler works without a DBC file."""

    @pytest.fixture
    def j1939_config(self):
        return {
            "interface": "virtual",
            "channel": "vcan0",
            "j1939": True,
        }

    def test_j1939_handler_active(self, j1939_config):
        """J1939 handler is created when j1939=True."""
        from zelos_extension_can.protocols.j1939.handler import J1939Handler

        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(j1939_config)
        assert isinstance(codec._protocol_handler, J1939Handler)

    def test_no_dbc_produces_empty_database(self, j1939_config):
        """Without database_file, DB is empty but codec doesn't crash."""
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(j1939_config)
        assert len(codec.db.messages) == 0
        assert codec.database_file_path is None

    def test_j1939_single_frame_not_consumed(self, j1939_config):
        """Single-frame J1939 messages return False (not consumed by handler)."""
        codec = _run_codec_with_messages(
            j1939_config,
            [
                _build_j1939_frame(0xFEF1, sa=0x00, data=bytes(8)),
            ],
        )
        # Frame passes through handler (returns False) then hits DBC decode
        # which finds nothing (no DBC) → counted as unknown
        assert codec.metrics.messages_received == 1
        assert codec.metrics.unknown_messages == 1

    def test_j1939_tp_frames_consumed(self, j1939_config):
        """TP.CM/TP.DT frames are consumed by handler (not passed to DBC directly)."""
        bam_msgs = _build_bam_sequence(
            pgn=0xFECA,
            sa=0x00,
            payload=bytes([0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF]),  # No-fault DM1
        )
        codec = _run_codec_with_messages(j1939_config, bam_msgs)

        # TP reassembly completes successfully
        assert codec._protocol_handler._tp.completed_transfers == 1
        # Reassembled payload is fed to DBC decode — PGN 0xFECA not in DBC → 1 unknown
        # (the TP.CM and TP.DT frames themselves are NOT counted as unknown)
        assert codec.metrics.unknown_messages == 1
        # Total messages = CM + DT frames (all consumed by TP handler)
        assert codec.metrics.messages_received == len(bam_msgs)

    def test_j1939_address_tracking(self, j1939_config):
        """Source addresses are tracked across frames."""
        codec = _run_codec_with_messages(
            j1939_config,
            [
                _build_j1939_frame(0xFEF1, sa=0x00, data=bytes(8)),
                _build_j1939_frame(0xFEF2, sa=0x00, data=bytes(8)),
                _build_j1939_frame(0xFEF1, sa=0x21, data=bytes(8)),
            ],
        )
        table = codec._protocol_handler.get_address_table()
        assert table["count"] == 2
        # SA=0x00 has 2 unique PGNs
        sa00 = next(a for a in table["addresses"] if a["address"] == "0x00")
        assert sa00["pgn_count"] == 2
        # SA=0x21 has 1 PGN
        sa21 = next(a for a in table["addresses"] if a["address"] == "0x21")
        assert sa21["pgn_count"] == 1

    def test_j1939_dm1_decode_via_bam(self, j1939_config):
        """DM1 with active DTCs decoded after BAM reassembly."""
        # DM1 payload: lamp status + 2 DTCs
        dm1_payload = bytearray()
        dm1_payload.extend([0x04, 0x00])  # Amber warning lamp
        # DTC 1: SPN=110, FMI=0, OCC=3
        dm1_payload.extend([110, 0, (0 << 5) | 0, 3])
        # DTC 2: SPN=190, FMI=2, OCC=1
        dm1_payload.extend([190, 0, (0 << 5) | 2, 1])

        bam_msgs = _build_bam_sequence(pgn=0xFECA, sa=0x00, payload=bytes(dm1_payload))
        codec = _run_codec_with_messages(j1939_config, bam_msgs)

        diag = codec._protocol_handler.get_diagnostics()
        assert diag["count"] == 1
        src = diag["sources"][0]
        assert src["source_address"] == "0x00"
        assert src["dtc_count"] == 2
        assert src["dtcs"][0]["spn"] == 110
        assert src["dtcs"][0]["fmi"] == 0
        assert src["dtcs"][1]["spn"] == 190
        assert src["dtcs"][1]["fmi"] == 2

    def test_standard_11bit_frames_pass_through_j1939(self, j1939_config):
        """11-bit standard CAN frames bypass J1939 handler completely."""
        codec = _run_codec_with_messages(
            j1939_config,
            [
                can.Message(arbitration_id=0x100, data=bytes(8), is_extended_id=False),
            ],
        )
        # Not extended → handler returns False → DBC path (no DBC = unknown)
        assert codec.metrics.unknown_messages == 1

    def test_j1939_options_customization(self):
        """J1939 options (timeout, diagnostics, tracking) are configurable."""
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "j1939": True,
            "j1939_options": {
                "track_source_addresses": False,
                "decode_diagnostics": False,
                "tp_timeout_ms": 5000,
            },
        }
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)

        handler = codec._protocol_handler
        assert handler._track_addresses is False
        assert handler._decode_diagnostics is False
        assert handler._tp.timeout_s == 5.0

    def test_tp_session_cleanup(self, j1939_config):
        """Stale TP sessions are cleaned up."""
        config = {**j1939_config, "j1939_options": {"tp_timeout_ms": 0}}
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)

        # Start a BAM but don't complete it
        cm_arb = (6 << 26) | (0xEC << 16) | (0xFF << 8) | 0x00
        cm_data = bytes([32, 14, 0, 2, 0xFF, 0xCA, 0xFE, 0x00])
        cm_msg = can.Message(arbitration_id=cm_arb, data=cm_data, is_extended_id=True)
        codec._handle_message(cm_msg)

        assert codec._protocol_handler._tp.active_session_count == 1
        codec._protocol_handler.cleanup()
        assert codec._protocol_handler._tp.active_session_count == 0


# ===========================================================================
# Phase 5: J1939 + DBC — PGN Lookup + Signal Decode
# ===========================================================================


class TestJ1939WithDBC:
    """Validate J1939 PGN-based DBC decode works alongside protocol handler."""

    @pytest.fixture
    def j1939_dbc_config(self):
        return {
            "interface": "virtual",
            "channel": "vcan0",
            "database_file": J1939_DBC,
            "j1939": True,
        }

    def test_pgn_lookup_table_built(self, j1939_dbc_config):
        """PGN lookup table is built from J1939 DBC entries."""
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(j1939_dbc_config)
        # test_j1939.dbc has EEC1 and CCVS — both extended frames
        assert len(codec.messages_by_id) == 2

    def test_eec1_decoded_from_any_sa(self, j1939_dbc_config):
        """EEC1 PGN decodes correctly regardless of source address."""
        # EEC1 in DBC has SA=0xFE baked in, but we send from SA=0x00
        # EngineSpeed at bytes 3-4: 0x2000 * 0.125 = 1024 rpm
        data = bytes([0, 0, 0, 0x00, 0x20, 0, 0, 0])
        codec = _run_codec_with_messages(
            j1939_dbc_config,
            [
                _build_j1939_frame(0xF004, sa=0x00, data=data),  # SA != DBC's 0xFE
                _build_j1939_frame(0xF004, sa=0x21, data=data),  # Yet another SA
                _build_j1939_frame(0xF004, sa=0xFE, data=data),  # Exact DBC SA
            ],
        )
        assert codec.metrics.messages_decoded == 3
        assert codec.metrics.unknown_messages == 0

    def test_ccvs_decoded(self, j1939_dbc_config):
        """CCVS (PDU2 broadcast) decodes correctly."""
        data = bytes([0, 0x00, 0x64, 0, 0, 0, 0, 0])
        codec = _run_codec_with_messages(
            j1939_dbc_config,
            [
                _build_j1939_frame(0xFEF1, sa=0x00, data=data),
            ],
        )
        assert codec.metrics.messages_decoded == 1

    def test_j1939_handler_and_dbc_both_fire(self, j1939_dbc_config):
        """J1939 handler emits PGN metadata AND DBC decode happens."""
        data = bytes([0, 0, 0, 0x00, 0x20, 0, 0, 0])  # EEC1
        codec = _run_codec_with_messages(
            j1939_dbc_config,
            [
                _build_j1939_frame(0xF004, sa=0x00, data=data),
            ],
        )
        # DBC decode happened
        assert codec.metrics.messages_decoded == 1
        # Address tracking happened
        table = codec._protocol_handler.get_address_table()
        assert table["count"] == 1

    def test_tp_reassembled_payload_decoded_via_dbc(self, j1939_dbc_config):
        """TP-reassembled payload is fed back through DBC decode."""
        # Build a BAM for a PGN that's in the DBC (use EEC1 PGN 0xF004)
        # This is contrived (EEC1 is normally single-frame) but tests the path
        data = bytes([0, 0, 0, 0x00, 0x20, 0, 0, 0])
        bam_msgs = _build_bam_sequence(pgn=0xF004, sa=0x00, payload=data)
        codec = _run_codec_with_messages(j1939_dbc_config, bam_msgs)
        # TP completed
        assert codec._protocol_handler._tp.completed_transfers == 1
        # Reassembled payload was decoded via DBC
        assert codec.metrics.messages_decoded >= 1

    def test_unknown_pgn_not_in_dbc(self, j1939_dbc_config):
        """J1939 frame with PGN not in DBC → protocol metadata emitted, DBC unknown."""
        codec = _run_codec_with_messages(
            j1939_dbc_config,
            [
                _build_j1939_frame(0xFEEE, sa=0x00, data=bytes(8)),  # ET1 - not in DBC
            ],
        )
        # Address tracked by handler
        assert codec._protocol_handler.get_address_table()["count"] == 1
        # But DBC decode didn't find it
        assert codec.metrics.unknown_messages == 1


# ===========================================================================
# Phase 6: Action Registration — Conditional on Protocol
# ===========================================================================


class TestActionRegistration:
    """Verify J1939 actions appear only when enabled, CANopen never appears."""

    def test_j1939_actions_register_when_enabled(self):
        """Importing j1939 actions module registers the @action decorators."""
        # The import itself should succeed without error
        import zelos_extension_can.actions.j1939 as j1939_mod

        # Verify the functions exist and are callable
        assert callable(j1939_mod.j1939_address_table)
        assert callable(j1939_mod.j1939_tp_sessions)
        assert callable(j1939_mod.j1939_diagnostics)

    def test_j1939_action_bus_dropdown_empty_when_no_codecs(self):
        """Bus dropdown returns empty list when no codecs registered."""
        # Clear any leftover state
        from zelos_extension_can.actions import registry
        from zelos_extension_can.actions.registry import j1939_buses

        saved = dict(registry._codecs)
        registry._codecs.clear()
        try:
            assert j1939_buses() == []
        finally:
            registry._codecs.update(saved)

    def test_j1939_action_bus_dropdown_filters_correctly(self):
        """Bus dropdown only returns buses with J1939 handler."""
        from zelos_extension_can.actions import registry
        from zelos_extension_can.protocols.j1939.handler import J1939Handler

        saved = dict(registry._codecs)
        registry._codecs.clear()
        try:
            # Mock a J1939 codec
            j1939_codec = MagicMock()
            j1939_codec._protocol_handler = MagicMock(spec=J1939Handler)
            registry.register("j1939_bus", j1939_codec)

            # Mock a non-J1939 codec
            plain_codec = MagicMock()
            plain_codec._protocol_handler = None
            registry.register("plain_bus", plain_codec)

            buses = registry.j1939_buses()
            assert buses == ["j1939_bus"]
            assert "plain_bus" not in buses
        finally:
            registry._codecs.clear()
            registry._codecs.update(saved)

    def test_j1939_action_returns_error_for_missing_bus(self):
        """J1939 action returns error dict when bus not found."""
        from zelos_extension_can.actions import registry
        from zelos_extension_can.actions.j1939 import j1939_address_table

        saved = dict(registry._codecs)
        registry._codecs.clear()
        try:
            result = j1939_address_table("nonexistent")
            assert "error" in result
        finally:
            registry._codecs.update(saved)

    def test_bus_messages_dropdown(self):
        """bus_messages() returns DBC message names for a bus."""
        from zelos_extension_can.actions import registry

        saved = dict(registry._codecs)
        registry._codecs.clear()
        try:
            with patch("zelos_sdk.TraceSource"):
                codec = CanCodec(
                    {
                        "interface": "virtual",
                        "channel": "vcan0",
                        "database_file": TEST_DBC,
                    }
                )
            registry.register("test_bus", codec)

            messages = registry.bus_messages("test_bus")
            assert "DUT_Status" in messages
            assert len(messages) > 0

            # Non-existent bus returns empty
            assert registry.bus_messages("no_such_bus") == []
        finally:
            registry._codecs.clear()
            registry._codecs.update(saved)

    def test_bus_signals_dropdown(self):
        """bus_signals() returns signal names for a message on a bus."""
        from zelos_extension_can.actions import registry

        saved = dict(registry._codecs)
        registry._codecs.clear()
        try:
            with patch("zelos_sdk.TraceSource"):
                codec = CanCodec(
                    {
                        "interface": "virtual",
                        "channel": "vcan0",
                        "database_file": TEST_DBC,
                    }
                )
            registry.register("test_bus", codec)

            signals = registry.bus_signals("test_bus", "DUT_Status")
            assert "state" in signals
            assert "float_signal" in signals
            assert len(signals) > 0

            # Non-existent message returns empty
            assert registry.bus_signals("test_bus", "NoSuchMessage") == []
        finally:
            registry._codecs.clear()
            registry._codecs.update(saved)


# ===========================================================================
# Phase 7: CANopen Fully Hidden
# ===========================================================================


class TestCANopenHidden:
    """Verify CANopen is not exposed to users."""

    def test_no_canopen_actions_on_codec(self):
        """CanCodec class has no canopen action methods."""
        canopen_methods = [
            m for m in dir(CanCodec) if "canopen" in m.lower() and not m.startswith("_")
        ]
        assert canopen_methods == [], f"CANopen methods found on CanCodec: {canopen_methods}"

    def test_no_canopen_in_config_schema(self):
        """Config schema does not mention canopen."""
        schema_path = Path(__file__).parent.parent / "config.schema.json"
        schema_text = schema_path.read_text()
        assert "canopen" not in schema_text.lower()

    def test_no_canopen_in_extension_toml_keywords(self):
        """extension.toml keywords don't include canopen."""
        toml_path = Path(__file__).parent.parent / "extension.toml"
        toml_text = toml_path.read_text()
        # Check the keywords line specifically
        for line in toml_text.splitlines():
            if line.startswith("keywords"):
                assert "canopen" not in line.lower()

    def test_no_canopen_pip_dependency(self):
        """pyproject.toml doesn't list canopen as a dependency."""
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        pyproject_text = pyproject_path.read_text()
        # Check the dependencies section (not dev or optional)
        in_deps = False
        for line in pyproject_text.splitlines():
            if line.strip().startswith("dependencies"):
                in_deps = True
            elif in_deps and line.strip().startswith("]"):
                break
            elif in_deps:
                assert "canopen" not in line.lower(), f"canopen found in dependencies: {line}"

    def test_canopen_handler_not_created_without_config(self):
        """CANopen handler is not created unless config explicitly enables it."""
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(
                {
                    "interface": "virtual",
                    "channel": "vcan0",
                    "database_file": TEST_DBC,
                }
            )
        assert codec._protocol_handler is None

        with patch("zelos_sdk.TraceSource"):
            codec_j = CanCodec(
                {
                    "interface": "virtual",
                    "channel": "vcan0",
                    "j1939": True,
                }
            )
        from zelos_extension_can.protocols.j1939.handler import J1939Handler

        assert isinstance(codec_j._protocol_handler, J1939Handler)


# ===========================================================================
# Phase 8: Multi-Bus Configuration
# ===========================================================================


class TestMultiBus:
    """Validate multi-bus setups with mixed protocols."""

    def test_multi_bus_mixed_protocols(self):
        """One standard bus + one J1939 bus work independently."""
        from zelos_extension_can.protocols.j1939.handler import J1939Handler

        with patch("zelos_sdk.TraceSource"):
            standard = CanCodec(
                {
                    "interface": "virtual",
                    "channel": "vcan0",
                    "database_file": TEST_DBC,
                },
                bus_name="standard",
            )

            j1939 = CanCodec(
                {
                    "interface": "virtual",
                    "channel": "vcan1",
                    "database_file": J1939_DBC,
                    "j1939": True,
                },
                bus_name="j1939",
            )

        # Standard bus: no protocol handler
        assert standard._protocol_handler is None
        # J1939 bus: handler active
        assert isinstance(j1939._protocol_handler, J1939Handler)

        # Feed standard CAN to standard bus
        standard._handle_message(can.Message(arbitration_id=0x64, data=bytes(8), timestamp=1.0))
        assert standard.metrics.messages_decoded == 1

        # Feed J1939 to J1939 bus
        j1939._handle_message(_build_j1939_frame(0xF004, sa=0x00, data=bytes(8)))
        assert j1939.metrics.messages_decoded == 1

        # Cross-contamination check: J1939 frame on standard bus
        standard._handle_message(
            can.Message(arbitration_id=0x18FEF100, data=bytes(8), is_extended_id=True)
        )
        assert standard.metrics.unknown_messages == 1  # Not in standard DBC

    def test_create_codecs_assigns_names(self):
        """_create_codecs properly assigns bus names."""
        from zelos_extension_can.cli.app import _create_codecs

        config = {
            "buses": [
                {
                    "name": "powertrain",
                    "interface": "virtual",
                    "channel": "vcan0",
                    "database_file": TEST_DBC,
                },
                {
                    "name": "chassis",
                    "interface": "virtual",
                    "channel": "vcan1",
                    "database_file": TEST_DBC,
                },
            ]
        }
        with patch("zelos_sdk.TraceSource"):
            codecs = _create_codecs(config, Path(TEST_DBC))

        assert len(codecs) == 2
        assert codecs[0][0].bus_name == "powertrain"
        assert codecs[0][1] == "powertrain"
        assert codecs[1][0].bus_name == "chassis"
        assert codecs[1][1] == "chassis"

    def test_create_codecs_with_j1939_bus(self):
        """_create_codecs correctly creates J1939-enabled bus."""
        from zelos_extension_can.cli.app import _create_codecs
        from zelos_extension_can.protocols.j1939.handler import J1939Handler

        config = {
            "buses": [
                {
                    "name": "truck",
                    "interface": "virtual",
                    "channel": "vcan0",
                    "database_file": J1939_DBC,
                    "j1939": True,
                },
            ]
        }
        with patch("zelos_sdk.TraceSource"):
            codecs = _create_codecs(config, Path(TEST_DBC))

        assert len(codecs) == 1
        codec, name = codecs[0]
        assert name == "truck"
        assert isinstance(codec._protocol_handler, J1939Handler)


# ===========================================================================
# Phase 8b: Transmit DBC Message — full encode+send path
# ===========================================================================


class TestTransmitDBCMessage:
    """Validate the new Transmit DBC Message action end-to-end."""

    BUS = "_test_tx_dbc"

    @pytest.fixture(autouse=True)
    def _setup(self):
        from zelos_extension_can.actions import registry

        with patch("zelos_sdk.TraceSource"):
            self.codec = CanCodec(
                {
                    "interface": "virtual",
                    "channel": "vcan0",
                    "database_file": TEST_DBC,
                }
            )
        # Give it a mock bus so we can capture sends
        self.codec.bus = MagicMock()
        self.codec.running = True
        registry.register(self.BUS, self.codec)
        yield
        registry._codecs.pop(self.BUS, None)

    def test_encode_and_send_happy_path(self):
        """Valid message + signals → cantools encode → bus.send called."""
        from zelos_extension_can.actions.transmit import transmit_dbc_message

        # cantools encode requires all signals for the message
        all_signals = {s.name: 0 for s in self.codec.messages_by_name["DUT_Status"].signals}
        all_signals["state"] = 1
        import json

        result = transmit_dbc_message(self.BUS, "DUT_Status", json.dumps(all_signals))
        assert result["status"] == "sent"
        assert result["message"] == "DUT_Status"
        assert "data" in result
        # bus.send was called exactly once with a can.Message
        self.codec.bus.send.assert_called_once()
        sent_msg = self.codec.bus.send.call_args[0][0]
        assert sent_msg.arbitration_id == 0x64  # DUT_Status frame_id
        assert len(sent_msg.data) == 8

    def test_encode_failure_returns_error(self):
        """Signal name not in DBC → encode raises → clean error returned."""
        from zelos_extension_can.actions.transmit import transmit_dbc_message

        result = transmit_dbc_message(self.BUS, "DUT_Status", '{"totally_bogus_signal": 999}')
        assert "error" in result
        assert "Encode failed" in result["error"] or "error" in result

    def test_send_uses_extended_id_from_dbc(self):
        """Extended frame flag comes from DBC definition, not hardcoded."""
        import json

        from zelos_extension_can.actions.transmit import transmit_dbc_message

        # DUT_Status is a standard (11-bit) frame — provide all signals
        all_signals = {s.name: 0 for s in self.codec.messages_by_name["DUT_Status"].signals}
        result = transmit_dbc_message(self.BUS, "DUT_Status", json.dumps(all_signals))
        assert result["status"] == "sent"
        sent_msg = self.codec.bus.send.call_args[0][0]
        assert sent_msg.is_extended_id is False

    def test_multi_bus_routing(self):
        """Action targets correct bus — sending on bus A doesn't touch bus B."""
        from zelos_extension_can.actions import registry
        from zelos_extension_can.actions.transmit import send_message

        bus_b = "_test_tx_bus_b"
        with patch("zelos_sdk.TraceSource"):
            codec_b = CanCodec(
                {
                    "interface": "virtual",
                    "channel": "vcan1",
                    "database_file": TEST_DBC,
                }
            )
        codec_b.bus = MagicMock()
        codec_b.running = True
        registry.register(bus_b, codec_b)

        try:
            # Send on bus A
            send_message(self.BUS, 0x100, "01 02")
            self.codec.bus.send.assert_called_once()
            codec_b.bus.send.assert_not_called()

            # Send on bus B
            send_message(bus_b, 0x200, "03 04")
            codec_b.bus.send.assert_called_once()
            # bus A still only has 1 call
            assert self.codec.bus.send.call_count == 1
        finally:
            registry._codecs.pop(bus_b, None)


# ===========================================================================
# Phase 9: Extension CLI — Convert Command
# ===========================================================================


class TestConvertCommand:
    """Validate trace conversion works end-to-end."""

    def test_convert_action_rejects_missing_input(self):
        """Convert action returns error for missing input file."""
        from zelos_extension_can.actions import registry
        from zelos_extension_can.actions.conversion import convert_trace_file

        bus = "_test_convert"
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(
                {
                    "interface": "virtual",
                    "channel": "vcan0",
                    "database_file": TEST_DBC,
                }
            )
        registry.register(bus, codec)
        try:
            result = convert_trace_file(bus, "/nonexistent/file.asc")
            assert result["status"] == "error"
            assert "not found" in result["message"]
        finally:
            registry._codecs.pop(bus, None)

    def test_convert_action_rejects_missing_database(self):
        """Convert action returns error when no database configured."""
        from zelos_extension_can.actions import registry
        from zelos_extension_can.actions.conversion import convert_trace_file

        bus = "_test_convert_nodb"
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(
                {
                    "interface": "virtual",
                    "channel": "vcan0",
                    "j1939": True,  # No database_file
                }
            )
        registry.register(bus, codec)
        try:
            result = convert_trace_file(bus, "/tmp/test.asc")
            assert result["status"] == "error"
            assert (
                "database" in result["message"].lower() or "not found" in result["message"].lower()
            )
        finally:
            registry._codecs.pop(bus, None)


# ===========================================================================
# Async E2E: Full Demo Lifecycle
# ===========================================================================


class TestFullLifecycle:
    """Test start → run → stop lifecycle with virtual bus."""

    def test_ev_demo_lifecycle(self):
        """EV demo starts, runs briefly, stops cleanly."""
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "database_file": TEST_DBC,
            "demo_mode": True,
            "receive_own_messages": True,
            "log_raw_frames": True,
        }
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)

        codec.start()
        assert codec.bus is not None
        assert codec.running is True

        # Feed a few messages to verify the pipeline works while running
        codec._handle_message(can.Message(arbitration_id=0x64, data=bytes(8), timestamp=1.0))
        assert codec.metrics.messages_decoded == 1

        codec.stop()
        assert codec.running is False

    def test_j1939_demo_lifecycle(self):
        """J1939 demo starts, runs briefly, stops cleanly."""
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "j1939": True,
            "demo_mode": True,
            "demo_type": "j1939",
            "receive_own_messages": True,
            "log_raw_frames": True,
        }
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)

        codec.start()
        assert codec.bus is not None
        assert codec.running is True
        assert codec._protocol_handler is not None

        # Feed J1939 frame while running
        codec._handle_message(_build_j1939_frame(0xFEF1, sa=0x00, data=bytes(8)))

        codec.stop()
        assert codec.running is False

    def test_j1939_demo_async_run(self):
        """J1939 demo async loop starts simulation and receives frames."""
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "j1939": True,
            "demo_mode": True,
            "demo_type": "j1939",
            "receive_own_messages": True,
            "log_raw_frames": True,
        }
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)

        codec.start()

        async def run_briefly():
            task = asyncio.create_task(codec._run_async())
            # Let the demo simulation run for 2 seconds
            await asyncio.sleep(2.0)
            codec.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(run_briefly())

        # After 2 seconds of J1939 demo, we should have received frames
        assert codec.metrics.messages_received > 0
        # J1939 handler should have tracked addresses
        table = codec._protocol_handler.get_address_table()
        assert table["count"] >= 1

    def test_shutdown_bus_closed_in_finally(self):
        """Bus is shut down in _run_async's finally block, not in stop()."""
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "database_file": TEST_DBC,
        }
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)

        codec.start()

        async def run_and_stop():
            task = asyncio.create_task(codec._run_async())
            await asyncio.sleep(0.2)
            codec.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(run_and_stop())

        # Bus should be None after _run_async's finally block
        assert codec.bus is None

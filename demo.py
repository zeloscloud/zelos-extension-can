#!/usr/bin/env python3
"""Demo: Full end-to-end CAN extension with continuous DBC replay mode."""

import asyncio
import contextlib
import logging

import can
import cantools
import cantools.database.can.database
import zelos_sdk
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_can.can_codec import CanCodec
from zelos_extension_can.utils.config import load_config

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

# Shared event to signal when extension is ready
extension_ready = asyncio.Event()


async def replay_can_traffic():
    """Continuously replay CAN traffic from demo.dbc with extended IDs and CAN-FD examples."""
    # Wait for extension to be fully ready
    await extension_ready.wait()
    await asyncio.sleep(0.5)

    db: cantools.database.can.database.Database = cantools.database.load_file("assets/demo.dbc")  # type: ignore[assignment]
    bus = can.Bus(interface="virtual", channel="vcan0", receive_own_messages=False)

    print("\n" + "=" * 70)
    print("CAN TRAFFIC REPLAY MODE")
    print("=" * 70)
    print("Continuously sending CAN messages (Ctrl+C to stop)")
    print("=" * 70 + "\n")

    iteration = 0
    try:
        while True:
            # DUT_Status
            msg_def = db.get_message_by_name("DUT_Status")
            data = msg_def.encode(
                {
                    "state": (iteration % 3),
                    "safety_pin_state": 1,
                    "enable_line_state": 1,
                    "duplicate_signal": 0,
                    "multibit_signal": iteration % 4,
                    "signed_signal": (iteration % 4) - 2,
                    "float_signal": 20.0 + (iteration % 10) * 5,
                    "small_float_signal": 0.5 + (iteration % 10) * 0.1,
                    "SOC_signal": 50.0 + (iteration % 10) * 5,
                }
            )
            msg = can.Message(arbitration_id=0x64, data=data)
            bus.send(msg)

            # DUT_Command
            msg_def = db.get_message_by_name("DUT_Command")
            data = msg_def.encode({"state_request": iteration % 2})
            msg = can.Message(arbitration_id=0xC8, data=data)
            bus.send(msg)

            # DUT_Logging with different mux values
            msg_def = db.get_message_by_name("DUT_Logging")
            mux = iteration % 3
            if mux == 0:
                data = msg_def.encode(
                    {
                        "logging_mux": 0,
                        "logging_signal0": 1,
                        "no_mux_logging_signal": 1,
                    }
                )
            elif mux == 1:
                data = msg_def.encode(
                    {
                        "logging_mux": 1,
                        "logging_signal1": 1,
                        "no_mux_logging_signal": 0,
                    }
                )
            else:
                data = msg_def.encode(
                    {
                        "logging_mux": 2,
                        "logging_signal2": 1,
                        "no_mux_logging_signal": 1,
                    }
                )
            msg = can.Message(arbitration_id=0x12C, data=data)
            bus.send(msg)

            # J1939_EngineSpeed (29-bit Extended ID)
            msg_def = db.get_message_by_name("J1939_EngineSpeed")
            data = msg_def.encode(
                {
                    "engine_speed": 800 + (iteration % 20) * 50,  # RPM
                    "engine_torque": (iteration % 50) - 25,  # %
                    "driver_demand": (iteration % 100) - 50,  # %
                    "actual_torque": (iteration % 40) - 20,  # %
                }
            )
            msg = can.Message(arbitration_id=0x18EFF000, data=data, is_extended_id=True)
            bus.send(msg)

            # CANopen_TPDO1 (29-bit Extended ID)
            msg_def = db.get_message_by_name("CANopen_TPDO1")
            data = msg_def.encode(
                {
                    "sensor_value_1": 12.0 + (iteration % 10) * 0.5,  # V
                    "sensor_value_2": 5.0 + (iteration % 10) * 0.2,  # A
                    "status_bits": iteration % 0xFFFF,
                    "timestamp": (iteration * 100) % 65535,  # ms
                }
            )
            msg = can.Message(arbitration_id=0x18FF1234, data=data, is_extended_id=True)
            bus.send(msg)

            # CANFD_BulkData (29-bit Extended ID, 64 bytes - CAN-FD)
            # Note: Virtual bus may not support CAN-FD, but message is defined
            msg_def = db.get_message_by_name("CANFD_BulkData")
            data = msg_def.encode(
                {
                    "data_counter": iteration,
                    "payload_byte_0": iteration % 256,
                    "payload_byte_1": (iteration * 2) % 256,
                    "payload_byte_2": (iteration * 3) % 256,
                    "checksum": (iteration * 4) % 256,
                }
            )
            msg = can.Message(arbitration_id=0x1FFFFF00, data=data, is_extended_id=True)
            try:
                bus.send(msg)
            except Exception:
                if iteration == 0:  # Only warn once
                    print("  Note: CAN-FD message sending may not be supported on virtual bus")

            # CANFD_HighSpeed (29-bit Extended ID, 32 bytes - CAN-FD)
            msg_def = db.get_message_by_name("CANFD_HighSpeed")
            data = msg_def.encode(
                {
                    "sequence_number": iteration % 65536,
                    "temperature_1": 25.0 + (iteration % 10) * 2,  # degC
                    "temperature_2": 30.0 + (iteration % 10) * 3,  # degC
                    "pressure_1": 1.0 + (iteration % 10) * 0.1,  # bar
                    "pressure_2": 2.0 + (iteration % 10) * 0.2,  # bar
                    "status_flags": iteration % 0xFFFFFFFF,
                }
            )
            msg = can.Message(arbitration_id=0x1FFFFFE0, data=data, is_extended_id=True)
            with contextlib.suppress(Exception):
                bus.send(msg)

            if iteration % 10 == 0:
                print(
                    f"  Iteration {iteration}: Sent 7 messages (3x STD, 4x EXT including 2x CAN-FD)"
                )

            iteration += 1
            await asyncio.sleep(0.1)  # 10Hz replay rate

    except asyncio.CancelledError:
        print("\n" + "=" * 70)
        print(f"Replay stopped after {iteration} iterations")
        print("=" * 70)
        bus.shutdown()
        raise


async def run_extension():
    """Run the CAN extension with full SDK initialization."""
    logger = logging.getLogger(__name__)

    # Initialize SDK
    logger.info("Initializing zelos_sdk...")
    zelos_sdk.init(name="zelos_extension_can", actions=True)

    # Add trace logging handler
    handler = TraceLoggingHandler("zelos_extension_can_log")
    logging.getLogger().addHandler(handler)

    # Load configuration and override dbc_file to use demo.dbc
    config = load_config()
    config["dbc_file"] = "assets/demo.dbc"

    # Create CAN codec
    logger.info("Creating CanCodec...")
    codec = CanCodec(config)

    # Register actions
    zelos_sdk.actions_registry.register(codec)
    logger.info("Actions registered")

    # Start CAN bus
    codec.start()

    # Signal that extension is ready
    extension_ready.set()
    logger.info("Extension ready, starting message reception...")

    # Run async message reception
    try:
        await codec._run_async()
    except asyncio.CancelledError:
        logger.info("Extension cancelled, cleaning up...")
        codec.stop()
        raise


async def main():
    """Run extension with continuous CAN traffic replay."""
    print("=" * 70)
    print("CAN EXTENSION DEMO - CONTINUOUS REPLAY MODE")
    print("=" * 70)
    print("\nThis demo:")
    print("  ✓ Initializes SDK and registers actions")
    print("  ✓ Loads demo.dbc with extended IDs and CAN-FD messages")
    print("  ✓ Continuously replays CAN traffic at 10Hz")
    print("  ✓ Sends 3x standard (11-bit) messages")
    print("  ✓ Sends 4x extended (29-bit) messages including CAN-FD")
    print("  ✓ Decodes all messages and emits to trace")
    print("\nPress Ctrl+C to stop")
    print("=" * 70 + "\n")

    # Run both tasks
    extension_task = asyncio.create_task(run_extension())
    replay_task = asyncio.create_task(replay_can_traffic())

    # Run until interrupted
    try:
        await asyncio.gather(extension_task, replay_task)
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        replay_task.cancel()
        extension_task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(replay_task, extension_task, return_exceptions=True)

    print("\nDemo complete!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")

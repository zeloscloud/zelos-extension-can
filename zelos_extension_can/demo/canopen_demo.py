"""CANopen CiA 402 servo drive simulator for demo mode.

Simulates a servo drive on node ID 1 with heartbeat, TPDOs,
occasional emergencies, and NMT boot sequence.
"""

import asyncio
import logging
import math
import random
import struct
from typing import Any

import can

logger = logging.getLogger(__name__)


class ServoDriveSimulator:
    """CiA 402 servo drive simulator."""

    def __init__(self, node_id: int = 1):
        self.node_id = node_id
        self.position = 0  # encoder counts
        self.velocity = 0.0  # counts/s
        self.current = 0.0  # mA
        self.statusword = 0x0237  # Operation enabled
        self.temperature = 35.0  # C
        self.uptime = 0.0
        self.target_position = 0
        self._fault_timer = 0.0

    def update(self, dt: float):
        self.uptime += dt

        # Simulate sinusoidal motion profile
        freq = 0.2  # Hz
        amplitude = 50000  # counts
        self.target_position = int(amplitude * math.sin(2 * math.pi * freq * self.uptime))

        # Position tracking with lag
        error = self.target_position - self.position
        self.velocity = error * 5.0  # P-controller
        self.position += int(self.velocity * dt)

        # Current proportional to velocity change
        self.current = abs(self.velocity) * 0.01 + random.uniform(-50, 50)
        self.current = max(0, min(5000, self.current))

        # Temperature drift
        load = abs(self.current) / 5000.0
        self.temperature += (25.0 + load * 30 - self.temperature) * 0.005

        # Fault simulation
        self._fault_timer += dt


async def run_demo_canopen_simulation(
    bus: can.Bus,
    running_flag: Any,
) -> None:
    """Run CANopen servo drive simulation.

    :param bus: CAN bus instance
    :param running_flag: Object with 'running' attribute
    """
    try:
        logger.info("Starting CANopen servo drive simulation")
        sim = ServoDriveSimulator(node_id=1)
        node_id = sim.node_id

        dt = 0.05  # 50ms
        iteration = 0

        # NMT boot sequence
        logger.info("CANopen: Sending boot-up heartbeat for node %d", node_id)
        # Boot-up heartbeat (state = 0x00)
        boot_msg = can.Message(
            arbitration_id=0x700 + node_id,
            data=bytes([0x00]),
            is_extended_id=False,
        )
        bus.send(boot_msg)
        await asyncio.sleep(0.1)

        # NMT Start command (broadcast)
        nmt_start = can.Message(
            arbitration_id=0x000,
            data=bytes([0x01, node_id]),
            is_extended_id=False,
        )
        bus.send(nmt_start)
        await asyncio.sleep(0.1)

        while running_flag.running:
            sim.update(dt)

            try:
                # Heartbeat (0x700 + node_id, 500ms)
                if iteration % 10 == 0:
                    hb_msg = can.Message(
                        arbitration_id=0x700 + node_id,
                        data=bytes([0x05]),  # OPERATIONAL
                        is_extended_id=False,
                    )
                    bus.send(hb_msg)

                # TPDO1 (0x180 + node_id, 100ms): position + velocity
                if iteration % 2 == 0:
                    pos_bytes = struct.pack("<i", sim.position)
                    vel_bytes = struct.pack("<h", int(sim.velocity / 10))
                    tpdo1_data = pos_bytes + vel_bytes + bytes(2)
                    tpdo1_msg = can.Message(
                        arbitration_id=0x180 + node_id,
                        data=tpdo1_data,
                        is_extended_id=False,
                    )
                    bus.send(tpdo1_msg)

                # TPDO2 (0x280 + node_id, 100ms): current + statusword
                if iteration % 2 == 0:
                    cur_bytes = struct.pack("<H", int(sim.current))
                    sw_bytes = struct.pack("<H", sim.statusword)
                    temp_byte = int(max(0, min(255, sim.temperature)))
                    tpdo2_data = cur_bytes + sw_bytes + bytes([temp_byte, 0, 0, 0])
                    tpdo2_msg = can.Message(
                        arbitration_id=0x280 + node_id,
                        data=tpdo2_data,
                        is_extended_id=False,
                    )
                    bus.send(tpdo2_msg)

                # EMERGENCY (0x080 + node_id, occasional over-temperature)
                if sim._fault_timer > 45 and sim.temperature > 50:
                    emcy_data = struct.pack(
                        "<HB5s",
                        0x4210,  # Temperature error
                        0x04,  # Error register: temperature
                        bytes(5),
                    )
                    emcy_msg = can.Message(
                        arbitration_id=0x080 + node_id,
                        data=emcy_data,
                        is_extended_id=False,
                    )
                    bus.send(emcy_msg)
                    sim._fault_timer = 0
                    logger.info("CANopen: Over-temperature EMERGENCY from node %d", node_id)

                # SDO read (periodic parameter query, 0x600/0x580 + node_id)
                if iteration % 40 == 0:
                    # SDO upload initiate: read 0x6041 (statusword)
                    sdo_req = can.Message(
                        arbitration_id=0x600 + node_id,
                        data=bytes([0x40, 0x41, 0x60, 0x00, 0x00, 0x00, 0x00, 0x00]),
                        is_extended_id=False,
                    )
                    bus.send(sdo_req)
                    await asyncio.sleep(0.002)
                    # SDO upload response (expedited, 2 bytes)
                    sw_bytes = struct.pack("<H", sim.statusword)
                    sdo_resp = can.Message(
                        arbitration_id=0x580 + node_id,
                        data=bytes([0x4B, 0x41, 0x60, 0x00]) + sw_bytes + bytes(2),
                        is_extended_id=False,
                    )
                    bus.send(sdo_resp)

                if iteration % 20 == 0:
                    logger.info(
                        "CANopen Sim: Pos=%d Vel=%.0f Cur=%.0fmA Temp=%.1fC",
                        sim.position,
                        sim.velocity,
                        sim.current,
                        sim.temperature,
                    )

            except Exception as e:
                logger.error("Error in CANopen simulation: %s", e)

            iteration += 1
            await asyncio.sleep(dt)

    except asyncio.CancelledError:
        logger.info("CANopen simulation cancelled")
    except Exception as e:
        logger.exception("Error in CANopen simulation: %s", e)

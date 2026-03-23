"""J1939 heavy-duty truck simulator for demo mode.

Simulates a diesel truck broadcasting standard J1939 PGNs including
engine data, vehicle speed, fuel economy, temperatures, and diagnostics.
DM1 uses BAM transport protocol (multi-frame) to exercise TP reassembly.
"""

import asyncio
import logging
import random
from typing import Any

import can

logger = logging.getLogger(__name__)


class TruckSimulator:
    """Physics-based heavy-duty truck simulator."""

    def __init__(self):
        self.engine_speed = 800.0  # rpm (idle)
        self.engine_torque = 0.0  # % of reference torque
        self.driver_demand = 0.0  # %
        self.vehicle_speed = 0.0  # km/h
        self.fuel_rate = 2.0  # L/h (idle)
        self.fuel_economy = 0.0  # km/L
        self.coolant_temp = 75.0  # C
        self.oil_temp = 80.0  # C
        self.cruise_active = False
        self.brake_active = False
        self.uptime = 0.0

        # DTC state
        self.dtc_active = False
        self.dtc_timer = 0.0

    def update(self, dt: float):
        self.uptime += dt

        # Random driver behavior
        if random.random() < 0.005:
            self.driver_demand = random.uniform(0, 90)
        if random.random() < 0.005:
            self.brake_active = random.choice([True, False])
        if random.random() < 0.001:
            self.cruise_active = not self.cruise_active

        # Engine response
        if self.brake_active:
            self.driver_demand = 0
            self.vehicle_speed = max(0, self.vehicle_speed - 3 * dt)
            self.engine_torque = -10
        elif self.driver_demand > 0:
            target_speed = self.driver_demand * 1.2  # Max ~108 km/h
            self.vehicle_speed += (target_speed - self.vehicle_speed) * 0.5 * dt
            self.engine_torque = self.driver_demand * 0.8
        else:
            self.vehicle_speed *= 1 - 0.3 * dt
            self.engine_torque *= 0.9

        self.vehicle_speed = max(0, min(120, self.vehicle_speed))

        # Engine speed from vehicle speed + gear ratio approximation
        if self.vehicle_speed > 2:
            self.engine_speed = 600 + self.vehicle_speed * 15
        else:
            self.engine_speed = 800  # Idle

        self.engine_speed = max(600, min(2500, self.engine_speed))

        # Fuel consumption
        load_factor = max(0, self.engine_torque) / 100.0
        self.fuel_rate = 2.0 + load_factor * 40.0  # 2-42 L/h
        if self.vehicle_speed > 1:
            self.fuel_economy = self.vehicle_speed / self.fuel_rate
        else:
            self.fuel_economy = 0

        # Temperatures (slow response)
        ambient = 25.0
        heat_factor = (self.engine_speed / 2500.0) * (1 + load_factor)
        self.coolant_temp += (ambient + 60 * heat_factor - self.coolant_temp) * 0.01
        self.oil_temp += (ambient + 70 * heat_factor - self.oil_temp) * 0.008

        # Periodic DTC simulation
        self.dtc_timer += dt
        if self.dtc_timer > 30:
            self.dtc_active = not self.dtc_active
            self.dtc_timer = 0


def _build_j1939_msg(pgn: int, source_address: int, data: bytes, priority: int = 6) -> can.Message:
    """Build a J1939 CAN message from PGN, SA, and data."""
    from zelos_extension_can.protocols.j1939.pgn import build_arb_id

    return can.Message(
        arbitration_id=build_arb_id(pgn, source_address, priority),
        data=data,
        is_extended_id=True,
    )


def _build_bam_sequence(pgn: int, source_address: int, payload: bytes) -> list[can.Message]:
    """Build a BAM transport protocol sequence for a multi-frame message."""
    total_bytes = len(payload)
    total_packets = (total_bytes + 6) // 7  # 7 data bytes per TP.DT

    messages = []

    # TP.CM_BAM (PGN 0xEC00 -> broadcast to 0xFF)
    cm_data = bytes(
        [
            32,  # Control byte: BAM
            total_bytes & 0xFF,
            (total_bytes >> 8) & 0xFF,
            total_packets,
            0xFF,  # Reserved
            pgn & 0xFF,
            (pgn >> 8) & 0xFF,
            (pgn >> 16) & 0xFF,
        ]
    )
    cm_msg = _build_j1939_msg(0xECFF, source_address, cm_data)
    messages.append(cm_msg)

    # TP.DT frames (PGN 0xEB00 -> broadcast to 0xFF)
    for seq in range(1, total_packets + 1):
        start = (seq - 1) * 7
        chunk = payload[start : start + 7]
        # Pad last packet with 0xFF
        if len(chunk) < 7:
            chunk = chunk + bytes([0xFF] * (7 - len(chunk)))
        dt_data = bytes([seq]) + chunk
        dt_msg = _build_j1939_msg(0xEBFF, source_address, dt_data)
        messages.append(dt_msg)

    return messages


async def run_demo_j1939_simulation(
    bus: can.Bus,
    running_flag: Any,
) -> None:
    """Run J1939 truck simulation.

    :param bus: CAN bus instance
    :param running_flag: Object with 'running' attribute
    """
    try:
        logger.info("Starting J1939 truck simulation")
        sim = TruckSimulator()
        sa = 0x00  # Engine ECU source address

        dt = 0.05  # 50ms update rate
        iteration = 0

        while running_flag.running:
            sim.update(dt)

            try:
                # EEC1 - Electronic Engine Controller 1 (PGN 61444 / 0xF004, 100ms)
                if iteration % 2 == 0:
                    eec1_data = bytearray(8)
                    # Engine torque mode (byte 0, bits 0-3)
                    eec1_data[0] = 0x01  # Driver demand
                    # Driver demand torque (byte 1): offset -125%, 1%/bit
                    eec1_data[1] = int(max(0, min(250, sim.driver_demand + 125)))
                    # Actual engine torque (byte 2): offset -125%, 1%/bit
                    eec1_data[2] = int(max(0, min(250, sim.engine_torque + 125)))
                    # Engine speed (bytes 3-4): 0.125 rpm/bit
                    rpm_raw = int(sim.engine_speed / 0.125)
                    eec1_data[3] = rpm_raw & 0xFF
                    eec1_data[4] = (rpm_raw >> 8) & 0xFF
                    # Source address (byte 5)
                    eec1_data[5] = sa
                    msg = _build_j1939_msg(0xF004, sa, bytes(eec1_data))
                    bus.send(msg)

                # CCVS - Cruise Control/Vehicle Speed (PGN 65265 / 0xFEF1, 100ms)
                if iteration % 2 == 0:
                    ccvs_data = bytearray(8)
                    # Two-speed axle switch, parking brake, cruise (byte 0)
                    ccvs_data[0] = 0x00
                    # Vehicle speed (bytes 1-2): 1/256 km/h per bit
                    speed_raw = int(sim.vehicle_speed * 256)
                    ccvs_data[1] = speed_raw & 0xFF
                    ccvs_data[2] = (speed_raw >> 8) & 0xFF
                    # Cruise control states (byte 3)
                    ccvs_data[3] = 0x05 if sim.cruise_active else 0x00
                    # Brake switch (byte 4)
                    ccvs_data[4] = 0x01 if sim.brake_active else 0x00
                    msg = _build_j1939_msg(0xFEF1, sa, bytes(ccvs_data))
                    bus.send(msg)

                # LFE - Liquid Fuel Economy (PGN 65266 / 0xFEF2, 500ms)
                if iteration % 10 == 0:
                    lfe_data = bytearray(8)
                    # Fuel rate (bytes 0-1): 0.05 L/h per bit
                    fuel_raw = int(sim.fuel_rate / 0.05)
                    lfe_data[0] = fuel_raw & 0xFF
                    lfe_data[1] = (fuel_raw >> 8) & 0xFF
                    # Instantaneous fuel economy (bytes 2-3): 1/512 km/L per bit
                    econ_raw = int(sim.fuel_economy * 512)
                    lfe_data[2] = econ_raw & 0xFF
                    lfe_data[3] = (econ_raw >> 8) & 0xFF
                    msg = _build_j1939_msg(0xFEF2, sa, bytes(lfe_data))
                    bus.send(msg)

                # ET1 - Engine Temperature 1 (PGN 65262 / 0xFEEE, 1000ms)
                if iteration % 20 == 0:
                    et1_data = bytearray(8)
                    # Coolant temp (byte 0): offset -40C, 1C/bit
                    et1_data[0] = int(max(0, min(250, sim.coolant_temp + 40)))
                    # Fuel temp (byte 1)
                    et1_data[1] = int(max(0, min(250, sim.coolant_temp - 10 + 40)))
                    # Oil temp (bytes 2-3): offset -273C, 0.03125C/bit
                    oil_raw = int((sim.oil_temp + 273) / 0.03125)
                    et1_data[2] = oil_raw & 0xFF
                    et1_data[3] = (oil_raw >> 8) & 0xFF
                    msg = _build_j1939_msg(0xFEEE, sa, bytes(et1_data))
                    bus.send(msg)

                # DM1 - Active DTCs via BAM (PGN 65226 / 0xFECA, 1000ms)
                if iteration % 20 == 0:
                    dm1_payload = bytearray()
                    # Lamp status (2 bytes)
                    if sim.dtc_active:
                        dm1_payload.extend([0x04, 0x00])  # Amber warning lamp
                        # DTC 1: SPN 110 (Coolant Temp), FMI 0 (above normal)
                        spn = 110
                        fmi = 0
                        occ = 3
                        dm1_payload.append(spn & 0xFF)
                        dm1_payload.append((spn >> 8) & 0xFF)
                        dm1_payload.append(((spn >> 16) & 0x07) << 5 | (fmi & 0x1F))
                        dm1_payload.append(occ & 0x7F)
                        # DTC 2: SPN 190 (Engine Speed), FMI 2 (erratic)
                        spn2 = 190
                        fmi2 = 2
                        occ2 = 1
                        dm1_payload.append(spn2 & 0xFF)
                        dm1_payload.append((spn2 >> 8) & 0xFF)
                        dm1_payload.append(((spn2 >> 16) & 0x07) << 5 | (fmi2 & 0x1F))
                        dm1_payload.append(occ2 & 0x7F)
                    else:
                        dm1_payload.extend([0x00, 0x00])  # No lamps
                        # No-fault indicator
                        dm1_payload.extend([0xFF, 0xFF, 0xFF, 0xFF])

                    # DM1 with DTCs is >8 bytes, use BAM transport
                    if len(dm1_payload) > 8:
                        bam_msgs = _build_bam_sequence(0xFECA, sa, bytes(dm1_payload))
                        for bam_msg in bam_msgs:
                            bus.send(bam_msg)
                            await asyncio.sleep(0.005)  # 5ms between TP.DT per J1939-21
                    else:
                        msg = _build_j1939_msg(0xFECA, sa, bytes(dm1_payload))
                        bus.send(msg)

                # Log status
                if iteration % 20 == 0:
                    logger.info(
                        "J1939 Sim: RPM=%.0f Speed=%.1fkm/h Torque=%.1f%% "
                        "Fuel=%.1fL/h Coolant=%.0fC DTCs=%s",
                        sim.engine_speed,
                        sim.vehicle_speed,
                        sim.engine_torque,
                        sim.fuel_rate,
                        sim.coolant_temp,
                        "ACTIVE" if sim.dtc_active else "none",
                    )

            except Exception as e:
                logger.error("Error in J1939 simulation: %s", e)

            iteration += 1
            await asyncio.sleep(dt)

    except asyncio.CancelledError:
        logger.info("J1939 simulation cancelled")
    except Exception as e:
        logger.exception("Error in J1939 simulation: %s", e)

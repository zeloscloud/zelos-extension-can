"""EV simulator and demo mode functionality."""

import asyncio
import logging
import random
from typing import Any

import can
import cantools

logger = logging.getLogger(__name__)


class EVSimulator:
    """Physics-based electric vehicle simulator for demo mode."""

    def __init__(self):
        """Initialize EV state."""
        # Vehicle state
        self.soc = 85.0  # State of charge %
        self.speed = 0.0  # km/h
        self.accel_pedal = 0.0  # %
        self.brake_pedal = False
        self.charging = False

        # Battery state
        self.pack_voltage = 400.0  # V
        self.pack_current = 0.0  # A
        self.pack_temp = 25.0  # C
        self.cell_voltages = [3.7] * 5  # V

        # Motor state
        self.motor_speed = 0  # rpm
        self.motor_torque = 0.0  # Nm
        self.motor_temp = 30.0  # C
        self.motor_state = 1  # IDLE

        # Time tracking
        self.uptime = 0

    def update(self, dt: float):
        """Update simulation state with realistic physics."""
        self.uptime += dt

        # Simulate driving behavior
        if not self.charging:
            # Random acceleration/braking
            if random.random() < 0.01:
                self.accel_pedal = random.uniform(0, 80)
            if random.random() < 0.01:
                self.brake_pedal = random.choice([True, False])

            # Update speed based on pedals
            if self.brake_pedal:
                self.speed = max(0, self.speed - 5)
                self.motor_torque = -50  # Regen braking
            elif self.accel_pedal > 0:
                self.speed = min(120, self.speed + self.accel_pedal * 0.1)
                self.motor_torque = self.accel_pedal * 3  # ~240 Nm max
            else:
                self.speed *= 0.98  # Coast down
                self.motor_torque *= 0.9

            # Motor speed from vehicle speed (approximation, limit to 10000 RPM max)
            self.motor_speed = min(10000, int(self.speed * 100))  # Rough gear ratio

            # Update motor state
            if self.speed > 1:
                self.motor_state = 3 if self.motor_torque > 0 else 4  # RUNNING or REGEN
            else:
                self.motor_state = 1  # IDLE

            # Power consumption/regen
            power_kw = (self.motor_torque * self.motor_speed / 9550) / 1000  # kW
            self.pack_current = (
                power_kw / (self.pack_voltage / 1000) if self.pack_voltage > 0 else 0
            )

            # Update SOC
            energy_used = self.pack_current * dt / 3600  # Ah
            capacity_ah = 75.0  # 75Ah battery
            self.soc -= (energy_used / capacity_ah) * 100
            self.soc = max(0, min(100, self.soc))

        else:
            # Charging mode
            self.speed = 0
            self.motor_speed = 0
            self.motor_torque = 0
            self.motor_state = 0  # OFF
            self.pack_current = -30.0  # Charging at 30A
            self.soc = min(100, self.soc + 0.02)  # Charge up

        # Update temperatures (slow changes)
        ambient = 20.0
        motor_load = abs(self.motor_torque) / 240.0
        self.motor_temp += (ambient + motor_load * 50 - self.motor_temp) * 0.01
        self.pack_temp += (ambient + abs(self.pack_current) * 0.5 - self.pack_temp) * 0.01

        # Cell voltages vary slightly around nominal (limit to 4.0V max for 12-bit encoding)
        base_voltage = 3.3 + (self.soc / 100) * 0.7  # 3.3V to 4.0V
        self.cell_voltages = [
            min(4.0, base_voltage + random.uniform(-0.05, 0.05)) for _ in range(5)
        ]

        # Pack voltage from cells
        self.pack_voltage = sum(self.cell_voltages) * 20  # 100 cells total (5 measured)


async def run_demo_ev_simulation(
    bus: can.Bus,
    db: cantools.database.can.Database,
    running_flag: Any,
) -> None:
    """Run physics-based EV simulation and transmit CAN messages.

    :param bus: CAN bus instance to send messages on
    :param db: CAN database with message definitions
    :param running_flag: Object with 'running' attribute to check if simulation should continue
    """
    try:
        logger.info("Starting EV simulation in demo mode")
        sim = EVSimulator()

        dt = 0.05  # 50ms update rate
        iteration = 0

        while running_flag.running:
            # Update physics
            sim.update(dt)

            try:
                # BMS_BatteryStatus (100ms / iteration 2)
                if iteration % 2 == 0:
                    msg_def = db.get_message_by_name("BMS_BatteryStatus")
                    data = msg_def.encode(
                        {
                            "pack_voltage": sim.pack_voltage,
                            "pack_current": sim.pack_current,
                            "state_of_charge": int(sim.soc),
                            "pack_temperature": int(sim.pack_temp),
                            "max_cell_voltage": max(sim.cell_voltages),
                            "min_cell_voltage": min(sim.cell_voltages),
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # BMS_CellVoltages (1000ms / iteration 20)
                if iteration % 20 == 0:
                    msg_def = db.get_message_by_name("BMS_CellVoltages")
                    data = msg_def.encode(
                        {
                            "cell_01_voltage": sim.cell_voltages[0],
                            "cell_02_voltage": sim.cell_voltages[1],
                            "cell_03_voltage": sim.cell_voltages[2],
                            "cell_04_voltage": sim.cell_voltages[3],
                            "cell_05_voltage": sim.cell_voltages[4],
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # BMS_Temperatures (500ms / iteration 10)
                if iteration % 10 == 0:
                    msg_def = db.get_message_by_name("BMS_Temperatures")
                    data = msg_def.encode(
                        {
                            "module_01_temp": int(sim.pack_temp),
                            "module_02_temp": int(sim.pack_temp + 2),
                            "module_03_temp": int(sim.pack_temp - 1),
                            "module_04_temp": int(sim.pack_temp + 1),
                            "coolant_inlet_temp": int(sim.pack_temp - 5),
                            "coolant_outlet_temp": int(sim.pack_temp + 3),
                            "ambient_temp": 20,
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # BMS_Limits (200ms / iteration 4)
                if iteration % 4 == 0:
                    msg_def = db.get_message_by_name("BMS_Limits")
                    data = msg_def.encode(
                        {
                            "max_charge_current": 200.0,
                            "max_discharge_current": 400.0,
                            "max_charge_power": 100.0,
                            "max_discharge_power": 200.0,
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # BMS_Status (100ms / iteration 2)
                if iteration % 2 == 0:
                    msg_def = db.get_message_by_name("BMS_Status")
                    data = msg_def.encode(
                        {
                            "bms_state": 3,  # READY
                            "contactor_state": 2,  # CLOSED
                            "balancing_active": 0,
                            "charging_enabled": 1,
                            "isolation_resistance": 5000,
                            "fault_code": 0,
                            "warning_code": 0,
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # Motor_Status (50ms / iteration 1)
                msg_def = db.get_message_by_name("Motor_Status")
                data = msg_def.encode(
                    {
                        "motor_speed": sim.motor_speed,
                        "motor_torque": sim.motor_torque,
                        "motor_temperature": int(sim.motor_temp),
                        "inverter_temperature": int(sim.motor_temp - 10),
                        "motor_state": sim.motor_state,
                        "fault_active": 0,
                        "torque_limit_active": 0,
                    }
                )
                bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # Motor_Power (100ms / iteration 2)
                if iteration % 2 == 0:
                    msg_def = db.get_message_by_name("Motor_Power")
                    power_output = (sim.motor_torque * sim.motor_speed / 9550) / 1000
                    data = msg_def.encode(
                        {
                            "dc_voltage": sim.pack_voltage,
                            "dc_current": sim.pack_current,
                            "ac_current_rms": abs(sim.pack_current) * 0.8,
                            "power_output": power_output,
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # Motor_Command (20ms but just use same values / iteration 1 with less frequency)
                if iteration % 2 == 0:
                    msg_def = db.get_message_by_name("Motor_Command")
                    data = msg_def.encode(
                        {
                            "torque_request": sim.motor_torque,
                            "speed_limit": 10000,
                            "direction": 1,  # FORWARD
                            "enable": 1 if sim.speed > 0 else 0,
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # Gateway_VehicleSpeed (100ms / iteration 2)
                if iteration % 2 == 0:
                    msg_def = db.get_message_by_name("Gateway_VehicleSpeed")
                    data = msg_def.encode(
                        {
                            "vehicle_speed": sim.speed,
                            "odometer": int(sim.uptime * 10),
                            "gear_position": 3,  # DRIVE
                            "brake_pedal": 1 if sim.brake_pedal else 0,
                            "accel_pedal_position": int(sim.accel_pedal),
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # Gateway_BodyControls (200ms / iteration 4)
                if iteration % 4 == 0:
                    msg_def = db.get_message_by_name("Gateway_BodyControls")
                    data = msg_def.encode(
                        {
                            "door_driver_open": 0,
                            "door_passenger_open": 0,
                            "door_rear_left_open": 0,
                            "door_rear_right_open": 0,
                            "hood_open": 0,
                            "trunk_open": 0,
                            "headlights_on": 1,
                            "turn_signal_left": 0,
                            "turn_signal_right": 0,
                            "hazard_lights": 0,
                            "wiper_status": 0,
                            "hvac_fan_speed": 5,
                            "hvac_temperature": 22.0,
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # Gateway_ChargeStatus (500ms / iteration 10)
                if iteration % 10 == 0:
                    msg_def = db.get_message_by_name("Gateway_ChargeStatus")
                    data = msg_def.encode(
                        {
                            "charge_port_open": 0,
                            "charge_cable_connected": 0,
                            "charging_active": 0,
                            "charge_power_available": 0.0,
                            "estimated_time_to_full": 0,
                            "charge_current_limit": 32,
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # Gateway_Diagnostics (1000ms / iteration 20)
                if iteration % 20 == 0:
                    msg_def = db.get_message_by_name("Gateway_Diagnostics")
                    data = msg_def.encode(
                        {
                            "system_uptime": int(sim.uptime),
                            "battery_12v_voltage": 13.8,
                            "key_state": 2,  # ON
                            "parking_brake": 0,
                        }
                    )
                    bus.send(can.Message(arbitration_id=msg_def.frame_id, data=data))

                # Log status every second
                if iteration % 20 == 0:
                    logger.info(
                        f"EV Sim: SOC={sim.soc:.1f}% Speed={sim.speed:.1f}km/h "
                        f"Motor={sim.motor_speed}rpm Torque={sim.motor_torque:.1f}Nm "
                        f"Power={sim.pack_current * sim.pack_voltage / 1000:.1f}kW"
                    )

            except Exception as e:
                logger.error(f"Error encoding/sending demo message: {e}")

            iteration += 1
            await asyncio.sleep(dt)

    except asyncio.CancelledError:
        logger.info("EV simulation cancelled")
    except Exception as e:
        logger.exception(f"Error in EV simulation: {e}")

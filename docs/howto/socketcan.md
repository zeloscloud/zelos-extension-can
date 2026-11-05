# Bring up a SocketCAN bus on Linux

On Linux the kernel drives most USB and PCIe CAN adapters (PEAK, Kvaser, candleLight,
CANable) as SocketCAN interfaces: `can0`, `can1`, ... No vendor driver is needed. The
extension opens the interface as it is, so bitrate, CAN FD and recovery are set with
`ip link` before you start it.

Needs `iproute2` (`ip`) and, for the checks below, `can-utils`:

```bash
sudo apt install can-utils
```

## Classic CAN

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 restart-ms 100
sudo ip link set can0 txqueuelen 1000
sudo ip link set can0 up
```

| Setting | Why |
|---|---|
| `bitrate 500000` | Must match every other node on the bus. |
| `restart-ms 100` | Restart the controller 100 ms after bus-off. Without it the interface stays bus-off until you bounce it. Some gs_usb firmware rejects it; drop it there. |
| `txqueuelen 1000` | The kernel default is 10 frames. A burst of sends overruns it and fails with `No buffer space available` (`ENOBUFS`). |

Check it:

```bash
ip -details link show can0    # state ERROR-ACTIVE, bitrate 500000, restart-ms 100, qlen 1000
candump can0                  # frames from the bus
cansend can0 123#DEADBEEF     # one frame out
```

## CAN FD

Set the arbitration (`bitrate`) and data (`dbitrate`) rates and turn `fd` on:

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 dbitrate 2000000 fd on restart-ms 100
sudo ip link set can0 txqueuelen 1000
sudo ip link set can0 up
```

`ip -details link show can0` now shows `<FD>` in the flags, `mtu 72`, and both rates.
Turn on **CAN-FD Mode** on the bus in the extension's form. Check with:

```bash
cansend can0 123##1DEADBEEFCAFEF00D   # FD frame, BRS on (flags 1)
```

The adapter must support FD (PCAN-USB FD / Pro FD, Kvaser FD, candleLight FD firmware).
`ip link` refuses `fd on` on one that does not.

## Keep it across reboots

`ip link` settings are lost on reboot or replug. systemd-networkd sets them each time
the interface appears:

`/etc/systemd/network/80-can0.network`

```ini
[Match]
Name=can0

[CAN]
BitRate=500K
RestartSec=100ms
# CAN FD:
# DataBitRate=2M
# FDMode=yes
```

`/etc/systemd/network/70-can0.link`

```ini
[Match]
OriginalName=can0

[Link]
TransmitQueueLength=1000
```

Only the first `.link` file that matches a device applies, in name order. Ubuntu's
`73-usb-net-by-mac.link` matches USB adapters, so this one must sort before it (`70-`).

```bash
sudo systemctl enable --now systemd-networkd
sudo networkctl reload
```

The `.network` file applies on reload; `.link` only when the adapter appears, so replug
it (or reboot). Check with `ip -details link show can0` and
`networkctl status can0`, which names the `.network` and `.link` files in use.

## No hardware: virtual CAN

`vcan` is a loopback bus inside the kernel: every frame sent is received by every reader.

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set vcan0 up
```

It carries CAN FD frames too (`mtu 72`).

## Use it

Press **Auto-configure** in the extension's form: it adds one SocketCAN bus per
interface, hardware before `vcan`. Or **Choose** beside a bus's Channel. From the CLI:

```bash
zelos extensions start zeloscloud.zelos-extension-can \
  --config '{"buses": [{"interface": "SocketCAN", "channel": "can0"}]}'
```

Add `"fd_mode": true` for a CAN FD bus.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `RTNETLINK answers: Device or resource busy` | Set the bitrate while the interface is up. `ip link set can0 down` first. |
| `RTNETLINK answers: Operation not supported` on `restart-ms` | The driver has no auto restart (some gs_usb firmware). Drop `restart-ms`. |
| `ip link show` says `state BUS-OFF` or `ERROR-PASSIVE` | Bitrate mismatch, no termination (120 ohm at each end), or no other node to acknowledge frames. |
| `No buffer space available` on send | TX queue full: raise `txqueuelen`, or the bus is not acknowledging (see above). |
| `can0` missing | Adapter not plugged in, or its kernel module did not load (`dmesg`, `lsmod | grep can`). |

## Links

- Kernel SocketCAN docs: <https://docs.kernel.org/networking/can.html>
- can-utils: <https://github.com/linux-can/can-utils>
- systemd-networkd `[CAN]`: <https://www.freedesktop.org/software/systemd/man/latest/systemd.network.html>

# Install the Kvaser driver on Windows

A Kvaser adapter needs Kvaser's CANlib on the machine running the agent. On Windows
that is `canlib32.dll`, installed by **Kvaser Drivers for Windows**. On Linux, use the
kernel driver instead ([SocketCAN](socketcan.md)). Kvaser ships no macOS driver.

## Install

1. Download **Kvaser Drivers for Windows** from <https://kvaser.com/download/>.
2. Run the installer as Administrator and accept the defaults.
3. Plug in the adapter. Device Manager lists it under **CAN Devices**.

Check the DLL is on the search path:

```bat
where canlib32.dll
```

**Kvaser Device Guide** (Start menu, installed with the driver) lists every channel
and can blink an adapter's LEDs to tell several apart.

## Virtual channels

The driver also installs **Kvaser Virtual CAN Driver**: two virtual channels wired to
each other, for testing without hardware. Auto-configure lists them as buses beside
the real ones; remove the ones you do not want before saving.

## Use it

Plug in the adapter, open the extension's config form, and press **Auto-configure**.
It adds one `kvaser` bus per channel CANlib finds (channel `0`, `1`, ...) at
500 kbit/s. Set **Bitrate** to match your bus, save, and start. For CAN FD, turn on
**CAN-FD Mode** and pick the **CAN-FD Data Bitrate**.

From the CLI:

```bash
zelos extensions start zeloscloud.zelos-extension-can \
  --config '{"buses": [{"interface": "Kvaser", "channel": "0", "bitrate": 500000}]}'
```

## Troubleshooting

### Auto-configure adds a Demo bus instead

The driver is missing or found no channel. Check `where canlib32.dll`, replug the
adapter, and close any program holding it (Kvaser CanKing, another extension).

### `Kvaser canlib is unavailable` in the log

`canlib32.dll` could not be loaded: the driver is not installed, or Windows has not
restarted since. Re-run the installer and restart Windows.

## Links

- Kvaser downloads: <https://kvaser.com/download/>

# Use an RP1210 adapter on Windows

RP1210 adapters are the diagnostic adapters of heavy-duty vehicles and equipment. The
adapter's vendor supplies an **RP1210 driver** for Windows, and the extension opens the
adapter through it. Install the vendor's **64-bit** driver on the machine running the
agent.

## Install

1. Install the adapter vendor's 64-bit RP1210 driver, as Administrator.
2. Plug in the adapter.

The driver adds its name to `C:\Windows\RP121032.INI`:

```bat
type C:\Windows\RP121032.INI
```

## Use it

1. Open the extension's config form and add a bus with **Interface** `RP1210`.
2. Press **Choose** beside **Adapter**. It lists every installed driver's devices as
   `driver:device`, each beside the driver's own description. Pick the one that
   matches how your adapter is connected.
3. Keep **Bitrate** at 250000 for J1939, or set it to match your bus.
4. Give the bus a **Name**, save and start.

From the CLI:

```bash
zelos extensions start zeloscloud.zelos-extension-can \
  --config '{"buses": [{"name": "truck", "interface": "RP1210", "channel": "VEND32:1", "bitrate": 250000}]}'
```

## What the bus carries

Classic CAN data frames, timestamped by the adapter. J1939 decode, transport messages
and address claim work as on any other interface.

A driver that offers J1939 alone carries the 29-bit frames of the bus; the log says so
when the bus starts.

**Device Channel** picks a CAN channel on a multi-channel adapter, counted from 1.

## Errors and fixes

Every refusal is logged with the driver's own text and error code.

| The log says | Fix |
|---|---|
| `is not a 64-bit library` | Install the vendor's 64-bit RP1210 driver |
| `The RP1210 driver ... is not available` | Press **Choose** to see the drivers that are installed |
| The driver refuses the protocol or the bitrate | Set **Bitrate** to 250000 |
| The device is in use | Close the other program holding the adapter, or match its bitrate |
| The device is invalid, or hardware is not responding | Plug in the adapter and pick the device that matches how it is connected |

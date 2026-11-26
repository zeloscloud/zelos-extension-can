# Use an RP1210 adapter on Windows

RP1210 adapters are the diagnostic adapters of heavy-duty vehicles and equipment. The
adapter's vendor supplies an **RP1210 driver** for Windows, and the extension opens the
adapter through it. Install the vendor's driver on the machine running the agent.

## Install

1. Install the adapter vendor's RP1210 driver, as Administrator.
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

## A 32-bit driver

A 64-bit driver loads directly. For a vendor that supplies a 32-bit driver only:

1. Install a 32-bit Python 3 from python.org (the "Windows installer (32-bit)").
2. Set **32-bit Python** on the bus to its `python.exe`.

The driver then runs in a helper process under that Python, and frames reach the
extension with the adapter's own timestamps.

## Errors and fixes

Every refusal is logged with the driver's own text and error code.

| The log says | Fix |
|---|---|
| `is not a 64-bit library` | Install the vendor's 64-bit driver, or set **32-bit Python** |
| `The RP1210 driver ... is not available` | Press **Choose** to see the drivers that are installed |
| The driver refuses the protocol or the bitrate | Set **Bitrate** to 250000 |
| The device is in use | Close the other program holding the adapter, or match its bitrate |
| The device is invalid, or hardware is not responding | Plug in the adapter and pick the device that matches how it is connected |

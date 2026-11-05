# Install the PCAN driver

A PEAK PCAN adapter needs PEAK's driver library on the machine running the agent:
`PCANBasic.dll` on Windows, `libPCBUSB.dylib` on macOS. Linux needs nothing; the
kernel driver exposes the adapter as SocketCAN (`can0`, see [SocketCAN](socketcan.md)).

| OS | Library | From |
|---|---|---|
| Windows 10/11 (x64, ARM64 on 11) | `PCANBasic.dll` | PEAK Device Driver Setup |
| macOS 12+ (Intel, Apple silicon) | `libPCBUSB.dylib` | MacCAN PCBUSB, a third-party library PEAK does not ship |

## Windows

1. Download PEAK's **Device Driver Setup** from <https://www.peak-system.com/quick/DrvSetup>
   (listed under <https://www.peak-system.com/support/downloads/drivers/>).
2. Extract the zip and run the setup as Administrator.
3. In the component list, keep **API Libraries > PCAN-Basic API** selected (it is by
   default). It installs `PCANBasic.dll`, which the extension loads.
4. Plug in the adapter. Device Manager lists it under **CAN-Hardware** (for example
   **PCAN-USB**).

Check the DLL is on the search path:

```bat
where PCANBasic.dll
```

## macOS

```bash
curl -LO https://github.com/mac-can/PCBUSB-Library/releases/download/v0.13/macOS_Library_for_PCANUSB_v0.13.tar.gz
tar -xzf macOS_Library_for_PCANUSB_v0.13.tar.gz
cd PCBUSB
sudo ./install.sh
```

`install.sh` copies `libPCBUSB.0.13.dylib` to `/usr/local/lib`, links it as
`libPCBUSB.dylib`, and copies `PCBUSB.h` to `/usr/local/include`. Check it:

```bash
ls -l /usr/local/lib/libPCBUSB*
```

Download with `curl` as above. A browser download marks the files as quarantined, and
because the library is not notarized, macOS then refuses to load it (see
[Troubleshooting](#macos-blocks-libpcbusb)).

PCBUSB drives the **PCAN-USB** and **PCAN-USB FD**, up to 8 at once. On a **PCAN-USB
Pro FD** it opens only the first channel. A PCAN-USB FD's LED does not turn green when
plugged in on macOS; that is expected.

Uninstall:

```bash
sudo rm /usr/local/lib/libPCBUSB.dylib /usr/local/lib/libPCBUSB.0.13.dylib /usr/local/include/PCBUSB.h
```

## Use it

Plug in the adapter, open the extension's config form, and press **Auto-configure**.
It adds one `pcan` bus per channel the driver finds (`PCAN_USBBUS1`, `PCAN_USBBUS2`, ...)
at 500 kbit/s. Set **Bitrate** to match your bus, save, and start. For CAN FD on a
PCAN-USB FD or Pro FD, turn on **CAN-FD Mode** and pick the **CAN-FD Data Bitrate**.

From the CLI:

```bash
zelos extensions start zeloscloud.zelos-extension-can \
  --config '{"buses": [{"interface": "PCAN", "channel": "PCAN_USBBUS1", "bitrate": 500000}]}'
```

## Troubleshooting

### Auto-configure adds a Demo bus instead

The driver library is missing or found no adapter. Re-run the check for your OS above,
replug the adapter, and close any other program holding it (PCAN-View, another
extension). On macOS, also check the quarantine below.

### macOS blocks libPCBUSB

macOS says `libPCBUSB.0.13.dylib` "Not Opened" or cannot be verified. Allow it once:
**System Settings > Privacy & Security**, scroll to the message about
`libPCBUSB.0.13.dylib`, press **Open Anyway**, then restart the extension.

Or clear the quarantine flag from a terminal:

```bash
sudo xattr -d com.apple.quarantine /usr/local/lib/libPCBUSB.0.13.dylib
```

### `PCBUSB library not found` / `PCANBasic library not found`

The extension could not find the library. On macOS, check `/usr/local/lib/libPCBUSB.dylib`
exists; on Windows, check `where PCANBasic.dll` prints a path. Re-run the install.

## Links

- MacCAN PCBUSB: <https://www.mac-can.com>, <https://github.com/mac-can/PCBUSB-Library>
- PEAK drivers: <https://www.peak-system.com/support/downloads/drivers/>

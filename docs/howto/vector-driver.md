# Install the Vector driver on Windows

A Vector adapter needs two free Vector packages on the Windows machine running the
agent: the **Vector Driver Setup** (device drivers) and the **XL Driver Library**
(`vxlapi64.dll`, the API the extension opens channels through). Vector ships neither
for macOS or Linux; from those, use [SocketCAN over SSH](../../README.md#remote-can-over-ssh)
to a Linux device instead.

## Install

1. Download both from <https://www.vector.com/int/en/download/>: search
   **Vector Driver Setup** (Windows 10 and 11) and **XL Driver Library**.
2. Unzip the Driver Setup and run `setup.exe` as Administrator; accept the defaults.
3. Unzip the XL Driver Library and run its setup; it puts `vxlapi64.dll` in the
   system folder.
4. Plug in the adapter. **Vector Hardware Manager** (Start menu, installed with the
   driver) lists it with its serial number and channels.

Check the DLL is on the search path:

```bat
where vxlapi64.dll
```

## Virtual channels

The driver also installs a virtual device: **Virtual Channel 1** and **Virtual
Channel 2**, wired to each other, CAN FD capable, for testing without hardware.
Auto-configure lists them as buses beside the real ones; remove the ones you do not
want before saving.

## Use it

Plug in the adapter, open the extension's config form, and press **Auto-configure**.
It adds one `Vector` bus per channel, each set by the device's **Serial Number** and
the **Channel** on that device (`0` is its Channel 1), named after it
(`VN1640A_Channel_1`), at 500 kbit/s. The serial keeps a bus on the same device when
adapters are plugged in, unplugged or reordered. Set **Bitrate** to match your bus,
save, and start. For CAN FD, turn on **CAN-FD Mode** and pick the
**CAN-FD Data Bitrate**.

From the CLI, with the serial Vector Hardware Manager shows:

```bash
zelos extensions start zeloscloud.zelos-extension-can \
  --config '{"buses": [{"interface": "Vector", "serial": 12345, "channel": "0", "bitrate": 500000}]}'
```

## Troubleshooting

### `Channel '0' of application ... is not assigned to any interface`

The bus has no **Serial Number**, so its channel is read as an application channel
of Vector Hardware Manager, and none is assigned. Press **Auto-configure**, or set
the serial by hand.

### `Another application might have set incompatible settings`

Another program has the channel open at a different bitrate. Close it, or match its
bitrate.

### `The VECTOR driver library is not available`

`vxlapi64.dll` could not be loaded: install the XL Driver Library (above) and check
`where vxlapi64.dll`.

### Auto-configure adds a Demo bus instead

The driver is missing or found no channel. Replug the adapter and check it in Vector
Hardware Manager.

## Links

- Vector downloads: <https://www.vector.com/int/en/download/>

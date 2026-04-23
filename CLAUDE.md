# CLAUDE.md

Two RX paths in `zelos_extension_can.codec.CanCodec`, switched on
`_use_native_rx`:

- **Native** (`zelos-virtual`, `zelos-socketcan`) — `zelos_can.CanCodec`
  owns recv, DBC decode, schema registration, and trace emit. The
  python-can `Bus` stays open only for TX actions; no `Notifier` is
  attached and `on_message_received` is a no-op.
- **Legacy** (`virtual`, `socketcan`, `pcan`, `kvaser`, `vector`) —
  python-can `Notifier` + extension `Listener` + cantools decode.

## Invariants

1. No Python per-frame work on the native path. Gate any new
   per-frame hook on `_use_native_rx`.
2. `zelos-virtual` shares one channel: `zelos_can.CanCodec(bus=...)`
   takes rx out of `PyVirtualBus`. Don't `.recv()` on the python-can
   wrapper after the codec is built.
3. `zelos-socketcan` opens a second kernel socket alongside the
   python-can `Bus`. Don't add RX work on the python-can side.
4. Snapshot `get_metrics()` before `codec.stop()` — native mode tears
   down the metrics handle.
5. `codec.run()` calls `asyncio.run()`. Inside an existing loop,
   schedule `_run_async()` directly (see `cli/app.py`).

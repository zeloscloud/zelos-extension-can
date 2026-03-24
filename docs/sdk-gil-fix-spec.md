# SDK GIL Fix Specification

## Problem

`zelos_sdk.TraceSourceEvent.log_at()` and `.log()` are implemented in a native
C/Rust extension (`zelos_sdk.abi3.so`). These methods hold the Python GIL for
the entire call duration, including periodic buffer flushes and file I/O.

Because the GIL is process-wide, a single long flush (measured up to **2.4 s**)
starves every other Python thread — including `can.Notifier`'s recv thread.
The kernel SocketCAN receive buffer (~800 frames at default `SO_RCVBUF`)
silently overflows, causing permanent data loss with no error or warning.

## Evidence

| Metric | Value |
|---|---|
| `event.log_at()` p50 | 2.8 µs |
| `event.log_at()` p99 | 15.5 µs |
| `event.log_at()` max | 38 ms |
| `_handle_message()` p50 | 77 µs |
| `_handle_message()` p99 | 641 µs |
| `_handle_message()` max | **2.4 s** |
| Reception without TraceWriter | 99.5% |
| Reception with TraceWriter | ~35% (64% silent loss) |
| `candump` (C, no GIL) at same rate | 100% |

The max latency spikes correlate with `TraceWriter` internal buffer flushes.
Adding a Python-side queue between recv and processing does not fix the loss
because the worker thread calling `log_at()` still holds the GIL during
flushes, starving the recv thread.

## Scope of Fix

**Only two methods** on `TraceSourceEvent`:

- `log(**kwargs)` — log with current timestamp
- `log_at(timestamp_ns, **kwargs)` — log with explicit timestamp

These are the hot-path methods called per CAN frame (hundreds to thousands of
times per second).

`TraceWriter.open()` / `close()` / `__enter__` / `__exit__` are called once
per session and do **not** need the optimization.

## Required Change

In the native implementation (PyO3/Rust or CPython C API):

1. **Parse arguments** while holding the GIL — convert Python objects to
   native types (timestamp, field values).
2. **Release the GIL** before any buffer append, flush check, or file I/O.
   - PyO3 (Rust): `py.allow_threads(|| { /* native work here */ })`
   - CPython C API: `Py_BEGIN_ALLOW_THREADS` / `Py_END_ALLOW_THREADS`
3. **Re-acquire the GIL** only if returning a Python object (these methods
   return `None`, so typically just re-acquire for the return path).

The internal buffer/flush logic should be fully thread-safe already since
`TraceWriter` handles concurrent sources. If not, the flush path needs a
native mutex (not the GIL) to protect shared state.

## Pseudocode (PyO3)

```rust
#[pyo3(signature = (timestamp_ns, **kwargs))]
fn log_at(&self, py: Python, timestamp_ns: i64, kwargs: HashMap<String, PyObject>) -> PyResult<()> {
    // Step 1: Convert Python args to Rust types while holding GIL
    let fields = extract_fields(py, kwargs)?;

    // Step 2: Release GIL for the actual write/flush
    py.allow_threads(|| {
        self.inner.append(timestamp_ns, &fields);  // may trigger flush → disk I/O
    });

    Ok(())
}
```

## Validation

After the fix, run the cross-platform test harness:

```bash
# Baseline (should always pass)
uv run python scripts/test_frame_loss.py --without-tracewriter --frames 5000

# GIL contention test (should pass after fix, fails before)
uv run python scripts/test_frame_loss.py --with-tracewriter --frames 5000
```

Both must report `PASS: Zero frame loss.`

The pytest regression test can also be run:

```bash
uv run pytest tests/test_frame_loss.py -v
```

Expected results:

| Test | Before fix | After fix |
|---|---|---|
| `test_no_loss_without_tracewriter` | PASS | PASS |
| `test_no_loss_with_tracewriter` | FAIL (~60% loss) | PASS |
| `test_rx_queue_drops_tracked` | PASS | PASS |

## Notes

- The extension-side queue + `SO_RCVBUF` increase (already applied) provides
  belt-and-suspenders defense against transient stalls even after the SDK fix.
- The `virtual` interface test isolates GIL starvation specifically — the
  virtual bus never drops frames at the transport layer, so any loss proves
  GIL contention in the codec/SDK path.
- On Linux with `socketcan`/`vcan0`, kernel buffer overflow is also a factor,
  which the `SO_RCVBUF` increase mitigates.

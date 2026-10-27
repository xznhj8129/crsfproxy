# AGENTS.md — working notes for crsfproxy

Project-specific workflow and hard-won constraints. The global agent policy and
the repository's Git rules still apply. Keep this file current when a durable
fact changes.

## What this project is

UDP ⇄ CRSF proxy for ExpressLRS / mLRS:

- `crsfproxy.py` — receives UDP RC packets, emits CRSF on a serial link,
  forwards telemetry, and serves the UDP configuration API.
- `joystick_crsf.py` — UDP RC sender (needs a joystick via pygame).
- `config_client.py` — remote ELRS Lua configuration client; talks to
  crsfproxy's UDP config port, not directly to the radio.
- `lib/` — radio backends, CRSF codec, RC packet format, deadline scheduler,
  serial/transport and config transport.
- `crsf_rx_sim.py` — virtual CRSF receiver for INAV SITL.
- `README.md`, `SITL_RECEIVER.md`, `tests/`.

Version: `v0.3` tag at `e66fc45`. Master includes the `socket://` block-read
fix (`7a13976`); the v0.3 tag does not. Use master or `7a13976`+ whenever
testing the ESP32-P4 transparent bridge.

## Session start

1. `git status --short`, `git log --oneline -8`, current branch.
2. Read the relevant README sections, especially Options, Notes & gotchas,
   Remote TX configuration, and SITL.
3. Identify the transport:
   - real serial device (`/dev/ttyUSB0`, `/dev/ttyACM0`, ...);
   - `socket://host:port` — transparent TCP bridge (ESP32-P4 CRSF bridge or
     SITL virtual UART);
   - TCP/UDP endpoints used by `--telemetry_udp`, `--config_udp`, `--port`.
4. If using the ESP32-P4 bridge, check the device `/info` and `/diag`; the UART
   baud is `crsf_baud` on the P4. `--baud` is ignored over `socket://`.
5. Ensure only one RC source and one TCP client are active. Check
   `ss -lunp | grep -E ':60000|:60001'` and the device bridge counters.

## Running and testing

```bash
python3 crsfproxy.py --device /dev/ttyUSB0 --baud 400000
python3 crsfproxy.py --device socket://<device_ip>:60000 --baud 400000
python3 joystick_crsf.py --target 127.0.0.1 --port 60000 --rate 50
python3 config_client.py --port 60001 info
```

- Over `socket://`, `--baud` is ignored; set the serial side on the bridge
  (`crsf_baud` on the ESP32-P4).
- `--tx_rate` is a maximum RC frame rate; `--loop_hz` is the main-loop cap.
  The deadline scheduler (`lib/schedule.py`) sends one frame per due slot and
  skips missed slots rather than bursting. While the loop outruns the slot
  rate, the average rate is `--tx_rate`; with `--loop_hz 250`, `--tx_rate 300`
  measures ~244 Hz. Keep `--loop_hz` comfortably above `--tx_rate`.
- `--rc-hold-ms N` repeats the latest sample for up to N ms after RC stops;
  `0` disables hold. crsfproxy does not inject center/failsafe frames; failsafe
  is downstream.
- `--telemetry_udp host:port` forwards valid inbound CRSF frames unchanged.
  `--config_udp` defaults to RC port + 1 (60001).
- `--debug` prints every serial TX/RX chunk and frame; use it for frame-level
  proof, but expect binary/control bytes in logs (`grep -a`).
- The repository has `tests/`; run the existing suite before changing code.
  Unit tests do not replace a real serial/bridge test.

## socket:// specifics (critical)

- pyserial's `socket://` `in_waiting` returns `len(select(...))`, which is 1
  when readable — not the number of bytes. Calling `read(in_waiting)` therefore
  reads one byte per loop. `lib/elrs_backend.py` and `lib/elrs_config.py` use a
  256-byte zero-timeout block read instead. Do not revert to `in_waiting`.
- The `socket://` transport is a byte stream, not packetized. The ESP32-P4
  bridge writes each TCP `recv()` chunk as one half-duplex burst; the CRSF
  parser reassembles bytes. Do not assume frame boundaries in TCP.
- The bridge accepts one TCP client; a new connection replaces the old one.
- The bridge sets `TCP_NODELAY` on its accepted socket. pyserial's socket
  client does not set `TCP_NODELAY`; lwIP delayed ACK can hold small writes.
  If timing measurement shows it, the fix belongs here in crsfproxy.
- `--baud` has no effect over `socket://`; the P4 UART is configured on the
  device. The ELRS TX module locks to the first baud it sees after boot, so set
  the P4 baud first, then reboot/power-cycle the TX.

## ELRS / CRSF facts

- The module locks to the first handset baud after boot. Changing the P4 baud
  requires a TX reboot/power cycle.
- Verified with RM Nomad X-Band, ELRS 4.0.1. Current settings at last test:
  `100Hz Full(-112dBm)`, `Switch Mode=16ch Rate/2`, `Link Mode=MAVLink`.
- `info` header `packets_good/bad` is the **handset link** (P4/bridge to
  module). The `Bad/Good` parameter is the **RF link** (module to receiver).
  Do not confuse them.
- 400000 is the most forgiving for configuration. 921600 works for RC and
  config, but after a P4/bridge boot it needs a continuous RC stream and about
  10 s for the handset link to settle before discovery is reliable. A failed
  `info`/`get` immediately after boot is usually settling, not a protocol bug.
- One RC source only. Two UDP senders to port 60000 (e.g. a center stream plus
  a sweep) interleave and look like the sticks snapping to center. That is a
  test-harness artifact, not a crsfproxy failsafe.
- For frame-level proof, parse the exact `Serial tx ... frame_hex` lines and
  run-length center vs move. An MSP poll at ~60 Hz can miss a 20 ms center
  frame; the 50 Hz TX log cannot.
- Channels are clamped to 1000..2000 us and mapped to CRSF 172..1811. The
  40-byte UDP RC packet is little-endian: timestamp, 16 × uint16 us, CRC32.

## Remote TX configuration

- Use `config_client.py --port 60001 ...` (`info`, `devices`, `params`, `get`,
  `set`, `command`, `tui`). It reaches crsfproxy over UDP; no direct serial.
- Keep a continuous RC stream while configuring; ELRS modules are more
  reliable with the handset link active.
- `set` verifies by reading the parameter back; report old/new and
  `verified=1`.
- The configuration port has no authentication; bind/firewall it to a trusted
  network.

## Test hygiene

- Start long sessions with `setsid timeout ...`; record explicit PIDs in
  `/tmp`. Stop every client and remove stale `/tmp/*.pid` files.
- Never use `pkill -f` with a pattern that appears in the current shell command
  line; it kills the shell. Kill explicit PIDs.
- Check cleanup:
  `ps -ef | grep -E "crsfproxy|send_rc|rc_sweep|poll_msp|mspapi"`,
  `ss -lunp | grep -E ':60000|:60001'`,
  `fuser /dev/ttyUSB0 /dev/ttyACM0 /dev/ttyACM1`.
- If testing the drone FC directly with `mspapi2`, use the
  `inav_development/mspapi2` checkout (the pip editable path is stale),
  `/dev/ttyACM1`, 115200. `get_rc_channels()` runs at about 60 Hz.
- Leave the radio/bridge in a known state and the host free of listeners.

## Do not

- Do not call `read(ser.in_waiting)` on `socket://`; use the block read.
- Do not assume `--baud` applies over `socket://`.
- Do not run two RC sources against one proxy.
- Do not expect 921600 config discovery to work immediately after a bridge
  boot; keep RC flowing and retry after the handset link settles.
- Do not confuse handset `packets_good/bad` with RF `Bad/Good`.
- Do not claim hardware verification from a unit-test-only result.
- Do not edit the ESP32-P4 firmware from this repository; the bridge is a
  separate project. Coordinate the `socket://` byte-stream contract with it.
- Do not commit logs, local captures, or user-specific serial paths.

## Git

- Commit/push only when the user asks.
- `origin` is SSH (`git@github.com:xznhj8129/crsfproxy.git`).
- Keep the README's version/ELRS compatibility statement consistent with the
  code and the `socket://` block-read requirement.

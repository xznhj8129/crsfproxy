# AGENTS.md — working notes for crsfproxy

Project-specific workflow and hard-won constraints. The global agent policy and
the repository's Git rules still apply. Keep this file current when a durable
fact changes.

## What this project is

Protocol v1 server for one CRSF device (ExpressLRS / mLRS TX module):

- `crsfproxy.py` — the server: one UDP port, CRSF RC/telemetry, CONTROL JSON,
  STATUS.
- `joystick_crsf.py` — CRSF RC sender.
- `config_client.py` — CONTROL client (CLI + TUI).
- `crsf_rx_sim.py` — virtual CRSF receiver for INAV SITL; same v1 envelope,
  CONTROL only.
- `lib/` — envelope, server loop, CRSF codec, radio backends, transport, config
  objects.
- `README.md`, `PROTOCOL.md`, `SITL_RECEIVER.md`, `tests/`.

Version: v0.4 implements protocol v1. v0.3 is the previous release (40-byte RC
packet, config port + 1). The `socket://` block-read fix is in `lib/server.py`;
the v0.3 tag lacks it.

## Session start

1. `git status --short`, `git log --oneline -8`, current branch.
2. Read the README's Quick start, Options, and Notes; read `PROTOCOL.md` for the
   contract.
3. Identify the transport:
   - real serial device (`/dev/ttyUSB0`, `/dev/ttyACM0`, ...);
   - `socket://host:port` — transparent TCP bridge (ESP32-P4 or SITL UART);
   - `--telemetry` / `--telemetry_raw` destinations.
4. If using the ESP32-P4 bridge, check the device `/info` and `/diag`; the UART
   baud is `crsf_baud` on the P4. `--baud` is ignored over `socket://`.
5. Ensure one RC source and one TCP client. Check `ss -lunp | grep ':60000'`
   and the device bridge counters.
6. One configurator: CONTROL is serialized; raw CRSF configuration is accepted
   only from the RC owner and is paused while CONTROL runs.

## Running and testing

```bash
python3 crsfproxy.py --device /dev/ttyUSB0
python3 crsfproxy.py --device socket://<device_ip>:60000 --baud 400000
python3 joystick_crsf.py --target 127.0.0.1 --port 60000 --rate 50
python3 config_client.py --port 60000 info
```

- One UDP port, default `60000`. CONTROL is on the same port; v0.3 used RC port
  + 1.
- `--telemetry host:port` sends CRSF envelopes + STATUS; `--telemetry_raw
  host:port` sends bare CRSF frames (MWP); both repeatable.
- Over `socket://`, `--baud` is ignored; set the serial side on the bridge.
- `--tx_rate` is a maximum RC frame rate; `--loop_hz` is the main-loop cap.
  `lib/schedule.py` sends one frame per due slot and skips missed slots. Keep
  `--loop_hz` comfortably above `--tx_rate`; with `--loop_hz 250`, `--tx_rate
  300` measures about 244 Hz.
- `--rc-hold-ms N` repeats the latest RC sample for up to N ms after RC stops;
  `0` disables hold. crsfproxy does not inject center/failsafe frames; failsafe
  is downstream.
- `--rc_owner_timeout_ms` (default 1000) releases the RC owner. A newer frame
  from the released owner can reacquire; an older one is stale; a different
  `(peer, session)` becomes the new owner.
- `--status_hz` (default 5) sends STATUS to `--telemetry` destinations.
- `--debug` prints serial chunks, frames, RC state, CONTROL requests/replies,
  and drops.
- Run the tests before changing code. Unit tests do not replace a real
  serial/bridge test.

## socket:// specifics (critical)

- pyserial's `socket://` `in_waiting` returns `len(select(...))`, which is 1
  when readable — not the number of bytes. `lib/server.py` uses a 256-byte
  zero-timeout block read. Do not revert to `in_waiting`.
- `socket://` is a byte stream, not packetized. The ESP32-P4 bridge writes each
  TCP `recv()` chunk as one half-duplex burst; the CRSF parser reassembles
  bytes. Do not assume frame boundaries in TCP.
- The bridge accepts one TCP client; a new connection replaces the old one.
- The bridge sets `TCP_NODELAY` on its accepted socket. pyserial's socket
  client does not; lwIP delayed ACK can hold small writes. If timing shows it,
  the fix belongs in `lib/server.py`.
- `--baud` has no effect over `socket://`; the P4 UART is configured on the
  device. The ELRS TX module locks to the first handset baud after boot; set
  the P4 baud first, then reboot/power-cycle the TX.

## ELRS / CRSF facts

- The server enforces one RC owner. A second sender is counted as `rc_foreign`
  and does not move the sticks.
- The module locks to the first handset baud after boot. Changing the baud
  requires a TX reboot/power cycle.
- Verified with RM Nomad X-Band, ELRS 4.0.1. Current settings at last test:
  `RF Band=915MHz`, `100Hz Full(-112dBm)`, `Switch Mode=16ch Rate/2`,
  `Link Mode=MAVLink`, `Telem Ratio=1:2`, `TX Power (25mW)`, `Max Power=25`,
  `Dynamic=Off`, `Model Match=Off`.
- RC is CRSF; the TX `Link Mode` (Normal/MAVLink) is independent of the RC
  channel path. The FC's serial RX provider must be CRSF. In the 2026-11-01
  test, the FC provider set to MAVLink did not carry RC; switching it to CRSF
  made RC work with the TX link mode unchanged.
- v0.4 hardware check (2026-11-01): `/dev/ttyUSB0` at **115200**; RM Nomad
  X-Band + 900 MHz RX on an INAV FC at `/dev/ttyACM0`; `RF Band=915MHz`,
  `Packet Rate=100Hz Full`, `Max Power=25`. `config_client
  info/params/get/set` worked; `set "Model Match" "Off"` (same value) returned
  `verified=1`; RC worked, including Ch5 low, with the FC serial RX provider
  set to CRSF. TX link LQ reached 100.
- 400000 and 921600 are valid handset bauds. 400000 is the most forgiving for
  configuration; 921600 exposes high packet rates. If the module is already
  locked to 115200, changing the proxy baud requires a TX power cycle.
- `info` header `packets_good/bad` is the **handset link** (proxy to module).
  The `Bad/Good` parameter is the **RF link** (module to receiver). Do not
  confuse them.
- The ELRS `Model Match` parameter is not a model ID. `info.model_id` is `null`
  when the firmware has no `Model Id` parameter; use `params` for the full
  table.
- Channels are mapped by the client/codec: 1000–2000 µs to CRSF 172–1811. The
  server passes received CRSF frames unchanged.
- For frame-level proof, parse the exact `Serial tx ... frame_hex` lines and
  run-length center vs move. An MSP poll at ~60 Hz can miss a 20 ms center
  frame; the 50 Hz TX log cannot.

## Remote TX configuration

- Use `config_client.py --port 60000 ...` (`info`, `devices`, `params`, `get`,
  `set`, `command`, `tui`). No direct serial.
- `params` is paginated; the client loops until `more` is false. Pages are
  compact (no `options`); `get` returns the full parameter with options.
- `info` is a summary; the full table is `params`.
- Keep a continuous RC stream while configuring an ELRS module.
- `set` verifies by reading back; report old/new and `verified=1`.
- One configurator is enforced: CONTROL is serialized, raw CRSF configuration
  is accepted only from the RC owner, and raw configuration is paused while
  CONTROL runs.
- The protocol has no authentication; bind/firewall it to the trusted
  network/VPN.

## Test hygiene

- Start long sessions with `setsid timeout ...`; record explicit PIDs in
  `/tmp`. Stop every client and remove stale `/tmp/*.pid` files.
- Never use `pkill -f` with a pattern that appears in the current shell command
  line; kill explicit PIDs.
- Check cleanup:
  - `ps -ef | grep -E "crsfproxy|send_rc|rc_sweep|poll_msp|mspapi"`;
  - `ss -lunp | grep ':60000'`;
  - `fuser /dev/ttyUSB0 /dev/ttyACM0 /dev/ttyACM1`.
- If testing the drone FC directly with `mspapi2`, use the
  `inav_development/mspapi2` checkout (the pip editable path is stale),
  `/dev/ttyACM1`, 115200. `get_rc_channels()` runs at about 60 Hz.
- Leave the radio/bridge in a known state and the host free of listeners.

## Do not

- Do not call `read(ser.in_waiting)` on `socket://`; use the block read.
- Do not assume `--baud` applies over `socket://`.
- Do not run two RC sources against one proxy; the second is `rc_foreign`.
- Do not expect 921600 config discovery to work immediately after a bridge
  boot; keep RC flowing and retry after the handset link settles.
- Do not confuse handset `packets_good/bad` with RF `Bad/Good`.
- Do not set the FC serial RX provider to MAVLink for CRSF RC; use CRSF.
- Do not claim hardware verification from a unit-test-only result.
- Do not edit the ESP32-P4 firmware from this repository; the bridge is a
  separate project. Coordinate the `socket://` byte-stream contract with it.
- Do not commit logs, local captures, or user-specific serial paths.

## Git

- Commit/push only when the user asks.
- `origin` is SSH (`git@github.com:xznhj8129/crsfproxy.git`).
- Keep the README's version/ELRS compatibility statement consistent with
  `PROTOCOL.md` and the hardware evidence.

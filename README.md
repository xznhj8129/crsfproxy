# crsfproxy

A UDP server that owns one CRSF device and exposes it to the network.

`crsfproxy` sits between an IP network and one CRSF endpoint — normally an
ExpressLRS or mLRS transmitter module. Clients send CRSF frames over UDP; the
server writes them to the module and sends the module's frames back. RC is one
CRSF frame type. Configuration is a JSON request/reply layer on the same port.
The server does not interpret channel values and never generates them.

> **Status:** crsfproxy v0.4 implements protocol v1. The contract is
> [`PROTOCOL.md`](PROTOCOL.md). The v0.3 wire formats are listed under
> [Changes from v0.3](#changes-from-v03).

## The model

```
          user ↔ server                            server ↔ device
 ┌──────────────┐   UDP, one port    ┌────────────┐  serial or socket://  ┌─────────────┐  RF  ┌──────────┐
 │ client(s)    │───────────────────▶│  crsfproxy │──────────────────────▶│ CRSF device │─────▶│ receiver │
 │ joystick     │◀───────────────────│            │◀──────────────────────│ TX module   │◀─────│ + FC     │
 │ configurator │  CRSF, STATUS,     │            │  CRSF frames          └─────────────┘      └──────────┘
 │ GCS, sim     │  CONTROL_REPLY     └────────────┘
 └──────────────┘
```

Three links:

- **user↔server** — UDP over IP. It may cross the internet; run it inside a VPN.
- **server↔device** — local serial, or a transparent TCP byte pipe
  (`--device socket://host:port`). The bridge parses nothing; crsfproxy owns the
  CRSF conversation with the module.
- **ground↔air** — the radio link.

A serial cable is part of a link, not a link of its own. The two directions are
independent: RC goes up, telemetry comes down, and neither waits for the other.

## One protocol

Every user↔server datagram is one envelope: a 10-byte header and a payload.

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | `version` | `1` |
| 1 | 1 | `kind` | payload type |
| 2 | 4 | `session` | sender's session, nonzero |
| 6 | 4 | `seq` | sender's counter for this kind |
| 10 | n | payload | depends on `kind` |

| `kind` | Name | Direction | Payload |
|---|---|---|---|
| 1 | CRSF | both | exactly one CRSF frame |
| 2 | CONTROL | client → server | JSON request object |
| 3 | CONTROL_REPLY | server → requester | JSON reply object |
| 4 | STATUS | server → destinations | link health |

`seq` counts per sender and per kind; it starts at 0 for each session, increments
by one, and wraps at 2³². A CONTROL_REPLY carries the `session` and `seq` of the
request it answers.

The shape follows from four rules:

- **CRSF is the payload.** RC is `RC_CHANNELS_PACKED`; telemetry is the module's
  own frames; parameter traffic is CRSF too. One codec, not one format per
  client.
- **The envelope adds only what UDP needs.** A version, a kind, a session, and a
  sequence number. No second channel encoding and no timestamp field.
- **Session and seq do the reliability work.** They identify the RC owner,
  reject stale frames, retry CONTROL requests without re-executing them, and
  expose downlink loss without a separate protocol.
- **CONTROL is for the server itself.** Device discovery, parameters, writes,
  and commands are JSON request/reply. It is not a second configuration
  protocol on a second port.

The full contract is [`PROTOCOL.md`](PROTOCOL.md).

## Quick start

```bash
pip install pyserial

python3 crsfproxy.py --device /dev/ttyUSB0
python3 joystick_crsf.py --target 127.0.0.1:60000
python3 config_client.py --port 60000 info
```

`crsfproxy` listens on `60000/udp`. The joystick sends CRSF RC frames.
`config_client.py` sends CONTROL requests to the same port.

A remote device through a transparent bridge:

```bash
python3 crsfproxy.py --device socket://192.168.1.50:60000
```

Over `socket://`, `--baud` is ignored: set the UART baud on the bridge. The
bridge accepts one TCP client.

Telemetry to MWP (bare CRSF frames):

```bash
python3 crsfproxy.py --telemetry_raw 127.0.0.1:40042
mwp -d udp://:40042 -a
```

INAV SITL:

```bash
python3 crsfproxy.py --device socket://127.0.0.1:5762
```

Configure the SITL UART as a CRSF serial receiver. No PTY or `socat` bridge is
needed.

One proxy owns one device. Run another process with another `--port` for a
second device; clients target that port. There is no logical system ID in the
protocol.

## RC

- The first accepted RC sender after start, or after a release, becomes the
  **owner**.
- An owner frame with a newer `seq` is accepted. A stale owner frame, or a frame
  from any other sender, is dropped and counted.
- Ownership is released after `--rc_owner_timeout_ms` (default 1000) with no
  accepted RC.
- The server writes at most one RC frame per `--tx_rate` slot. `--rc-hold-ms`
  optionally repeats the latest sample for a short gap; after that it stops.
- The server never generates center or failsafe values. Failsafe is downstream.
- `--tx_rate` is a maximum. Keep `--loop_hz` above it; a late slot is skipped,
  not sent in a burst.
- The joystick client maps stick positions to 1000–2000 µs; the shared CRSF
  codec packs them into the 11-bit channel field.

## Telemetry and status

- Every valid frame from the device goes to each `--telemetry` destination as a
  CRSF envelope and to each `--telemetry_raw` destination as a bare CRSF frame.
- `--telemetry` destinations also receive STATUS at `--status_hz` (default 5):
  RC age, owner session and seq, accepted/stale/foreign counts, and device RX
  age.
- The server sends nothing to a peer just because the peer sent something. To
  receive downlink, add your endpoint to `--telemetry` or `--telemetry_raw`. The
  one exception is a CONTROL_REPLY to its requester.
- Gaps in the server's per-kind `seq` are downlink loss. A new server session
  means the server restarted.
- RTT: `now - send_time(STATUS.rc_seq) - STATUS.rc_age_ms`.

## Configuration

CONTROL requests are JSON objects on the same port:

| Request | Result |
|---|---|
| `{"command":"devices"}` | devices found by a ping |
| `{"command":"info"}` | summary: device, key settings, status |
| `{"command":"params","offset":O}` | a page of the parameter table |
| `{"command":"get","parameter":P}` | one parameter |
| `{"command":"set","parameter":P,"value":V}` | write, then read back |
| `{"command":"command","parameter":P,"step":S}` | one command step |

`P` is a parameter name or decimal id.

`V` is a string:
- option text or index for a selection;
- a number otherwise.

`S` is `start`, `confirm`, `cancel`, or `poll`.

`params` is paginated:
- `offset` defaults to 0;
- the reply has `total` and `more`;
- loop until `more` is false.

A reply is `{"ok":true,"result":{...}}` or
`{"ok":false,"error":{"code":C,"message":"..."}}` with `code` one of `busy`,
`bad_request`, `failed`, `stale`.

One request runs at a time, server-wide:
- The request key is (peer, session, `seq`).
- A retry with the same key returns the cached reply and does not execute again.
- `busy`: another request is running; retry the same request later.

Commands run as steps:
- send `start`;
- `command_status` 3: send `confirm` or `cancel`;
- running: `poll` after `command_timeout` × 10 ms;
- `0`: finished.
`command_info` is the device's prompt or progress text.

ELRS parameter content lives in the module's firmware:
- names;
- choices;
- folders;
- rates.

crsfproxy does not ship or parse `elrs.lua`; it does not hard-code the table.
`params` and `get` read it live.
`info` is a convenience summary of a few well-known ELRS names. A renamed
parameter can appear as null there while remaining present in `params`.
New firmware settings therefore appear without a crsfproxy release.

Keep RC flowing while configuring an ELRS module; its handset link is more
reliable with traffic.

Raw non-RC CRSF frames are forwarded only from the RC owner, and they are paused
while a CONTROL request runs. They are a legacy path for a radio's own
configurator; its endpoint must be a `--telemetry` destination to see the
device's answers.

mLRS uses `--radio mlrs` and 400000 baud on the handset side. It speaks mBridge
in CRSF frames `0x81` and `0x82`; writes are live until the `Save` command.

The protocol has no authentication or encryption. Run it inside a VPN and bind
the server to the VPN interface (`--host`).

## Radio backends

`--radio elrs` is the default; `--radio mlrs` selects the mLRS backend. The
backend supplies its default baud, the frame addresses it accepts, its
configuration transport and service, and whether it polls the module's status.
The server loop, envelope, RC owner, scheduling, and telemetry forwarding are
shared.

## Clients

| Program | Role |
|---|---|
| `joystick_crsf.py` | pygame joystick to CRSF RC; one or more targets |
| `config_client.py` | CONTROL client: CLI and curses TUI |
| `crsf_rx_sim.py` | virtual CRSF receiver server for INAV SITL |

`config_client.py` provides `info`, `devices`, `params`, `get`, `set`,
`command`, and `tui`. `set` reads the value back. A command shows the device's
own confirmation prompt and progress, and can be cancelled. In the TUI, arrows
navigate, Enter edits or runs, Esc backs out or cancels a running command.

`crsf_rx_sim.py` impersonates a CRSF receiver at the UART boundary INAV sees.
It speaks the same envelope to its clients, takes CRSF RC in, and exposes its
simulation controls as CONTROL. See [`SITL_RECEIVER.md`](SITL_RECEIVER.md).

The contract is not crsfproxy-specific. The ESP32-P4 node, the C6 bay module,
and NetGCS are expected to implement the same user↔server link.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--radio` | `elrs` | radio backend: `elrs` or `mlrs` |
| `--device` | `/dev/ttyUSB0` | serial device or `socket://host:port` |
| `--baud` | 115200 ELRS / 400000 mLRS | CRSF serial baud; ignored over `socket://` |
| `--host` | `0.0.0.0` | UDP bind address; use the VPN interface |
| `--port` | `60000` | the one protocol port |
| `--tx_rate` | `100` | maximum RC frame rate (Hz) |
| `--loop_hz` | `250` | main-loop cap; keep above `--tx_rate` |
| `--rc-hold-ms` | `0` | repeat the latest RC sample for up to this long; 0 disables |
| `--telemetry` | none | envelope downlink destination; repeatable |
| `--telemetry_raw` | none | bare CRSF downlink destination; repeatable |
| `--rc_owner_timeout_ms` | `1000` | RC owner release time |
| `--status_hz` | `5` | STATUS rate |
| `--debug` | off | verbose frame and timing logging |

## Notes

- **Baud startup:** the module learns the handset baud from CRSF traffic. Open
  at the baud the module should use; after changing it, power-cycle the module.
- **`socket://`:** a byte stream, not packets. The bridge owns the UART baud and
  accepts one TCP client; a new connection replaces the old one.
- **ELRS high rates:** ELRS hides packet rates that do not fit the handset baud.
  921600 exposes them, but after a bridge boot the handset link needs continuous
  RC for about 10 s to settle. 400000 is the forgiving choice for configuration.
- **mLRS:** 400000 baud on the handset side.
- **CP2102:** some adapters behave badly around 400–460k baud.
- **Timing:** `--tx_rate` is a fixed slot grid. With the default `--loop_hz 250`,
  a `--tx_rate` above 250 is capped by the loop (300 measures about 244 Hz).
- **Security:** no authentication. Bind to the VPN interface and firewall the
  port.

## Version and compatibility

crsfproxy v0.4 implements protocol v1. Verified against ExpressLRS 4.0.1.
mLRS support is implemented; verify it against your firmware version.

The frame codec and the parameter read/write/command protocol are spec-stable;
the parameter content is read from the module, so new settings do not need a
code change. A firmware that changes the protocol itself would. "Tested" means
what has been exercised, not a hard gate.

### Changes from v0.3

| v0.3 | v1 |
|---|---|
| 40-byte RC packet (`t_ms`, 16 × µs, CRC32) | CRSF `RC_CHANNELS_PACKED` in an envelope |
| JSON or shell-style text on `--config_udp` (RC port + 1) | CONTROL JSON on the same port |
| full parameter table in one `params` reply | paginated `params`; `info` is a summary |
| raw telemetry to one `--telemetry_udp` address | `--telemetry` (envelope) and `--telemetry_raw` (bare), repeatable |
| "one RC source" as a usage rule | RC owner, enforced |
| — | STATUS |

v0.3 is the previous release and keeps the old wire format. v0.4 includes the
`socket://` block-read fix; use v0.4 for new work.

## Development

```bash
python3 -m unittest discover -s tests -v
```

```text
crsfproxy.py          server entry point
joystick_crsf.py      CRSF RC client
config_client.py      CONTROL client (CLI + TUI)
crsf_rx_sim.py        virtual receiver server for INAV SITL

lib/envelope.py       protocol v1 envelope and STATUS
lib/server.py         server loop, RC owner, CONTROL cache, downlink
lib/config.py         shared parameter/device objects and pagination
lib/transport.py      serial loop <-> configuration service transport
lib/radio.py          radio backend contract
lib/elrs_backend.py   ExpressLRS backend
lib/mlrs_backend.py   mLRS backend
lib/elrs_config.py    CRSF parameter protocol client
lib/crsf_protocol.py  CRSF codec
lib/schedule.py       deadline schedule

tests/                unit tests
examples/             launch scripts
```

The root Python files are the programs a user runs. Supporting code is in
`lib/`.

## License

[GPL-3.0](LICENSE), the same as ExpressLRS.

## Credits

- Yours truly
- Claude Fable 5, Opus 5.5
- Deepseek 4.1 Flash, Flash 1017
- Codex Sol 5.6

<div align="center">

# 📡 crsfproxy

**UDP ⇄ CRSF serial bridge for RC control, telemetry, and remote ExpressLRS / mLRS configuration**

[![Version](https://img.shields.io/badge/version-0.3-informational)](#-version-compatibility)
[![Python](https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Protocol](https://img.shields.io/badge/protocol-CRSF-orange)](https://github.com/tbs-fpv/tbs-crsf-spec/blob/main/crsf.md)
[![ExpressLRS](https://img.shields.io/badge/ExpressLRS-4.0.1-blueviolet)](https://www.expresslrs.org/)
[![mLRS](https://img.shields.io/badge/mLRS-mBridge-blue)](#mlrs)
[![Dependencies](https://img.shields.io/badge/deps-pyserial%20%2B%20pygame-brightgreen)](#-install)

*Fly your quad from anything that can send a UDP packet — and configure your TX module over the network, no handset screen required.*

> **Verified against ExpressLRS 4.0.1.** The wire protocol is stable across releases, but the configuration feature is only tested against this version — see [Version compatibility](#-version-compatibility).

</div>

---

## ✨ What it does

`crsfproxy` sits between a UDP RC client (`udp_crsf` format) and a CRSF serial port, and does three jobs at once:

| | Feature | Description |
|---|---|---|
| 🎮 | **RC bridge** | Receives UDP RC packets and sends CRSF `RC_CHANNELS_PACKED` frames to the transmitter, at most `--tx_rate` per second |
| 📊 | **Telemetry forwarding** | Forwards inbound CRSF telemetry frames over UDP, unchanged — plugs straight into [MWP](https://github.com/stronnag/mwptools) |
| 🛠️ | **Remote TX configuration** | Speaks the TX module's own configuration protocol directly (ExpressLRS device-parameter protocol, or mLRS mBridge-over-CRSF), exposing its live configuration table on a second UDP port for `config_client.py`. No `elrs.lua`, no handset — see [why](#-no-elrslua-required) |

The repository also ships [`crsf_rx_sim.py`](#-virtual-crsf-receiver-for-inav-sitl), a virtual CRSF **receiver** for INAV SITL and hardware-in-the-loop testing that takes the same UDP RC packets.

```
┌──────────────┐  UDP :60000   ┌────────────┐  serial (CRSF)  ┌─────────────┐    RF    ┌──────────┐
│  RC source   │──────RC──────▶│            │────────────────▶│  ELRS/mLRS  │─────────▶│ Receiver │
│ (joystick,   │               │  crsfproxy │◀────────────────│  TX module  │◀─────────│  + FC    │
│  GCS, ...)   │               │            │    telemetry    └─────────────┘          └──────────┘
└──────────────┘               └─────┬──────┘
                                     │ ▲
                      UDP :40042 ◀───┘ └───▶ UDP :60001
                      (raw telemetry          (config API:
                       → MWP)                  info / get / set / bind)
```

The network side is intentionally simple. One `crsfproxy` process represents one CRSF endpoint. If a client controls several transmitters, the client is responsible for knowing their UDP endpoints.

## 📦 Install

Self-contained — the CRSF codec and the ELRS/mLRS parameter clients live in this repo, no sibling checkout required.

```bash
pip install pyserial          # the proxy
pip install pygame            # only for joystick_crsf.py
```

## 🚀 Quick start

Defaults:

```text
device      /dev/ttyUSB0
baud        115200
RC UDP      0.0.0.0:60000
config UDP  RC port + 1 (60001 by default)
RC hold     disabled
radio       elrs
```

Flags change those defaults; they are not required to enable the normal features.

```bash
# Start the proxy
python3 crsfproxy.py

# Send joystick RC to it
python3 joystick_crsf.py --target 127.0.0.1

# Open the transmitter configuration UI
python3 config_client.py tui
```

A full example: ELRS at 921600 baud (exposes the high packet rates, see [Notes](#-notes--gotchas)), telemetry streamed raw to MWP on `:40042`, joystick at 75 Hz:

```bash
python3 crsfproxy.py --device /dev/ttyUSB0 --baud 921600 \
    --host 0.0.0.0 --port 60000 \
    --loop_hz 250 --tx_rate 100 \
    --telemetry_udp 127.0.0.1:40042 --config_udp 60001

# MWP: listen for UDP telemetry
mwp -d udp://:40042 -a

# RC source (joystick example)
python3 joystick_crsf.py --target 127.0.0.1 --port 60000 --rate 75
```

## 🎮 RC input

### 📨 Wire format

**RC (client → proxy)** — 40-byte little-endian UDP payload:

```
┌────────────┬───────────────────────────────────┬─────────────────┐
│ uint32     │ 16 × uint16                       │ uint32          │
│ t_ms       │ channels (microseconds)           │ crc32(payload)  │
└────────────┴───────────────────────────────────┴─────────────────┘
  4 bytes       32 bytes                            4 bytes
```

The CRC32 covers the first 36 bytes: timestamp plus 16 channels.

### RC hold

RC hold is **disabled by default**.

With the default `--rc-hold-ms 0`, each fresh UDP RC sample is forwarded once, subject to `--tx_rate` as a maximum output rate. If the UDP RC source stops producing fresh samples, crsfproxy stops emitting CRSF RC channel frames. It does not generate substitute channel values and it does not implement a second aircraft failsafe policy.

If you want to smooth very short UDP/input gaps, opt into a small hold window explicitly:

```bash
python3 crsfproxy.py --rc-hold-ms 80
```

During that window, crsfproxy may repeat the latest valid RC sample. Once the hold expires, it stops emitting RC frames until fresh UDP RC arrives again.

Actual RF/receiver/flight-controller failsafe behavior remains entirely downstream of crsfproxy.

### 🕹️ joystick_crsf.py

`joystick_crsf.py` reads a pygame joystick and sends the RC packet to one or more proxies (`--rate`, default 50 Hz). The channel mapping for the TX12/RadioMaster and the generic gamepad is documented at the top of `get_joystick_state()` in the script.

## 🔀 Multiple transmitters

Run one proxy per transmitter. Different processes on the same host use different UDP ports:

```bash
python3 crsfproxy.py \
    --device /dev/ttyACM0 \
    --port 60000
```

```bash
python3 crsfproxy.py \
    --device /dev/ttyUSB0 \
    --port 60010
```

Because the configuration port defaults to RC port + 1, these automatically use config ports `60001` and `60011`.

`joystick_crsf.py` can send the same controller state to several proxies:

```bash
python3 joystick_crsf.py \
    --target 127.0.0.1:60000 \
    --target 127.0.0.1:60010
```

The equivalent grouped form is also accepted:

```bash
python3 joystick_crsf.py \
    --target 127.0.0.1 --port 60000 \
    --target 127.0.0.1 --port 60010
```

There is no extra logical system ID in the UDP protocol. The UDP destination identifies the proxy.

## 📊 Telemetry

`--telemetry_udp host:port` forwards each valid inbound CRSF frame unchanged, one frame per datagram.

```bash
python3 crsfproxy.py --telemetry_udp 127.0.0.1:40042
mwp -d udp://:40042 -a
```

If a program collects telemetry from several proxies, that program is responsible for assigning separate endpoints or otherwise knowing which proxy each stream belongs to.

## 🛠️ Remote TX configuration

The configuration service is on by default at RC port + 1 (`60001`). `DEVICE_INFO` and `PARAMETER_*` frames are inserted into the same scheduled CRSF writes as RC, **while RC traffic keeps flowing**, exactly where a handset would place them, at the pacing the module dictates via `RADIO_ID` sync.

> [!NOTE]
> This interface only works when the serial device is connected to a transmitter's handset (JR-bay) CRSF port.

### 🚫 No `elrs.lua` required

This is the part people expect to be wrong, so here is precisely what happens.

**`elrs.lua` is not the configuration interface. It is a *client* of it.** On an EdgeTX/OpenTX handset, the settings you see are not defined in the Lua script — they live in the **TX module's firmware**. `elrs.lua` is a generic CRSF parameter browser: it pings the module, reads back whatever parameter table the firmware streams (`DEVICE_INFO` → chunked `PARAMETER_SETTINGS_ENTRY` reads), and renders it. The radio runs the Lua only because *the radio* is what has the screen and the input keys.

crsfproxy is a **second, independent client of that same firmware protocol.** It issues the same `DEVICE_PING` / `PARAMETER_READ` / `PARAMETER_WRITE` / command frames and reads back the same entries. The module is the single source of truth in both cases.

So there is nothing to emulate and nothing to bundle:

- We do **not** parse, ship, or reimplement `elrs.lua`. It never runs here.
- We do **not** hard-code parameter names, choice lists, folders, unit strings, or rate tables. Every one of those is decoded live from the bytes the module sends (`read_all()` loops `1..parameter_count`, where `parameter_count` comes from the module's own `DEVICE_INFO`).
- We are **not** EdgeTX. We don't run a Lua VM to produce an interface — we *are* a consumer of the interface the module already exposes on the wire.

The only things crsfproxy actually implements are the two layers that are defined by the CRSF spec and are version-stable: the frame codec, and the parameter read/write/command/chunking state machine. That is the same protocol `elrs.lua` implements in Lua — because both talk to the same firmware.

| Layer | Where it lives | Changes per ELRS version? | In this repo? |
|---|---|---|---|
| Parameter **content** — names, choices, folders, rate tables | TX module firmware | Yes | **No** — read live off the wire |
| Parameter **protocol** — ping/read/write/command/chunk | CRSF spec | No (spec-stable) | Yes (`lib/elrs_config.py`) |
| Frame **codec** — CRSF framing & types | CRSF spec | No (spec-stable) | Yes (`lib/crsf_protocol.py`) |

### One-shot commands

The default endpoint is `127.0.0.1:60001`.

```bash
python3 config_client.py info
python3 config_client.py devices
python3 config_client.py params
python3 config_client.py get "Packet Rate"
python3 config_client.py set "RF Band" "2.4GHz"
python3 config_client.py set "Packet Rate" "333Hz Full(-105dBm)"
python3 config_client.py command Bind --confirm
```

For another proxy, or a proxy on another host:

```bash
python3 config_client.py --port 60011 info
python3 config_client.py --host proxy-host --port 60001 info
```

Writes are read back for verification. Commands with side effects require `--confirm`.

### Interactive TUI

```bash
python3 config_client.py --host proxy-host --port 60001 tui
```

A curses browser over the live parameter hierarchy read from the module, in your terminal's default colors:

| Key | Action |
|---|---|
| <kbd>↑</kbd> / <kbd>↓</kbd> | Select |
| <kbd>→</kbd> / <kbd>Enter</kbd> | Open folder · edit value · run command |
| <kbd>←</kbd> / <kbd>Esc</kbd> | Back to parent (exits at root) |
| <kbd>r</kbd> | Reload the table |

Selection parameters get a choice list; numeric parameters get a text prompt and are **verified by reading them back**. Empty selection placeholders sent by ELRS are hidden without disturbing their underlying selection indices. A persistent banner shows the latest ELRS status message, connection state, flags, and good/bad packet counts.

<details>
<summary><b>What <code>info</code> returns</b></summary>

- `DEVICE_INFO` model and version
- All current parameter values reported by the module (band, packet rate, link mode, model ID, firmware-provided info fields, …)
- The `RADIO_ID` handset rate
- Latest ELRS status: connection/binding state, flags, packet counts, message
- Firmware hash, when the transmitter exposes it as an INFO or STRING parameter

</details>

### 🔌 UDP API

The configuration interface is ordinary UDP request/reply: UTF-8 datagrams on the config port. Replies return to the source address and port of each request.

Accepts JSON:

```json
{"command":"set","parameter":"Packet Rate","value":"333Hz Full(-105dBm)"}
```

…or shell-style UTF-8 commands:

```text
info
devices
params
get "Packet Rate"
set "Packet Rate" "333Hz Full(-105dBm)"
command "Bind" --confirm
```

Every response is JSON:

```json
{"ok":true,"result":{"verified":true}}
```

> [!IMPORTANT]
> Only one configuration operation is processed at a time. Reads and writes can take several seconds — ELRS parameter entries are chunked and every write is read back for verification. There are **no automatic UDP retries**, because repeating a command like `Bind` can have side effects.

## 📻 Radio selection

`--radio` selects only the transmitter-module configuration backend. The RC bridge, telemetry forwarding, serial scheduling and UDP configuration API are shared.

### ExpressLRS

ExpressLRS is the default:

```bash
python3 crsfproxy.py
```

Use a different handset-side baud when required by the module or packet rate:

```bash
python3 crsfproxy.py --baud 921600
```

The ELRS configuration backend uses the standard CRSF device/parameter protocol used by `elrs.lua` (see [No `elrs.lua` required](#-no-elrslua-required)).

### mLRS

```bash
python3 crsfproxy.py --radio mlrs
```

mLRS uses **400000 baud** on the radio-side CRSF interface. When `--radio mlrs` is selected and `--baud` is omitted, the mLRS backend supplies that value.

mLRS configuration uses the official mBridge command protocol carried in CRSF frame types `0x81` and `0x82`; the backend also accepts mLRS replies addressed to CRSF radio address `0xEA`. The same `config_client.py` commands work against the mLRS backend, including Save and Bind commands.

Example:

```bash
python3 config_client.py get "Tx Ch Source"
python3 config_client.py set "Tx Ch Source" crsf
python3 config_client.py command Save --confirm
```

mLRS parameter writes are live changes and are not persistent until Save, matching the official mLRS Lua configurator.

## 🧪 INAV SITL

### crsfproxy against a SITL UART

INAV SITL exposes virtual UARTs as TCP ports:

```text
UART1 -> TCP 5760
UART2 -> TCP 5761
UART3 -> TCP 5762
...
```

Configure the selected SITL UART as a normal CRSF serial receiver, then point `crsfproxy` directly at it using pySerial's `socket://` transport.

Example for UART3:

```bash
python3 crsfproxy.py --device socket://127.0.0.1:5762
```

Then send RC normally:

```bash
python3 joystick_crsf.py --target 127.0.0.1
```

No PTY or `socat` bridge is required.

### 🛰️ Virtual CRSF receiver for INAV SITL

`crsf_rx_sim.py` impersonates an ELRS/CRSF **receiver** at the UART boundary INAV sees. It takes the same 40-byte UDP RC packet, sends `RC_CHANNELS_PACKED` and `LINK_STATISTICS` to INAV over TCP, reads telemetry back, and can simulate RC-source timeout, RF loss and TCP serial loss separately. Run one process per receiver for dual-receiver testing.

```bash
python3 crsf_rx_sim.py --endpoint tcp://127.0.0.1:5763
```

Full documentation: [`SITL_RECEIVER.md`](SITL_RECEIVER.md). Two-receiver launch script: [`examples/dual_receiver_commands.sh`](examples/dual_receiver_commands.sh).

## ⚙️ Options

| Flag | Default | Description |
|---|---|---|
| `--radio` | `elrs` | Radio backend: `elrs` or `mlrs` |
| `--device` | `/dev/ttyUSB0` | Serial device or pySerial URL, e.g. `socket://127.0.0.1:5762` |
| `--baud` | `115200` | CRSF serial baudrate; mLRS defaults to `400000` |
| `--host` | `0.0.0.0` | UDP bind host for RC packets |
| `--port` | `60000` | UDP port for RC packets |
| `--tx_rate` | `100` | Maximum RC frame rate (Hz) |
| `--loop_hz` | `250` | Max main loop rate (Hz) — caps main-loop CPU |
| `--rc-hold-ms` | `0` | Repeat the latest RC sample for up to this many ms after input stops; `0` disables hold |
| `--telemetry_udp` | — | Send raw CRSF telemetry to `host:port` (e.g. MWP) |
| `--config_udp` | RC port + 1 | UDP port for TX `elrs.lua` configuration commands |
| `--debug` | off | Verbose serial/RC/telemetry logging |

## 📝 Notes & gotchas

- **Baud startup:** The serial port opens directly at `--baud` and immediately streams RC frames. ExpressLRS detects the handset baud from that continuous CRSF traffic.
- 📉 ELRS hides packet rates that can't fit over the configured handset baud. At 115200 the TX reports `Baud rate too low` and blanks rates such as 333 Hz and 500 Hz — use 921600 to expose the full high-rate table.
- mLRS only supports 400K baud on its handset-side CRSF interface.
- 🐛 Some CP2102 USB serial adapters behave badly around 400–460K baud.
- ⏱️ **RC timing:** RC slots run on a fixed deadline grid (slot *n* due at start + *n* / `--tx_rate`, `lib/schedule.py`), so the slot rate equals `--tx_rate` as long as `--loop_hz` is higher; each frame goes out up to one loop period after its deadline, and a late slot is skipped rather than sent in a burst. With the default `--loop_hz 250`, `--tx_rate` above 250 is capped by the loop (measured: 300 → about 244 Hz); raise `--loop_hz` with it.

> [!WARNING]
> The configuration port has **no authentication**. Bind it to a trusted interface or restrict it with a firewall.

## 📌 Version compatibility

**This is crsfproxy v0.3. Developed and verified against ExpressLRS 4.0.1.**

The configuration feature depends only on protocol layers that are defined by the [CRSF specification](https://github.com/tbs-fpv/tbs-crsf-spec/blob/main/crsf.md) and are stable across ELRS releases — the frame codec and the device-parameter read/write/command/chunking exchange. It does **not** depend on any per-version parameter list, because that list is read live from the module (see [No `elrs.lua` required](#-no-elrslua-required)). New, renamed, or reordered *settings* on a different firmware version therefore need no changes here — they simply appear.

What *would* require a code change is a change to the protocol itself: a new `PARAMETER_TYPE`, an altered `DEVICE_INFO`/`PARAMETER_SETTINGS_ENTRY` layout, or new command-status semantics. Those are rare and spec-governed, but they are the reason this is pinned to a tested version rather than claimed as universally compatible. If you run a different ELRS version and everything reads and writes back correctly, it works on your version too; the 4.0.1 pin is a statement of what has actually been exercised, not a hard gate.

## 🗂️ Repository layout

The root Python files are the programs a user runs. Supporting code lives under `lib/`.

```text
crsfproxy.py          run one CRSF proxy (--radio selects the backend)
joystick_crsf.py      send joystick RC to one or more proxies
config_client.py      inspect and configure one transmitter (CLI + TUI)
crsf_rx_sim.py        virtual CRSF receiver for INAV SITL

lib/
    __init__.py
    elrs_backend.py   proxy core + ExpressLRS configuration service
    mlrs_backend.py   mLRS mBridge configuration backend
    elrs_config.py    ELRS device-parameter protocol client
    crsf_protocol.py  CRSF frame codec
    rc_packet.py      the 40-byte UDP RC packet
    schedule.py       fixed-rate deadline schedule (proxy + simulator)

examples/
    dual_receiver_commands.sh

tests/
    ...
```

If a Python file is in the repository root, it is something a user runs.

Run the tests from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

<div align="center">

# 📡 crsfproxy

**UDP ⇄ CRSF serial bridge for RC control, telemetry, and remote ExpressLRS configuration**

[![Python](https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Protocol](https://img.shields.io/badge/protocol-CRSF-orange)](https://github.com/tbs-fpv/tbs-crsf-spec/blob/main/crsf.md)
[![ExpressLRS](https://img.shields.io/badge/ExpressLRS-4.0.1-blueviolet)](https://www.expresslrs.org/)
[![Dependencies](https://img.shields.io/badge/deps-pyserial%20%2B%20pygame-brightgreen)](#-install)

*Fly your quad from anything that can send a UDP packet — and configure your TX module over the network, no handset screen required.*

> **Verified against ExpressLRS 4.0.1.** The wire protocol is stable across releases, but the configuration feature is only tested against this version — see [Version compatibility](#-version-compatibility).

</div>

---

## ✨ What it does

`crsfproxy` sits between a UDP RC client (`udp_crsf` format) and a CRSF serial port, and does three jobs at once:

| | Feature | Description |
|---|---|---|
| 🎮 | **RC bridge** | Receives UDP RC packets and streams `RC_CHANNELS_PACKED` frames to the radio at a configurable rate |
| 📊 | **Telemetry forwarding** | Forwards raw CRSF telemetry frames over UDP, unchanged — plugs straight into [MWP](https://github.com/stronnag/mwptools) |
| 🛠️ | **Remote ELRS config** | Speaks the CRSF device-parameter protocol to the TX module directly, exposing its live configuration table over a separate UDP port. No `elrs.lua`, no handset — see [why](#-no-elrslua-required) |

```
┌──────────────┐  UDP :60000   ┌────────────┐  serial (CRSF)  ┌─────────────┐    RF    ┌──────────┐
│  RC source   │──────RC──────▶│            │────────────────▶│  ELRS TX    │─────────▶│ Receiver │
│ (joystick,   │               │  crsfproxy │◀────────────────│  module     │◀─────────│  + FC    │
│  GCS, ...)   │               │            │    telemetry    └─────────────┘          └──────────┘
└──────────────┘               └─────┬──────┘
                                     │ ▲
                      UDP :40042 ◀───┘ └───▶ UDP :60001
                      (raw telemetry          (ELRS config API:
                       → MWP)                  info / get / set / bind)
```

## 🚀 Quick start

```bash
# Start the proxy (telemetry streamed raw to MWP on :40042)
python crsfproxy.py --device /dev/ttyUSB0 --baud 460800 \
    --host 0.0.0.0 --port 60000 \
    --loop_hz 250 --tx_rate 100 \
    --telemetry_udp 127.0.0.1:40042 --config_udp 60001

# MWP: listen for UDP telemetry
mwp -d udp://:40042 -a

# RC source (joystick example)
python joystick_crsf.py --target 127.0.0.1 --port 60000 --rate 75
```

## 📦 Install

Self-contained — the CRSF codec and ELRS parameter client live in this repo, no sibling checkout required.

```bash
pip install pyserial          # the proxy
pip install pygame            # only for joystick_crsf.py
```

## 🎛️ Remote ELRS configuration

`--config_udp PORT` enables TX-module configuration **while RC traffic keeps flowing** — `DEVICE_INFO` and `PARAMETER_*` frames are inserted into the same scheduled CRSF writes, exactly where a handset would place them, at the pacing the module dictates via `RADIO_ID` sync.

> [!NOTE]
> This interface only works when the serial device is connected to an ELRS transmitter's handset (JR-bay) CRSF port.

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
| Parameter **protocol** — ping/read/write/command/chunk | CRSF spec | No (spec-stable) | Yes (`elrs_config.py`) |
| Frame **codec** — CRSF framing & types | CRSF spec | No (spec-stable) | Yes (`crsf_protocol.py`) |

### One-shot commands

```bash
python config_client.py --port 60001 info
python config_client.py --port 60001 params
python config_client.py --port 60001 get "Packet Rate"
python config_client.py --port 60001 set "RF Band" "2.4GHz"
python config_client.py --port 60001 set "Packet Rate" "333Hz Full(-105dBm)"
python config_client.py --port 60001 command Bind --confirm
```

### Interactive TUI

```bash
python config_client.py --host proxy-host --port 60001 tui
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

Accepts JSON:

```json
{"command":"set","parameter":"Packet Rate","value":"333Hz Full(-105dBm)"}
```

…or shell-style UTF-8 commands:

```text
info
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

## 🧪 Virtual pair for SITL / testing

```bash
socat -d -d pty,raw,echo=0 pty,raw,echo=0
```

## 📨 Wire formats

**RC (client → proxy)** — 40-byte little-endian UDP payload:

```
┌────────────┬───────────────────────────────────┬─────────────────┐
│ uint32     │ 16 × uint16                       │ uint32          │
│ t_ms       │ channels (microseconds)           │ crc32(payload)  │
└────────────┴───────────────────────────────────┴─────────────────┘
  4 bytes       32 bytes                            4 bytes
```

CRC32 covers the first 36 bytes.

**Telemetry (proxy → client)** — raw CRSF frames, sent unchanged to `--telemetry_udp` (`host:port`).

**Configuration** — ordinary UTF-8 UDP datagrams on `--config_udp`. Replies return to the source address and port of each request.

## ⚙️ Options

| Flag | Default | Description |
|---|---|---|
| `--device` | `/dev/ttyUSB0` | Serial device |
| `--baud` | `115200` | Negotiated serial baudrate; startup is always 115200 |
| `--host` | `0.0.0.0` | UDP bind host for RC packets |
| `--port` | `60000` | UDP port for RC packets |
| `--tx_rate` | `100` | RC frame rate (Hz) |
| `--loop_hz` | `250` | Max main loop rate (Hz) — caps main-loop CPU |
| `--failsafe_time_ms` | `1000` | Enter failsafe after this much RC silence |
| `--failsafe_channels_us` | see below | 16 space-separated failsafe channel values (µs) |
| `--telemetry_udp` | — | Send raw CRSF telemetry to `host:port` (e.g. MWP) |
| `--config_udp` | — | UDP port for TX `elrs.lua` configuration commands |
| `--debug` | off | Verbose RC/telemetry logging |

### 🪂 Failsafe

If RC updates stop for **less** than `--failsafe_time_ms`, the last valid channels are repeated. Beyond that, `--failsafe_channels_us` is sent (default: `1500,1500,900,1500,900,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500`). Default RC init sets throttle/arm low.

Radio-link failsafe (TX↔RX loss) is handled by the receiver, not this proxy.

## 📝 Notes & gotchas

- **Baud negotiation:** `--baud 115200` uses the bootstrap speed directly. Any other requested baud is negotiated after opening at 115200 — the proxy sends the CRSF General Speed Proposal command, validates the accepted response, and only then changes the local serial baudrate, per the [CRSF protocol specification](https://github.com/tbs-fpv/tbs-crsf-spec/blob/main/crsf.md).
- 🐛 On CP2102 adapters, 400–460K baud is buggy.
- 📉 ELRS hides packet rates that can't fit over the configured handset baud. At 115200 the TX reports `Baud rate too low` and blanks rates such as 333 Hz and 500 Hz — use 460800 to expose the full high-rate table.

> [!WARNING]
> The configuration port has **no authentication**. Bind it to a trusted interface or restrict it with a firewall.

## 📌 Version compatibility

**Developed and verified against ExpressLRS 4.0.1.**

The configuration feature depends only on protocol layers that are defined by the [CRSF specification](https://github.com/tbs-fpv/tbs-crsf-spec/blob/main/crsf.md) and are stable across ELRS releases — the frame codec and the device-parameter read/write/command/chunking exchange. It does **not** depend on any per-version parameter list, because that list is read live from the module (see [No `elrs.lua` required](#-no-elrslua-required)). New, renamed, or reordered *settings* on a different firmware version therefore need no changes here — they simply appear.

What *would* require a code change is a change to the protocol itself: a new `PARAMETER_TYPE`, an altered `DEVICE_INFO`/`PARAMETER_SETTINGS_ENTRY` layout, or new command-status semantics. Those are rare and spec-governed, but they are the reason this is pinned to a tested version rather than claimed as universally compatible. If you run a different ELRS version and everything reads and writes back correctly, it works on your version too; the 4.0.1 pin is a statement of what has actually been exercised, not a hard gate.

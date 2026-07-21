<div align="center">

# 📡 crsfproxy

**UDP ⇄ CRSF serial bridge for RC control, telemetry, and remote ExpressLRS configuration**

[![Python](https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Protocol](https://img.shields.io/badge/protocol-CRSF-orange)](https://github.com/tbs-fpv/tbs-crsf-spec/blob/main/crsf.md)
[![ExpressLRS](https://img.shields.io/badge/ExpressLRS-compatible-blueviolet)](https://www.expresslrs.org/)
[![Dependencies](https://img.shields.io/badge/deps-pyserial%20%2B%20pygame-brightgreen)](#-install)

*Fly your quad from anything that can send a UDP packet — and configure your TX module over the network, no handset screen required.*

</div>

---

## ✨ What it does

`crsfproxy` sits between a UDP RC client (`udp_crsf` format) and a CRSF serial port, and does three jobs at once:

| | Feature | Description |
|---|---|---|
| 🎮 | **RC bridge** | Receives UDP RC packets and streams `RC_CHANNELS_PACKED` frames to the radio at a configurable rate |
| 📊 | **Telemetry forwarding** | Forwards raw CRSF telemetry frames over UDP, unchanged — plugs straight into [MWP](https://github.com/stronnag/mwptools) |
| 🛠️ | **Remote ELRS config** | Exposes the transmitter's `elrs.lua` configuration interface over a separate UDP port — speaks the same dynamic parameter protocol the real Lua script uses |

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

`--config_udp PORT` enables TX-module configuration **while RC traffic keeps flowing** — DEVICE/PARAMETER frames are inserted into the same scheduled CRSF writes, matching the handset-side behavior used by `elrs.lua` and elrsbuddy.

> [!NOTE]
> This interface only works when the serial device is connected to an ELRS transmitter's handset CRSF port.

The transmitter supplies its **live** parameter names, types, choices, folders, info strings, and commands. Nothing is hard-coded from a local Lua file — the client speaks the same dynamic protocol the proper ELRS Lua script uses.

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

A curses browser over the live Lua parameter hierarchy, in your terminal's default colors:

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
- All current Lua parameter values (band, packet rate, link mode, model ID, firmware-provided info fields, …)
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

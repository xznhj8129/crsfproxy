# crsfproxy

UDP <-> CRSF bridge for using ExpressLRS or mLRS transmitter modules from a computer.

`crsfproxy` does three jobs:

- receives RC channel state over UDP and sends CRSF `RC_CHANNELS_PACKED` frames to the transmitter;
- forwards inbound CRSF telemetry over UDP unchanged;
- exposes transmitter configuration on a second UDP port for `config_client.py`.

The network side is intentionally simple. One `crsfproxy` process represents one CRSF endpoint. If a client controls several transmitters, the client is responsible for knowing their UDP endpoints.

## Install

```bash
pip install pyserial
pip install pygame  # only needed for joystick_crsf.py
```

## Normal use

Defaults:

```text
device      /dev/ttyUSB0
baud        115200
RC UDP      0.0.0.0:60000
config UDP  RC port + 1 (60001 by default)
radio       elrs
```

Start the proxy:

```bash
python3 crsfproxy.py
```

Send joystick RC to it:

```bash
python3 joystick_crsf.py --target 127.0.0.1
```

Open the transmitter configuration UI:

```bash
python3 config_client.py tui
```

Flags change those defaults; they are not required to enable the normal features.

## Multiple transmitters

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

## RC input

The RC UDP packet is 40 bytes, little-endian:

```text
uint32 t_ms
uint16 channels_us[16]
uint32 crc32
```

The CRC32 covers the first 36 bytes: timestamp plus 16 channels.

If RC updates stop, the proxy repeats the last valid channels until `--failsafe_time_ms` expires, then sends the configured failsafe channels. RF-link failsafe remains the receiver's job.

## Telemetry

`--telemetry_udp host:port` forwards each valid inbound CRSF frame unchanged.

Example:

```bash
python3 crsfproxy.py --telemetry_udp 127.0.0.1:40042
```

If a program collects telemetry from several proxies, that program is responsible for assigning separate endpoints or otherwise knowing which proxy each stream belongs to.

## Configuration client

The configuration interface is ordinary UDP request/reply. The default endpoint is `127.0.0.1:60001`.

```bash
python3 config_client.py info
python3 config_client.py devices
python3 config_client.py params
python3 config_client.py get "Packet Rate"
python3 config_client.py set "RF Band" "2.4GHz"
python3 config_client.py tui
```

For another proxy on the same host:

```bash
python3 config_client.py --port 60011 info
```

Writes are read back for verification. Commands with side effects require `--confirm`.

## Radio selection

### ExpressLRS

ExpressLRS is the default:

```bash
python3 crsfproxy.py
```

Use a different handset-side baud when required by the module or packet rate:

```bash
python3 crsfproxy.py --baud 921600
```

The ELRS configuration backend uses the standard CRSF device/parameter protocol used by `elrs.lua`.

### mLRS

```bash
python3 crsfproxy.py --radio mlrs
```

mLRS uses 400000 baud on the radio-side CRSF interface. When `--radio mlrs` is selected and `--baud` is omitted, the mLRS backend supplies that value.

mLRS configuration uses its mBridge-over-CRSF protocol. The same `config_client.py` commands work against the mLRS backend, including Save and Bind commands.

Example:

```bash
python3 config_client.py get "Tx Ch Source"
python3 config_client.py set "Tx Ch Source" crsf
python3 config_client.py command Save --confirm
```

mLRS parameter writes are live changes and are not persistent until Save.

## INAV SITL

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

## Main options

| Flag | Default | Meaning |
|---|---:|---|
| `--radio` | `elrs` | Radio backend: `elrs` or `mlrs` |
| `--device` | `/dev/ttyUSB0` | Serial device or pySerial URL |
| `--baud` | `115200` | Serial baud; mLRS defaults to `400000` |
| `--host` | `0.0.0.0` | UDP bind address |
| `--port` | `60000` | RC input UDP port |
| `--tx_rate` | `100` | CRSF RC frame rate |
| `--loop_hz` | `250` | Main loop ceiling |
| `--failsafe_time_ms` | `1000` | RC silence before proxy failsafe |
| `--telemetry_udp` | off | Raw CRSF telemetry destination |
| `--config_udp` | RC port + 1 | Radio configuration UDP port |
| `--debug` | off | Verbose serial/RC/telemetry logging |

## Repository layout

The root Python files are the programs a user runs. Supporting code lives under `lib/`.

```text
crsfproxy.py          run one CRSF proxy
joystick_crsf.py      send joystick RC to one or more proxies
config_client.py      inspect and configure one transmitter

lib/
    __init__.py
    elrs_backend.py
    mlrs_backend.py
    elrs_config.py
    crsf_protocol.py

tests/
    ...
```

If a Python file is in the repository root, it is something a user runs.

## Notes

- mLRS only supports 400K baud on its handset-side CRSF interface.
- Some CP2102 USB serial adapters behave badly around 400-460K baud.
- The configuration UDP interface has no authentication. Bind it to a trusted interface or firewall it.

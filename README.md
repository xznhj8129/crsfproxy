<div align="center">

# 📡 crsfproxy

**UDP ⇄ CRSF bridge for remote RC, telemetry, and radio configuration**

Supports **ExpressLRS** and **mLRS** transmitter modules.

</div>

## What it is

`crsfproxy` makes a CRSF transmitter module usable from a computer instead of a normal RC handset.

It does three things at once:

- receives RC channel values over UDP and sends normal CRSF `RC_CHANNELS_PACKED` frames to the transmitter;
- forwards inbound CRSF telemetry over UDP unchanged;
- exposes the transmitter's configuration interface over a second UDP port so `config_client.py` can inspect and change radio settings remotely.

```text
 RC source                              radio link
    |                                      |
    | UDP                                  |
    v                                      v
+-----------+        CRSF serial      +----------+       RF       +----------+
| crsfproxy | <---------------------> | TX module| <------------> | receiver |
+-----------+                         +----------+                 +----------+
    |
    +---- raw CRSF telemetry UDP
    |
    +---- configuration UDP <----> config_client.py
```

The RC bridge and telemetry path are common to both radio families. `--radio` only selects the transmitter-configuration backend.

## Install

```bash
pip install pyserial
pip install pygame  # only needed for joystick_crsf.py
```

## Radio selection

### ExpressLRS

ExpressLRS is the default, so old commands still work:

```bash
python3 crsfproxy.py \
    --radio elrs \
    --device /dev/ttyUSB0 \
    --baud 921600 \
    --host 0.0.0.0 \
    --port 60000 \
    --telemetry_udp 127.0.0.1:40042 \
    --config_udp 60001
```

`--radio elrs` uses the standard CRSF device/parameter protocol used by `elrs.lua`: `DEVICE_INFO`, `PARAMETER_READ`, `PARAMETER_WRITE`, and CRSF commands. Parameter names, options, folders, and values are read live from the transmitter rather than hard-coded in `crsfproxy`.

ExpressLRS handset baud depends on the module and desired packet rates. `921600` is useful when high ELRS rates are required.

### mLRS

```bash
python3 crsfproxy.py \
    --radio mlrs \
    --device /dev/ttyUSB0 \
    --host 0.0.0.0 \
    --port 60000 \
    --telemetry_udp 127.0.0.1:40042 \
    --config_udp 60001
```

mLRS uses **400000 baud** on the radio-side CRSF interface. When `--radio mlrs` is selected and `--baud` is omitted, `crsfproxy` automatically uses `400000`.

mLRS configuration is not the standard CRSF parameter protocol. Its Lua configurator tunnels the mBridge command protocol through CRSF frame types `0x81` and `0x82`; the mLRS backend implements that same protocol, including live parameter discovery, multi-frame LIST options, writes, bind commands, bootloader commands, and Save.

mLRS parameter writes take effect immediately but are **not persistent until Save**:

```bash
python3 config_client.py --port 60001 command Save --confirm
```

## Configuration client

`config_client.py` is radio-agnostic. The same commands work against whichever backend `crsfproxy` is running:

```bash
python3 config_client.py --port 60001 info
python3 config_client.py --port 60001 devices
python3 config_client.py --port 60001 params
python3 config_client.py --port 60001 get "Packet Rate"
python3 config_client.py --port 60001 set "RF Band" "2.4GHz"
python3 config_client.py --port 60001 tui
```

For mLRS, for example:

```bash
python3 config_client.py --port 60001 get "Tx Ch Source"
python3 config_client.py --port 60001 set "Tx Ch Source" crsf
python3 config_client.py --port 60001 command Save --confirm
```

Writes are read back for verification. Commands with side effects require `--confirm`.

## RC input

The RC UDP packet is 40 bytes, little-endian:

```text
uint32 t_ms
uint16 channels_us[16]
uint32 crc32
```

The CRC32 covers the first 36 bytes.

Example joystick source:

```bash
python3 joystick_crsf.py --target 127.0.0.1 --port 60000 --rate 75
```

If RC updates stop, the proxy repeats the last valid channels until `--failsafe_time_ms` expires, then sends the configured failsafe channels. RF-link failsafe remains the receiver's job.

## Telemetry

`--telemetry_udp host:port` forwards valid inbound CRSF frames unchanged.

Example with MWP:

```bash
python3 crsfproxy.py \
    --radio mlrs \
    --device /dev/ttyUSB0 \
    --telemetry_udp 127.0.0.1:40042

mwp -d udp://:40042 -a
```

## Main options

| Flag | Default | Meaning |
|---|---:|---|
| `--radio` | `elrs` | Radio backend: `elrs` or `mlrs` |
| `--device` | `/dev/ttyUSB0` | CRSF serial device |
| `--baud` | `115200` | Serial baud; mLRS defaults to `400000` when omitted |
| `--host` | `0.0.0.0` | UDP bind address |
| `--port` | `60000` | RC input UDP port |
| `--tx_rate` | `100` | CRSF RC frame rate |
| `--loop_hz` | `250` | Main loop ceiling |
| `--failsafe_time_ms` | `1000` | RC silence before proxy failsafe |
| `--telemetry_udp` | off | Raw CRSF telemetry destination |
| `--config_udp` | off | Radio configuration UDP port |
| `--debug` | off | Verbose serial/RC/telemetry logging |

## Internal layout

```text
crsfproxy.py          public entry point and --radio selector
_elrs_backend.py      shared proxy implementation + ExpressLRS config backend
_mlrs_backend.py      mLRS mBridge-over-CRSF config backend
elrs_config.py        standard CRSF parameter client used by ELRS
crsf_protocol.py      CRSF framing and parameter codec
config_client.py      common UDP configuration CLI/TUI
```

The underscore-prefixed backend modules are implementation details. Run `crsfproxy.py`.

## Notes

- mLRS only supports 400K baud on its handset-side CRSF interface.
- Some CP2102 USB serial adapters behave badly around 400-460K baud. If mLRS traffic is corrupt or unreliable, try another adapter or a direct UART.
- `--config_udp` has no authentication. Bind it to a trusted interface or firewall it.
- ExpressLRS configuration has been exercised against ELRS 4.0.1.
- mLRS support follows the current official mBridge protocol and Lua configurator behavior; hardware validation is still the next step.

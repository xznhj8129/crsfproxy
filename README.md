<div align="center">

# 📡 crsfproxy

**UDP ⇄ CRSF bridge for remote RC, telemetry, radio configuration, and INAV SITL**

Supports **ExpressLRS**, **mLRS**, multiple addressed proxy instances, and CRSF serial RX in **INAV SITL**.

</div>

## What it is

`crsfproxy` makes a CRSF transmitter module usable from a computer instead of a normal RC handset. The same CRSF side can also connect directly to an INAV SITL virtual UART.

It does three things at once:

- receives addressed RC channel values over UDP and sends normal CRSF `RC_CHANNELS_PACKED` frames;
- forwards inbound CRSF telemetry over UDP with a one-byte source system ID;
- exposes the transmitter configuration interface over a second addressed UDP port so `config_client.py` can inspect and change radio settings remotely.

```text
 RC source                         CRSF endpoint
    |                                  |
    | addressed UDP                    | serial or TCP
    v                                  v
+-----------+                      +----------+
| crsfproxy | <------------------> | TX module| <---- RF ----> receiver
+-----------+                      +----------+
    |
    | or socket://127.0.0.1:5762
    v
 INAV SITL UART3 configured as CRSF serial RX

crsfproxy also provides:
    +---- addressed CRSF telemetry UDP
    +---- addressed configuration UDP <----> config_client.py
```

The RC bridge and telemetry path are common to both radio families. `--radio` only selects the transmitter configuration backend.

## Install

```bash
pip install pyserial
pip install pygame  # only needed for joystick_crsf.py
```

## Normal use

The defaults are intended to work without launch-script archaeology:

```text
device      /dev/ttyUSB0
baud        115200
RC UDP      0.0.0.0:60000
config UDP  0.0.0.0:60001
sys_id      1
radio       elrs
```

Start the proxy:

```bash
python3 crsfproxy.py
```

Send joystick RC to it:

```bash
python3 joystick_crsf.py --target localhost
```

Open the transmitter configuration UI:

```bash
python3 config_client.py tui
```

Flags are for changing those defaults, not enabling the normal features.

## System IDs and UDP addressing

Every `crsfproxy` instance has a system ID:

- `0` means broadcast or all systems and is only used as a destination;
- `1..254` identify a specific proxy;
- `255` is reserved and invalid.

`crsfproxy` defaults to `--sys-id 1`.

The system ID is an outer UDP routing byte. It is not part of CRSF, ELRS configuration, or the mLRS mBridge protocol.

For controller-to-proxy traffic, the byte is the destination system ID:

```text
uint8 target_sys_id
payload...
```

For proxy-to-controller traffic, the byte is the source system ID:

```text
uint8 source_sys_id
payload...
```

A proxy accepts RC and configuration packets addressed to its own ID or to ID `0`.

## Radio selection

### ExpressLRS

ExpressLRS is the default:

```bash
python3 crsfproxy.py
```

Use flags only when the hardware or network differs from the defaults. For example, an ELRS module that needs a higher handset baud can use:

```bash
python3 crsfproxy.py --baud 921600
```

`--radio elrs` uses the standard CRSF device/parameter protocol used by `elrs.lua`: `DEVICE_INFO`, `PARAMETER_READ`, `PARAMETER_WRITE`, and CRSF commands. Parameter names, options, folders, and values are read live from the transmitter rather than hard-coded in `crsfproxy`.

ExpressLRS handset baud depends on the module and desired packet rates. `921600` is useful when high ELRS rates are required.

### mLRS

```bash
python3 crsfproxy.py --radio mlrs
```

mLRS uses **400000 baud** on the radio-side CRSF interface. When `--radio mlrs` is selected and `--baud` is omitted, `crsfproxy` automatically uses `400000`.

mLRS configuration is not the standard CRSF parameter protocol. Its Lua configurator tunnels the mBridge command protocol through CRSF frame types `0x81` and `0x82`. The mLRS backend implements that same protocol, including live parameter discovery, multi-frame LIST options, writes, bind commands, bootloader commands, and Save.

mLRS parameter writes take effect immediately but are **not persistent until Save**:

```bash
python3 config_client.py command Save --confirm
```

## RC input

The addressed RC UDP packet is 41 bytes, little-endian after the routing byte:

```text
uint8  target_sys_id
uint32 t_ms
uint16 channels_us[16]
uint32 crc32
```

The CRC32 still covers only the original 36-byte RC payload: `t_ms` plus the 16 channels. The routing byte is outside the RC payload.

### One proxy

```bash
python3 joystick_crsf.py --target 127.0.0.1
```

### Send the same controller state to several proxy servers

`--target` is repeatable:

```bash
python3 joystick_crsf.py \
    --target 192.168.1.20 \
    --target 192.168.1.21 \
    --sys-id 0
```

The RC packet is built once and sent unchanged to every target.

### UDP broadcast

The joystick sender enables `SO_BROADCAST`, so one broadcast datagram can reach every proxy on the subnet:

```bash
python3 joystick_crsf.py \
    --target 192.168.1.255 \
    --sys-id 0
```

To address only one proxy on that same broadcast network, use its specific ID instead of `0`.

If RC updates stop, the proxy repeats the last valid channels until `--failsafe_time_ms` expires, then sends the configured failsafe channels. RF-link failsafe remains the receiver's job.

## Configuration client

`config_client.py` defaults to `127.0.0.1:60001` and system ID `1`:

```bash
python3 config_client.py info
python3 config_client.py devices
python3 config_client.py params
python3 config_client.py get "Packet Rate"
python3 config_client.py set "RF Band" "2.4GHz"
python3 config_client.py tui
```

Use `--host`, `--port`, or `--sys-id` when selecting a different proxy.

For mLRS, for example:

```bash
python3 config_client.py get "Tx Ch Source"
python3 config_client.py set "Tx Ch Source" crsf
python3 config_client.py command Save --confirm
```

Writes are read back for verification. Commands with side effects require `--confirm`.

### Broadcast configuration query

System ID `0` sends one configuration request to all proxies that receive the UDP datagram. The client prints each response under its source system ID:

```bash
python3 config_client.py \
    --host 192.168.1.255 \
    --sys-id 0 \
    info
```

After the first response arrives, `config_client.py` keeps collecting responses until no new reply arrives for `--broadcast-wait` seconds. The default is 2 seconds. `--timeout` controls how long it waits for the first response.

The interactive TUI requires one specific system ID from `1..254`.

## Telemetry

`--telemetry_udp host:port` forwards each valid inbound CRSF frame with the proxy system ID prepended:

```text
uint8 source_sys_id
uint8 crsf_frame[]
```

For example, telemetry from proxy 7 begins with byte `0x07`, followed immediately by the normal CRSF sync/address byte and the rest of the untouched CRSF frame.

This is an intentional breaking change. A consumer that expects raw CRSF, such as a direct MWP UDP input, must remove the first byte before passing the CRSF frame onward.

## INAV SITL

INAV SITL maps its virtual UARTs to TCP ports starting at 5760:

```text
UART1 -> TCP 5760
UART2 -> TCP 5761
UART3 -> TCP 5762
...
```

Configure the selected INAV SITL UART as a normal serial receiver using CRSF. Then point `crsfproxy` directly at that UART with pySerial's `socket://` transport.

Example for UART3:

```bash
python3 crsfproxy.py --device socket://127.0.0.1:5762
```

Then send RC input normally:

```bash
python3 joystick_crsf.py --target 127.0.0.1
```

The path is:

```text
joystick_crsf.py
    |
    | addressed UDP RC
    v
crsfproxy
    |
    | raw CRSF over TCP
    v
INAV SITL virtual UART
    |
    | normal INAV CRSF RX and telemetry
    v
crsfproxy
```

No pseudo-terminal, USB serial adapter, `--serialport`, or `--serialuart` bridge is required. `crsfproxy` connects directly to the TCP UART that INAV SITL already exposes.

The same `--device` option still accepts normal local devices such as `/dev/ttyUSB0`. The proxy uses `serial.serial_for_url()` so local serial devices and `socket://` endpoints share the same CRSF loop.

## Main options

| Flag | Default | Meaning |
|---|---:|---|
| `--radio` | `elrs` | Radio backend: `elrs` or `mlrs` |
| `--sys-id` | `1` | Local proxy ID, `1..254` |
| `--device` | `/dev/ttyUSB0` | Local serial device or pySerial URL such as `socket://127.0.0.1:5762` |
| `--baud` | `115200` | Serial baud; mLRS defaults to `400000` when omitted; ignored by `socket://` |
| `--host` | `0.0.0.0` | UDP bind address |
| `--port` | `60000` | RC input UDP port |
| `--tx_rate` | `100` | CRSF RC frame rate |
| `--loop_hz` | `250` | Main loop ceiling |
| `--failsafe_time_ms` | `1000` | RC silence before proxy failsafe |
| `--telemetry_udp` | off | Addressed CRSF telemetry destination |
| `--config_udp` | `60001` | Addressed radio configuration UDP port |
| `--debug` | off | Verbose serial/RC/telemetry logging |

## Repository layout

The root Python files are the user-facing executables. Supporting code lives under `lib/`.

```text
crsfproxy.py          run the CRSF proxy
joystick_crsf.py      send joystick RC to one or more proxies
config_client.py      inspect and configure a transmitter

lib/
    __init__.py
    elrs_backend.py   shared proxy implementation + ExpressLRS config backend
    mlrs_backend.py   mLRS mBridge-over-CRSF config backend
    elrs_config.py    standard CRSF parameter client used by ELRS
    crsf_protocol.py  CRSF framing and parameter codec
    udp_mux.py        one-byte system ID routing for UDP transports

tests/
    ...
```

If a Python file is in the repository root, it is something a user runs.

## Notes

- mLRS only supports 400K baud on its handset-side CRSF interface.
- Some CP2102 USB serial adapters behave badly around 400-460K baud. If mLRS traffic is corrupt or unreliable, try another adapter or a direct UART.
- The configuration UDP interface has no authentication. Bind it to a trusted interface or firewall it.
- System ID routing is intentionally simple. It provides addressing, not authentication or cryptographic identity.
- ExpressLRS configuration has been exercised against ELRS 4.0.1.
- mLRS support follows the current official mBridge protocol and Lua configurator behavior; hardware validation is still the next step.

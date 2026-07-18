# crsfproxy

Bridge between a UDP RC client (udp_crsf format) and a CRSF serial port. Sends
RC_CHANNELS_PACKED to the radio, can forward raw telemetry frames over UDP (for
MWP), and can expose the transmitter's elrs.lua configuration interface over a
separate UDP port.

## Run

```bash

# In one terminal (telemetry streamed raw to MWP on :40042)
python crsfproxy.py --device /dev/ttyUSB0 --baud 115200 --host 0.0.0.0 --port 60000 --loop_hz 250 --tx_rate 100 --telemetry_udp 127.0.0.1:40042 --config_udp 60001

# MWP example (listen for UDP telemetry)
mwp -d udp://:40042 -a

# RC source (joystick example)
python joystick_crsf.py --target 127.0.0.1 --port 60000 --rate 75
```

## Remote ELRS configuration

`--config_udp PORT` enables TX-module configuration. The proxy continues to
send RC traffic and inserts DEVICE/PARAMETER frames into the same scheduled
CRSF writes, matching the handset-side behavior used by elrs.lua and
elrsbuddy. This interface only works when the serial device is connected to an
ELRS transmitter's handset CRSF port.

The transmitter supplies its live parameter names, types, choices, folders,
information strings, and commands. The client therefore does not execute or
parse a local Lua file; it speaks the same dynamic protocol that the proper
ELRS Lua script uses. `crsfproxy` imports the verified protocol implementation
from the repository's sibling `elrstest/` directory.

One-shot examples:

```bash
python config_client.py --port 60001 info
python config_client.py --port 60001 params
python config_client.py --port 60001 get "Packet Rate"
python config_client.py --port 60001 set "RF Band" "2.4GHz"
python config_client.py --port 60001 set "Packet Rate" "333Hz Full(-105dBm)"
python config_client.py --port 60001 command Bind --confirm
```

For an interactive terminal:

```bash
python config_client.py --host proxy-host --port 60001 tui
```

The terminal uses the terminal's default colors and presents the live Lua
parameter hierarchy. Up and Down select, Right or Enter opens a folder, edits
a value, or runs a command, and Left or Escape returns to the parent. Left or
Escape at the root exits. `r` reloads the table. Selection parameters use a
choice list; numeric parameters use a text prompt and are verified by reading
them back. Empty selection placeholders sent by ELRS are omitted from the
terminal without changing their underlying selection indices. A persistent
banner displays the latest ELRS status message, connection state, flags, and
good/bad packet counts.

`info` returns the DEVICE_INFO model and version, all current Lua parameter
values (including band, packet rate, link mode, model ID, and firmware-provided
information fields), the RADIO_ID handset rate, and the latest ELRS status
including connection/binding state, flags, packet counts, and message. A
firmware hash is included when the transmitter exposes it as an INFO or STRING
parameter.

The UDP API accepts either JSON:

```json
{"command":"set","parameter":"Packet Rate","value":"333Hz Full(-105dBm)"}
```

or shell-style UTF-8 commands:

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

Only one configuration operation is processed at a time. Reads and writes can
take several seconds because ELRS parameter entries are chunked and every write
is read back for verification. There are no automatic UDP retries because
repeating a command such as Bind can have side effects.

## Virtual pair for SITL or testing
socat -d -d pty,raw,echo=0 pty,raw,echo=0

## Message format

Client -> proxy (RC): 40-byte little-endian UDP payload  
`uint32 t_ms | 16 x uint16 channels in microseconds | uint32 crc32(payload)`  
CRC32 covers the first 36 bytes.

Telemetry (proxy -> client): raw CRSF frames sent unchanged to `--telemetry_udp` (host:port).

Configuration commands use ordinary UTF-8 UDP datagrams on `--config_udp`.
Replies return to the source address and source port of each request.

## Notes

- On CP2102, 400-460K baud is buggy
- ELRS hides packet rates that cannot fit over the configured handset baud.
  At 115200 baud the TX reports `Baud rate too low` and blanks rates such as
  333 Hz and 500 Hz. Use 460800 baud to expose the full high-rate table.
- The configuration port has no authentication. Bind it to a trusted interface
  or restrict it with a firewall.
- Loop limiter: `--loop_hz` caps main loop CPU. RC send rate is `--tx_rate`.
- Failsafe (proxy): if RC updates stop for < `--failsafe_time_ms`, repeat last valid channels. If RC updates stop for >= `--failsafe_time_ms`, send `--failsafe_channels_us` (16 values; default: `1500,1500,900,1500,900,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500`). Radio link failsafe (TX<->RX loss) is handled by the receiver, not this proxy. Default RC init sets throttle/arm low.

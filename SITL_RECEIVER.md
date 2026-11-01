# Virtual CRSF receiver for INAV SITL

`crsf_rx_sim.py` impersonates an ELRS/CRSF receiver at the UART boundary seen by INAV. It sends ordinary `RC_CHANNELS_PACKED` and `LINK_STATISTICS` frames and reads CRSF telemetry returned by the flight controller.

On the user side it speaks crsfproxy protocol v1: one UDP port carries `Kind.CRSF` envelopes for RC input and `Kind.CONTROL` envelopes for runtime control. The contract is [`PROTOCOL.md`](PROTOCOL.md).

It does **not** emulate LoRa, FLRC, binding, frequency hopping, or ExpressLRS firmware. Those details are deliberately below the boundary INAV observes.

## First run

Start a receiver that connects to an INAV SITL TCP serial endpoint:

```bash
python crsf_rx_sim.py \
    --endpoint tcp://127.0.0.1:5763 \
    --host 127.0.0.1 --port 60000 \
    --rc_rate 150 --link_rate 10 \
    --telemetry_udp 127.0.0.1:40042 \
    --debug
```

When the external program must accept a connection from SITL instead, use:

```bash
python crsf_rx_sim.py --endpoint tcp-listen://127.0.0.1:5763
```

The exact SITL UART port depends on the serial mapping used by the INAV build. Configure that UART as a serial receiver with CRSF as the provider. Do not use the `SIM` receiver provider for this path: the purpose is to exercise the actual CRSF serial receiver implementation.

## UDP protocol input

One UDP port carries protocol v1 envelopes. The default is `0.0.0.0:60000/udp`.

Every datagram is a 10-byte little-endian header — `version=1`, `kind`, `session`, `seq` — followed by the payload:

- `Kind.CRSF` (1): exactly one CRSF frame. An `RC_CHANNELS_PACKED` frame (0x16) drives the receiver. The simulator uses the local arrival time for source-timeout handling.
- `Kind.CONTROL` (2): a UTF-8 JSON request object.
- `Kind.CONTROL_REPLY` (3): the simulator's JSON answer, carrying the request's `session` and `seq`.

The existing `joystick_crsf.py` client drives the RC input directly; its `--target` addresses the simulator's UDP port.

## Runtime control

Send a `Kind.CONTROL` envelope to the same port. The payload is JSON only; shell-style text is not accepted. The reply is a `Kind.CONTROL_REPLY` envelope with the same `session` and `seq`.

Request payloads:

```json
{"command":"status"}
{"command":"drop_rf"}
{"command":"restore_rf"}
{"command":"set_link","uplink_lq":35,"uplink_rssi_ant1":105}
{"command":"set_channels","channels":[1500,1500,1000,1500,1000,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500]}
```

A success reply is `{"ok":true,"result":{...}}` with the complete current receiver state in `result`. A malformed or unknown request gets `{"ok":false,"error":{"code":"bad_request","message":"..."}}`.

For example, from Python in the repository root:

```python
import json, socket
from lib import envelope

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
request = {"command": "drop_rf"}
payload = json.dumps(request, separators=(",", ":")).encode()
sock.sendto(envelope.encode(envelope.Kind.CONTROL, 0x12345678, 0, payload),
            ("127.0.0.1", 60000))
reply, _ = sock.recvfrom(65535)
print(json.loads(envelope.decode(reply).payload))
```

Each new request uses a new `seq`; a retry resends the same `session` and `seq`.

## Distinct failure modes

The simulator intentionally keeps these conditions separate.

### Upstream RC source timeout

Configured with:

```bash
--source_timeout_ms 1000
--source_timeout_action hold|failsafe|stop
```

This represents the simulator losing its joystick, GCS, or other RC source while the simulated RF link remains healthy.

### Simulated RF loss

Triggered with `drop_rf` and restored with `restore_rf`.

Configured with:

```bash
--rf_loss_policy stop|failsafe
```

`stop` ceases RC frame output while continuing `LINK_STATISTICS` with uplink and downlink LQ forced to zero. This exercises INAV's real RX timeout and failsafe path.

`failsafe` continues valid CRSF RC frames using `--failsafe_channels` while link quality is zero. This represents receiver-side channel failsafe behavior and is intentionally not equivalent to stopping RC frames.

### TCP serial loss

Closing the SITL socket or killing the peer exercises receiver UART disconnection. The simulator automatically retries the connection.

## Multiple receivers

Run one process per simulated receiver. Each instance is independent and must use a distinct endpoint and UDP protocol port.

```bash
python crsf_rx_sim.py \
    --name primary \
    --endpoint tcp://127.0.0.1:5763 \
    --port 60000

python crsf_rx_sim.py \
    --name secondary \
    --endpoint tcp://127.0.0.1:5764 \
    --port 60010 \
    --rc_rate 50
```

Both instances may be driven by separate UDP sources. `joystick_crsf.py` can target both with `--target 127.0.0.1:60000 --target 127.0.0.1:60010`, and each simulator accepts its own `Kind.CONTROL` requests on its own port. To mirror RC input, send the same CRSF envelope to both ports.

This is enough to test dual-receiver selection, receiver loss, recovery, hysteresis, different packet rates, different link statistics, and conflicting channel values without any RF hardware.

## Telemetry and captures

`--telemetry_udp host:port` forwards every complete inbound CRSF frame unchanged.

`--capture path` appends timestamped records:

```text
uint64 little-endian Unix timestamp in nanoseconds
uint32 little-endian frame length
raw CRSF frame bytes
```

## Status

Implemented in the first milestone:

- Direct CRSF transport over TCP connect or TCP listen mode
- Protocol v1 `Kind.CRSF` envelope RC input carrying `RC_CHANNELS_PACKED`
- `RC_CHANNELS_PACKED` generation at a configurable rate
- Standards-shaped 10-byte `LINK_STATISTICS` generation
- Bidirectional parsing of CRSF telemetry returned by INAV
- Optional raw telemetry forwarding over UDP
- Timestamped raw CRSF capture files
- Independent RC-source timeout and simulated RF-loss behavior
- Runtime JSON `Kind.CONTROL` UDP control on the same port
- Automatic TCP reconnection
- Multiple-receiver operation through independent process instances
- Unit coverage for frame layout, RC frame validation, failure states, control operations, and local UDP envelope round trips

Validation before the merge into `master` (2026-08-02): 14 unit tests passed, Python bytecode compilation passed, TCP socket smoke test passed, RF drop/restore control smoke test passed.

Validation after the merge (2026-10-27, `19708b5`): full suite 22 tests passed; a live TCP-listen run at `--rc_rate 150` delivered 300 RC frames in 2.00 s.

Not yet implemented:

- Single-process receiver farm configuration
- Scenario timeline engine
- Latency, jitter, packet-loss, and corruption injection
- Automated INAV launch, MSP assertions, and CI harness

## Tests

```bash
python -m unittest discover -s tests -v
```

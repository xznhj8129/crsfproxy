# Virtual CRSF receiver for INAV SITL

`crsf_rx_sim.py` impersonates an ELRS/CRSF receiver at the UART boundary seen by INAV. It sends ordinary `RC_CHANNELS_PACKED` and `LINK_STATISTICS` frames and reads CRSF telemetry returned by the flight controller.

It does **not** emulate LoRa, FLRC, binding, frequency hopping, or ExpressLRS firmware. Those details are deliberately below the boundary INAV observes.

## First run

Start a receiver that connects to an INAV SITL TCP serial endpoint:

```bash
python crsf_rx_sim.py \
    --endpoint tcp://127.0.0.1:5763 \
    --host 127.0.0.1 --port 60000 \
    --control_host 127.0.0.1 --control_port 60001 \
    --rc_rate 150 --link_rate 10 \
    --telemetry_udp 127.0.0.1:40042 \
    --debug
```

When the external program must accept a connection from SITL instead, use:

```bash
python crsf_rx_sim.py --endpoint tcp-listen://127.0.0.1:5763
```

The exact SITL UART port depends on the serial mapping used by the INAV build. Configure that UART as a serial receiver with CRSF as the provider. Do not use the `SIM` receiver provider for this path: the purpose is to exercise the actual CRSF serial receiver implementation.

## RC UDP input

The RC input is identical to `crsfproxy.py`:

```text
uint32 little-endian timestamp_ms
16 x uint16 little-endian channel_us
uint32 little-endian CRC32 over the preceding 36 bytes
```

Default RC input port: `60000/udp`.

The existing `joystick_crsf.py` client can drive the simulator directly.

## Runtime control

Default control port: `127.0.0.1:60001/udp`.

JSON examples:

```json
{"action":"status"}
{"action":"drop_rf"}
{"action":"restore_rf"}
{"action":"set_link","uplink_lq":35,"uplink_rssi_ant1":105}
{"action":"set_channels","channels":[1500,1500,1000,1500,1000,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500]}
```

Shell-style datagrams are also accepted:

```text
status
drop-rf
restore-rf
set uplink_lq 35
set uplink_rssi_ant1 105
```

Example with netcat:

```bash
printf 'drop-rf' | nc -u -w1 127.0.0.1 60001
printf 'restore-rf' | nc -u -w1 127.0.0.1 60001
printf 'set uplink_lq 25' | nc -u -w1 127.0.0.1 60001
```

Every control response is JSON and includes the complete current receiver state.

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

Run one process per simulated receiver. Each instance is independent and must use a distinct endpoint, RC port, and control port.

```bash
python crsf_rx_sim.py \
    --name primary \
    --endpoint tcp://127.0.0.1:5763 \
    --port 60000 --control_port 60001

python crsf_rx_sim.py \
    --name secondary \
    --endpoint tcp://127.0.0.1:5764 \
    --port 60010 --control_port 60011 \
    --rc_rate 50
```

Both instances may be driven by separate UDP sources. To mirror controls, send the same 40-byte RC datagram to both RC ports.

This is enough to test dual-receiver selection, receiver loss, recovery, hysteresis, different packet rates, different link statistics, and conflicting channel values without any RF hardware.

## Telemetry and captures

`--telemetry_udp host:port` forwards every complete inbound CRSF frame unchanged.

`--capture path` appends timestamped records:

```text
uint64 little-endian Unix timestamp in nanoseconds
uint32 little-endian frame length
raw CRSF frame bytes
```

## Tests

```bash
python -m unittest discover -s tests -v
```

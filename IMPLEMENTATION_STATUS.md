# Virtual receiver implementation status

Branch: `agent/virtual-crsf-receiver-sitl`

Implemented in the first milestone:

- Direct CRSF transport over TCP connect or TCP listen mode
- Existing 40-byte `udp_crsf` RC input format
- `RC_CHANNELS_PACKED` generation at a configurable rate
- Standards-shaped 10-byte `LINK_STATISTICS` generation
- Bidirectional parsing of CRSF telemetry returned by INAV
- Optional raw telemetry forwarding over UDP
- Timestamped raw CRSF capture files
- Independent RC-source timeout and simulated RF-loss behavior
- Runtime JSON and shell-style UDP control
- Automatic TCP reconnection
- Multiple-receiver operation through independent process instances
- Unit coverage for frame layout, RC packet validation, failure states, and control operations

Local validation performed before publication:

```text
14 unit tests passed
Python bytecode compilation passed
TCP socket smoke test passed
RF drop/restore control smoke test passed
```

Not yet implemented:

- Single-process receiver farm configuration
- Scenario timeline engine
- Latency, jitter, packet-loss, and corruption injection
- Automated INAV launch, MSP assertions, and CI harness

# Plan: MAVLink over the existing CRSF link

## Goal

Carry gamepad RC and bidirectional MAVLink through the Nomad over the one
pin-swapped CRSF serial connection already used by `crsfproxy`.

RC remains ordinary CRSF. MAVLink is wrapped in the standard CRSF `0xAA`
MAVLink Envelope and sent alongside it.

```text
 gamepad --udp_crsf--> CRSF 0x16 --+
                                     +-- crsfproxy -- CRSF serial -- Nomad ))) RX -- CRSF UART -- INAV
 GCS <------MAVLink UDP------> 0xAA -+
```

On the aircraft:

- CRSF `RC_CHANNELS_PACKED` continues through INAV's normal CRSF receiver path.
- CRSF `0xAA` envelopes are reassembled and passed to INAV's MAVLink stack.
- INAV MAVLink output is split into `0xAA` envelopes and returned through CRSF
  telemetry.

## Fixed architecture

- The Nomad remains a CRSF transmitter module receiving normal handset CRSF.
- The ExpressLRS receiver remains in CRSF serial-output mode.
- RC stays on ExpressLRS's native low-latency OTA RC path.
- MAVLink uses ExpressLRS's existing bidirectional CRSF data transport.
- ExpressLRS source code is not changed.
- `crsfproxy` remains one process and one serial-port owner.
- Existing ELRS configuration traffic, CRSF telemetry forwarding, and RC
  failsafe behavior remain intact.

There is no raw-MAVLink serial mode, no ELRS MAVLink-mode auto-detection, no
MAVLink RC override, and no second serial connection.

## CRSF MAVLink Envelope

CRSF frame type `0xAA` is the standard MAVLink Envelope defined by the
[CRSF specification](https://github.com/tbs-fpv/tbs-crsf-spec/blob/main/crsf.md#0xaa-crsf-mavlink-envelope).
It carries a complete serialized MAVLink 1 or MAVLink 2 frame in one or more
CRSF frames:

```text
 uint8  chunk       high nibble = zero-based last chunk index
                    low nibble  = zero-based current chunk index
 uint8  data_size   number of MAVLink bytes in this chunk, maximum 58
 uint8  data[]      serialized MAVLink bytes
```

A one-chunk message uses `last = 0`, `current = 0`. A maximum-size MAVLink 2
frame fits in five CRSF envelopes. The MAVLink checksum and optional signature
remain inside the serialized MAVLink data; the enclosing CRSF frame has its
normal CRSF CRC.

ExpressLRS does not need to decode the envelope. Its CRSF router carries the
frame over the existing reliable data path and the receiver forwards it to the
FC's CRSF UART.

## crsfproxy changes

Extend the existing `crsfproxy.py`; do not create a separate raw-MAVLink proxy.

1. Add `MAVLINK_ENVELOPE = 0xAA` to the CRSF codec. Treat it as the standard
   envelope layout, not as a destination/origin extended-header frame.
2. Add envelope fragmentation and reassembly for complete serialized MAVLink
   frames.
3. Add a bidirectional MAVLink UDP endpoint for the GCS.
4. Parse MAVLink received from the GCS into complete frames, wrap each frame in
   one or more `0xAA` CRSF frames, and queue them for serial transmission.
5. Keep `RC_CHANNELS_PACKED` first in every scheduled handset write. Append
   queued MAVLink envelopes in the same place configuration frames are already
   inserted; MAVLink must never delay an RC write.
6. Reassemble inbound `0xAA` telemetry frames and forward each complete raw
   MAVLink frame to the GCS.
7. Continue forwarding all other inbound CRSF telemetry unchanged to the
   existing telemetry target.

The current UDP RC packet format, channel mapping, ELRS parameter service,
serial speed negotiation, and failsafe channel behavior do not change.

## INAV changes

Add a CRSF-backed MAVLink endpoint to the existing MAVLink implementation.

1. Define CRSF frame type `0xAA` and its two-byte envelope header.
2. In the CRSF receive path, validate the CRSF frame normally, reassemble its
   MAVLink chunks, and give each complete serialized frame to the existing
   MAVLink parser and dispatcher.
3. Register the CRSF-backed endpoint with the existing MAVLink runtime so
   routing, commands, missions, request handling, and stream scheduling are
   shared with ordinary MAVLink serial ports.
4. Serialize MAVLink output from that endpoint, split it into `0xAA` envelopes,
   and queue those envelopes through CRSF telemetry.
5. Keep CRSF RC handling independent. A MAVLink or GCS failure must not alter
   RC frame processing or receiver failsafe behavior.

The FC serial configuration remains CRSF:

```text
receiver_type = SERIAL
serialrx_provider = CRSF
```

The UART is not configured as a MAVLink serial receiver and RC is not converted
to `RC_CHANNELS_OVERRIDE`.

## Implementation order

1. Add `0xAA` codec and chunking tests to `crsfproxy`.
2. Add the UDP bridge and verify envelope round trips without hardware.
3. Add INAV CRSF-envelope ingress and verify GCS heartbeat and commands.
4. Add INAV MAVLink egress through CRSF telemetry.
5. Verify missions and normal telemetry through the complete radio link.

## Acceptance checks

- Gamepad RC works through ordinary CRSF with no GCS connected.
- Existing ELRS configuration commands still work while RC is running.
- A GCS discovers INAV and receives heartbeat and telemetry through `0xAA`.
- Commands and mission upload/download work in both directions.
- Stopping gamepad UDP produces the same configured proxy failsafe behavior as
  current `crsfproxy`.
- Losing the RF link produces the normal ExpressLRS/INAV CRSF failsafe.
- No ExpressLRS firmware changes or second serial connection are required.

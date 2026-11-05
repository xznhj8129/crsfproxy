# crsfproxy protocol v1

> **Status:** the contract for crsfproxy v0.4, implemented by v0.4. The v0.3
> wire formats are listed under [Changes from v0.3](#changes-from-v03).

This is the **user↔server** contract: what crosses the network between
crsfproxy and its clients. Any other program that acts as the server implements
the same rules.

## Links

| Link | Medium | Covered here |
|---|---|---|
| **user↔server** | any IP path, possibly the internet, always inside a VPN | yes |
| **server↔node** | LAN: raw CRSF bytes over TCP to a node such as the ESP32-P4 (`socket://host:port`) | no |
| **ground↔air** | ExpressLRS RF | no |

Serial cables are not links. With the TX module on a local serial port there is no server↔node link.

## Terms

- **server** — the program at the field that drives the CRSF device.
- **device** — the CRSF port the server drives: a TX module, directly or through a node.
- **client** — anything that talks to the server over user↔server.
- **peer** — a client's UDP source address (IP, port).
- **session** — a random nonzero u32 a sender picks when it starts.
- **destination** — a `host:port` configured on the server that receives downlink.

## Transport

- UDP over IPv4.
- One server port. Default `60000`.
- One datagram is one envelope.
- No authentication and no encryption.
- Run it inside a VPN and bind the server to the VPN interface (`--host`).
- The protocol has no fragmentation layer of its own.

**Rejected datagrams** are dropped and logged with the peer and the reason:

- shorter than 10 bytes;
- `version` is not 1;
- `session` is 0;
- `kind` is reserved or not valid in that direction;
- a CRSF payload that is not exactly one valid frame;
- an `RC_CHANNELS_PACKED` frame (0x16) whose payload is not 22 bytes.

A CONTROL request with a bad payload is answered with `bad_request` instead.

## Envelope

Little-endian. A 10-byte header, then the payload.

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | `version` | `1` |
| 1 | 1 | `kind` | payload type |
| 2 | 4 | `session` | sender's session, nonzero |
| 6 | 4 | `seq` | sender's counter for this kind |
| 10 | n | payload | depends on `kind` |

- `seq` starts at 0 for each session.
- `seq` counts per sender and per kind.
- `seq` increments by one and wraps at 2³².
- `a` is newer than `b` when `(a − b) mod 2³²` is in `[1, 2³¹)`.
- A CONTROL_REPLY carries the `session` and `seq` of the request it answers.
- The server picks a random nonzero session at start and uses it for CRSF and
  STATUS.

| `kind` | Name | Direction | Payload |
|---|---|---|---|
| 1 | CRSF | both | exactly one CRSF frame |
| 2 | CONTROL | client → server | UTF-8 JSON request object |
| 3 | CONTROL_REPLY | server → requester | UTF-8 JSON reply object |
| 4 | STATUS | server → destinations | 28-byte status struct |

Other `kind` values are reserved.

## Kinds

### CRSF

- The payload is one CRSF frame exactly as on the wire.
- A frame is 4 to 64 bytes:
  - address;
  - length;
  - type;
  - payload;
  - CRC8, DVB-S2 over type and payload.
- The length byte must match the datagram.
- The server passes frame bytes through unchanged.

**Client → server**

- `RC_CHANNELS_PACKED` (0x16, 22-byte payload) is **RC**.
- It follows the [RC rules](#rc).
- Other frame types follow [Other frames to the device](#other-frames-to-the-device).
- A raw-CRSF configurator only receives the device's answers if its endpoint is
  a `--telemetry` destination.

**Server → destinations**

- Every valid frame read from the device, in arrival order.
- Envelope destinations get a CRSF envelope.
- Raw destinations get the bare frame.
- The server uses one CRSF counter for all envelope destinations.

### CONTROL

A request to the server itself. The payload is a UTF-8 JSON object with a
`command`.

| Request | Result |
|---|---|
| `{"command":"devices"}` | `protocol`, `devices` |
| `{"command":"info"}` | summary |
| `{"command":"params","offset":O}` | page |
| `{"command":"get","parameter":P}` | `protocol`, `parameter` (full) |
| `{"command":"set","parameter":P,"value":V}` | `protocol`, `verified`, `old_value`, `parameter` (full, read back) |
| `{"command":"command","parameter":P,"step":S}` | `protocol`, `parameter` (full, with the command's new state) |

`info` and `params` read a parameter table that is cached for up to 30 s; `set` and
`command` clear it. `get` locates the parameter in that table and reads its value live.

**`info` result**

- `protocol`
- `device`
- `current_band`
- `packet_rate`
- `mode`
- `model_id`
- `telemetry_ratio`
- `firmware_hash`
- `radio_rate_hz`
- `binding`

**`params` result**

- `protocol`
- `device`
- `parameters`
- `offset`
- `total`
- `more`
- `radio_rate_hz`
- `binding`

- `protocol` is `elrs` or `mlrs`.
- `info` is a summary. It does not repeat the full parameter table; use `params`.
- `devices` lists the device objects found by a ping.
- `P` is a parameter name or decimal id.
- `V` is a string.
  - For a selection: the option text or its decimal index.
  - Otherwise: a decimal number.
- `S` is one of `start`, `confirm`, `cancel`, `poll`.
- `A` is an optional `address` on `params`, `get`, `set`, and `command`: the
  decimal CRSF address of a device from `devices`.
  - Absent means the transmitter (`0xEE`).
  - `info` and `devices` ignore it.
  - An address nobody answered is `bad_request`.
  - mLRS accepts only `0xEE`; its receiver settings are in the transmitter's table.
  - ELRS requests to a device other than the TX resend every 5 s and time out
    after 10 s per chunk, because they cross the air link.
  - It is an additive field: the protocol stays v1.

**`params` pagination**

- `offset` defaults to 0.
- The server returns a page that fits one datagram.
- The reply adds `offset`, `total`, and `more`.
- `more` is true when `offset + len(parameters) < total`.
- The client loops until `more` is false.

**Parameter objects**

- A **compact parameter object** contains:
  - `id`, `parent`, `type`, `hidden`, `name`, `value`, `raw_value`;
  - `minimum`, `maximum`, `default`, `unit`;
  - `children`, `command_status`, `command_timeout`, `command_info`.
- A compact object does not contain `options`.
- A **full parameter object** adds `options`.
- `get`, `set`, and `command` return full objects.
- `params` returns compact objects.

**Device objects**

- A device object contains:
  - `address`, `name`, `serial`;
  - `hardware_version`, `software_version`, `version`;
  - `parameter_count`, `parameter_version`.
- `hardware_version` and `software_version` are backend-formatted strings.

**Status fields**

- `radio_rate_hz` is a number or `null`.
- `binding` is the backend's handset-link status object, or `null`.
- A field the backend does not have is `null`.
- ELRS `binding` contains:
  - `connected`, `packets_bad`, `packets_good`, `flags`, `message`.
- These are handset-link counters (server↔module), not RF.
- mLRS adds an `mlrs` object with its own status and fields.
- Its `binding` is `null`.
- Backends may add fields. Clients must ignore unknown fields.

**Running a command**

- Send `start`.
- Read `command_status`:
  - `0` READY: finished.
  - `3` CONFIRMATION_NEEDED: `command_info` is the device's prompt. Send
    `confirm` or `cancel`.
  - Anything else: running. Send `poll` after `command_timeout` × 10 ms, or
    `cancel`.

**mLRS**

- `--radio mlrs`.
- mLRS uses mBridge in CRSF frames 0x81 and 0x82.
- Writes are live until the `Save` command.

### CONTROL_REPLY

- Sent to the peer the request came from.
- Carries the request's `session` and `seq`.
- Success: `{"ok": true, "result": {...}}`.
- Failure: `{"ok": false, "error": {"code": C, "message": "..."}}`.

| `code` | Meaning | Client action |
|---|---|---|
| `busy` | another request is running | retry the same request |
| `bad_request` | invalid request | fix it; use a new seq |
| `failed` | the operation ran and failed | report it; use a new seq to retry |
| `stale` | older than a completed request, and its reply is no longer cached | do not resend; use a new request to check state if needed |

- `bad_request` covers:
  - not JSON;
  - unknown command;
  - missing field;
  - unknown parameter.

- A CONTROL_REPLY must fit in one datagram.
- If a reply would not fit, the server returns `failed` with a message.

### STATUS

- Sent to every `--telemetry` destination at `--status_hz`.
- Sent whether or not anything arrives.
- Seven u32 LE fields:

| # | Field | Meaning |
|---|---|---|
| 0 | `rc_age_ms` | ms since the last accepted RC frame; `0xFFFFFFFF` if none since start |
| 1 | `rc_session` | session of that frame; 0 if none |
| 2 | `rc_seq` | seq of that frame; 0 if none |
| 3 | `rc_accepted` | RC frames accepted since start |
| 4 | `rc_stale` | RC frames from the current or last owner dropped because their seq was not newer |
| 5 | `rc_foreign` | RC frames dropped while an owner was present because they came from a different `(peer, session)` |
| 6 | `device_rx_age_ms` | ms since the last valid frame from the device; `0xFFFFFFFF` if none |

- Counters wrap at 2³².

## Server rules

### RC

- The **owner** is the `(peer, session)` of the first RC frame accepted after
  start or after a release.
- **No owner:**
  - a frame from the released owner with a newer seq is accepted;
  - a frame from the released owner with a non-newer seq is dropped,
    `rc_stale` + 1;
  - a frame from any other `(peer, session)` is accepted and becomes the owner.
- **Owner present:**
  - a frame from the owner with a newer seq is accepted;
  - a frame from the owner with a non-newer seq is dropped, `rc_stale` + 1;
  - a frame from a different `(peer, session)` is dropped, `rc_foreign` + 1.
- The owner is released after `--rc_owner_timeout_ms` (default 1000) with no
  accepted RC.
- After release, keep the released owner's last accepted seq.
- The server writes to the device in fixed slots at `--tx_rate`. Each slot:
  - **live**: an RC frame was accepted since the last slot → write the latest
    accepted frame, unchanged;
  - **hold**: `--rc-hold-ms` > 0 and the latest accepted frame is younger than
    that → write it again;
  - **idle**: otherwise → no RC frame.
- The server never generates channel values.

### Other frames to the device

- Non-RC CRSF frames from the RC owner are queued in arrival order.
- The queue is bounded. When full, new frames are dropped and logged.
- All queued frames are written in the next slot, after that slot's RC frame if
  any.
- Frames from any other `(peer, session)` are dropped and logged.
- While a CONTROL request is running, all non-RC frames are dropped and logged.
- CONTROL frames are a separate path. The owner rule does not apply to them.

### Downlink

- Every valid device frame goes to:
  - each `--telemetry` destination as a CRSF envelope;
  - each `--telemetry_raw` destination as the bare frame.
- STATUS goes to each `--telemetry` destination.
- The server's CRSF and STATUS envelopes each use one counter for all envelope
  destinations, so every envelope destination sees a contiguous seq.
- Destinations come from configuration only.
- The server sends nothing to a peer because the peer sent something.
- The one exception is a CONTROL_REPLY to its requester.

### CONTROL

- One request executes at a time, across all peers.
- The request key is `(peer, session, seq)`.
- For each CONTROL request:
  1. Same key as the running request → ignore it.
  2. Same key as a cached reply → resend the cached reply.
  3. The session has a high-water mark and the seq is not newer → reply
     `stale`.
  4. Invalid request → immediate `bad_request`, not cached.
  5. Another request is running → immediate `busy`, not cached.
  6. Otherwise → execute it.
- When execution finishes, cache the reply and set the session's high-water
  mark to the request's seq.
- Do this before sending the reply.
- The cache is per `(peer, session)`.
- It keeps recent `(seq → reply)` entries for at least 60 s.
- The client's retry window is at most 30 s, so a retry finds its reply.
- `busy`, `bad_request`, and `stale` do not set the high-water mark and are not
  cached.
- A repeated request therefore executes once, including `start`, `confirm`, and
  `cancel`.
- A server restart loses the cache.

### One configurator at a time

- Only one configurator may talk to the module at a time.
- CONTROL is serialized server-wide.
- A raw configurator must be the RC owner.
- Raw configuration is paused while CONTROL runs.
- A client that wants to configure without sending RC uses CONTROL.

## Client rules

- Pick a new random nonzero session at every start.
- Count `seq` per kind from 0.
- Use one UDP socket for the session, so the peer stays stable.
- **RC**:
  - Send CRSF RC frames at your own rate.
  - Nothing is acknowledged.
  - Keep sending to keep ownership.
  - Stop, and ownership is released after the timeout.
- **CONTROL**:
  - At most one request in flight per session.
  - Resend the same datagram (same session and seq) about every second.
  - Stop after 30 s without a reply.
  - Match replies by session and seq.
  - Follow the [CONTROL_REPLY](#control_reply) table for `busy`, `stale`,
    `bad_request`, and `failed`.
- **Downlink**:
  - To receive anything, be a `--telemetry` destination on the server.
  - The exception is a CONTROL_REPLY, which goes to the requester.
  - Gaps in the server's per-kind seq are downlink loss.
  - A new server session means the server restarted.
- **RTT**:
  - Remember when you sent each recent RC seq.
  - When `STATUS.rc_session` is yours:
    - RTT = now − (send time of `STATUS.rc_seq`) − `STATUS.rc_age_ms`.

Shared rules the server does not enforce:

- Keep RC flowing while configuring an ELRS module.

## Health per link

| Link | Uplink | Downlink |
|---|---|---|
| user↔server | STATUS: `rc_age_ms`, counters, RTT | gaps in the server's per-kind seq |
| server↔node | ELRS status `packets_good` / `packets_bad` | STATUS: `device_rx_age_ms` |
| ground↔air | ELRS `LINK_STATISTICS` | ELRS `LINK_STATISTICS` |

## Failure behavior

**user↔server uplink lost**

- RC frames to the device stop once `--rc-hold-ms` runs out.
- Nothing is substituted.
- STATUS keeps arriving with `rc_age_ms` growing.
- Device frames keep arriving for as long as the device produces them.

What the device does next is outside this protocol.

- ExpressLRS 4.0.1 (`src/src/tx_main.cpp`, `SendRCdataToRF()`):
  - once it has received at least one RC frame, the TX module stops sending
    channel data when the last one is more than 1 s old;
  - normal link mode: no RF packets at all;
  - MAVLink link mode: sync and MAVLink data packets continue, without channel
    data.
- The receiver and flight controller failsafe follows from that.
- mLRS has not been checked.

**server↔node lost**

- With `socket://`, a dropped connection makes the server exit.
- Run it under a service manager that restarts it.

**server restart**

- The CONTROL cache is in memory.
- A request that was executing may be repeated.

## Server configuration

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | bind address; use the VPN interface |
| `--port` | `60000` | the one protocol port |
| `--telemetry HOST:PORT` | none | envelope downlink destination; repeatable |
| `--telemetry_raw HOST:PORT` | none | bare CRSF downlink destination; repeatable |
| `--rc_owner_timeout_ms` | `1000` | RC owner release time; positive |
| `--status_hz` | `5` | STATUS rate; positive |
| `--tx_rate` | `100` | RC slot rate |
| `--loop_hz` | `250` | main-loop cap; keep above `--tx_rate` |
| `--rc-hold-ms` | `0` | repeat the latest RC sample for up to this long |

## Decisions

- RC is never acknowledged or retransmitted; the newest sample is the only
  useful one.
- Security is the VPN's job.
- CONTROL is serialized server-wide.
- The reply cache makes retries exactly-once.
- `params` is paginated and `info` is a summary, so CONTROL replies stay small.
- Raw CRSF configuration is limited to the RC owner and paused during CONTROL,
  so one configurator at a time is enforced.
- The bare CRSF downlink stays for third-party tools.
- Clients port to this protocol; they do not shape it.

## Open points

- IPv6.
- A STATUS rate chosen by the client.

## Changes from v0.3

| v0.3 | v1 |
|---|---|
| 40-byte RC packet (`t_ms`, 16 × µs, CRC32) on `--port` | CRSF `RC_CHANNELS_PACKED` frame in a CRSF envelope on `--port` |
| JSON or shell-style text on `--config_udp` (RC port + 1) | CONTROL / CONTROL_REPLY envelopes on `--port`, JSON only; errors carry a `code` |
| full parameter table in one `params` reply | paginated `params`; `info` is a summary |
| raw telemetry to one `--telemetry_udp` address | `--telemetry` (envelope) and `--telemetry_raw` (bare), both repeatable |
| "one RC source" was a usage rule | RC owner, enforced |
| — | STATUS |

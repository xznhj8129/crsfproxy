# Radio backends

`crsfproxy.py` is the single public proxy entry point. The RC bridge, telemetry forwarding, serial scheduling, UDP configuration API, and failsafe behavior are shared; `--radio` selects only the transmitter-module configuration backend.

## ExpressLRS

ExpressLRS remains the default, so existing commands continue to work unchanged:

```bash
python3 crsfproxy.py --radio elrs \
    --device /dev/ttyUSB0 \
    --baud 921600 \
    --config_udp 60001
```

`--radio elrs` uses the standard CRSF device/parameter protocol implemented by `elrs_config.py`.

## mLRS

```bash
python3 crsfproxy.py --radio mlrs \
    --device /dev/ttyUSB0 \
    --config_udp 60001
```

When `--radio mlrs` is selected and `--baud` is omitted, the proxy uses **400000 baud**, which is the mLRS radio-side CRSF rate.

The mLRS backend uses the official mBridge command protocol carried in CRSF frame types `0x81` and `0x82`. It also accepts mLRS replies addressed to CRSF radio address `0xEA`.

The existing `config_client.py` is unchanged and works with either radio:

```bash
python3 config_client.py --port 60001 info
python3 config_client.py --port 60001 params
python3 config_client.py --port 60001 get "Tx Ch Source"
python3 config_client.py --port 60001 set "Tx Ch Source" crsf
```

mLRS parameter changes are live but not persistent until the native Save command is sent:

```bash
python3 config_client.py --port 60001 command Save --confirm
```

## Internal layout

```text
crsfproxy.py                 public entry point / --radio selector
    |
    +-- _elrs_backend.py     existing proxy implementation + ELRS config
    |
    +-- _mlrs_backend.py     mLRS mBridge configuration adapter

config_client.py             same UDP client for either backend
```

The underscore-prefixed backend modules are implementation details. Run `crsfproxy.py`, not the backend modules directly.

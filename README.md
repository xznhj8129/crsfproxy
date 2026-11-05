<div align="center">

# 📡 crsfproxy

**Put your ExpressLRS or mLRS transmitter module on the network.**

[![Version](https://img.shields.io/badge/version-0.4-informational)](#-version-compatibility)
[![Python](https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Protocol](https://img.shields.io/badge/protocol-CRSF-orange)](https://github.com/tbs-fpv/tbs-crsf-spec/blob/main/crsf.md)
[![ExpressLRS](https://img.shields.io/badge/ExpressLRS-4.0.1-blueviolet)](https://www.expresslrs.org/)
[![mLRS](https://img.shields.io/badge/mLRS-mBridge-blue)](#mlrs)
[![License](https://img.shields.io/badge/license-GPL--3.0-green)](LICENSE)

*Fly your quad from anything that can send a UDP packet, and configure your TX module from a terminal. No handset, no handset screen, no `elrs.lua`.*

</div>

---

## 🤔 Why this exists

A **TX module** is the radio transmitter that plugs into the back of an RC handset. ExpressLRS and mLRS are the open-source, long-range, low-latency firmware that most of FPV now runs on. The handset reads your sticks and sends them to the module over a serial wire, in a format called CRSF. The module broadcasts them to the receiver in the aircraft and brings telemetry back.

The catch is the handset. It's the only thing that can fly with the module, and the only way into the module's settings is the handset's little screen running `elrs.lua`. You can't script it, you can't put it on a network, and you can't plug your own software into it.

**crsfproxy turns a computer into the handset.** Plug the module into a laptop or a small board and run crsfproxy next to it. Now anything that can send a UDP packet can fly with it, configure it and read its telemetry: a joystick, a script or a ground station, sitting next to it or on the other side of the planet.

A cheap off-the-shelf TX module becomes a long-range radio link you can program:

- Your own code can fly a real aircraft.
- The module and antenna can stay at the field while the pilot is somewhere else.
- Packet rate or power can change from a script in the middle of a flight, with no menus.
- The same tools fly an INAV simulation with no radio at all.

## ✨ What it does

| | | |
|---|---|---|
| 🎮 | **Fly** | Joystick, script, ground station, whatever you like sends RC over UDP. The module transmits it exactly as if it came from a handset. |
| 🛠️ | **Configure** | Packet rate, power, band, bind, the whole menu, from a terminal UI or one-line commands. Read live from the module itself. |
| 📊 | **Telemetry** | Everything the aircraft sends back, forwarded over UDP. Plugs straight into [MWP](https://github.com/stronnag/mwptools). |
| 🧪 | **Simulate** | Point it at INAV SITL instead of a radio and fly a simulated aircraft with the exact same setup. |

```
┌──────────────┐                ┌────────────┐                ┌─────────────┐    RF    ┌──────────┐
│ joystick     │───── RC ──────▶│            │─── serial ────▶│  ELRS/mLRS  │─────────▶│ receiver │
│ config TUI   │◀── telemetry ──│  crsfproxy │◀── (CRSF) ─────│  TX module  │◀─────────│   + FC   │
│ MWP, GCS ... │   UDP :60000   │            │                └─────────────┘          └──────────┘
└──────────────┘                └────────────┘
```

## 🚀 Quick start

```bash
pip install pyserial pygame     # pygame is only for the joystick client
```

```bash
# Start the proxy next to the module
python3 crsfproxy.py --device /dev/ttyUSB0

# Fly with a joystick
python3 joystick_crsf.py --target 127.0.0.1

# Open the module's settings
python3 config_client.py tui
```

That's it. To work from another machine, use `--target` and `--host` with the proxy's address.

## 🌐 How do I reach it?

**Over anything that carries IP.** crsfproxy only sends and receives UDP packets, so it doesn't care whether the path is Ethernet, WiFi, LTE or Starlink. What changes is latency and packet loss, and judging whether a link is good enough to fly over is up to you. If RC stops arriving, crsfproxy stops sending and the receiver's failsafe takes over. `--rc-hold-ms` can smooth over short dropouts.

**Put it on a VPN.** There is no login and no encryption, so anyone who can reach the port can fly your aircraft. Run both ends on WireGuard, Tailscale, NetBird or whatever you already use, and bind `--host` to the VPN address. The VPN also takes care of NAT, so a field machine on LTE doesn't need a public IP.

**No cloud.** There's no relay, no account and no service in the middle. Your client talks straight to your proxy.

**crsfproxy is the server.** It runs on the machine the module is plugged into. Joysticks, config tools and ground stations are the clients, and one process drives one module.

**One port carries everything:** RC, configuration, telemetry and status, all on UDP `60000` by default. Several clients can be connected at once:

- **RC:** one pilot at a time. Everyone else is ignored.
- **Configuration:** one request at a time. Anyone else gets "busy" and retries.
- **Telemetry:** goes to every destination you list, so a pilot, MWP and a logger can all watch at once.

## 🛠️ Configure the module from a terminal

```bash
python3 config_client.py --host proxy-host tui
```

A browser for the module's settings, in your terminal:

| Key | Action |
|---|---|
| <kbd>↑</kbd> / <kbd>↓</kbd> | Select |
| <kbd>→</kbd> / <kbd>Enter</kbd> | Open folder · edit value · run command |
| <kbd>←</kbd> / <kbd>Esc</kbd> | Back (exits at the top) |
| <kbd>r</kbd> | Reload |
| <kbd>Esc</kbd> during a command | Cancel it |

Every write is read back to make sure it took. Commands like Bind or WiFi update work the way they do in `elrs.lua`: the module's own confirmation prompt, then its progress, for as long as it runs. **Other Devices** at the top level switches to another device, such as the receiver.

For scripts:

```bash
python3 config_client.py info                    # summary: module, firmware, key settings, link
python3 config_client.py params                  # the full settings table
python3 config_client.py get "Packet Rate"
python3 config_client.py set "Packet Rate" "333Hz Full(-105dBm)"
python3 config_client.py command Bind --confirm
python3 config_client.py --address 0xEC params   # another device's table, e.g. the receiver
```

Keep RC running while you configure. The module answers much more reliably with traffic on its handset port.

### 🚫 No `elrs.lua` required

This is the part people expect to be wrong, so here is what actually happens.

**`elrs.lua` is not the configuration interface. It is a *client* of it.** The settings you see on a handset aren't defined in the Lua script. They live in the **TX module's firmware**. `elrs.lua` is a generic CRSF parameter browser: it pings the module, reads back whatever settings table the firmware sends, and draws it. The handset runs it only because the handset is what has the screen and the buttons.

crsfproxy is **another client of that same firmware protocol**. It sends the same ping, read, write and command frames and gets back the same entries. The module is the source of truth either way.

So there is nothing to emulate and nothing to bundle:

- No `elrs.lua` is parsed, shipped or reimplemented. It never runs here.
- No setting names, choice lists, folders or rate tables are hard-coded. All of it is read from the module as it runs.
- New settings in a new ELRS release show up on their own, without a crsfproxy update.

| Layer | Where it lives | Changes between ELRS versions? | In this repo? |
|---|---|---|---|
| Settings **content**: names, choices, folders, rates | TX module firmware | Yes | **No**, read live |
| Settings **protocol**: ping, read, write, command | CRSF spec | No | Yes (`lib/elrs_config.py`) |
| Frame **codec** | CRSF spec | No | Yes (`lib/crsf_protocol.py`) |

## 🎮 Flying

`joystick_crsf.py` reads a joystick through pygame and sends RC at 50 Hz (`--rate`). Give it several targets and it drives several proxies with the same sticks:

```bash
python3 joystick_crsf.py --target 10.0.0.5 --target 10.0.0.6:60010
```

It recognises a RadioMaster/TX12 plugged in as a USB joystick, and treats anything else as a gamepad. The mappings are at the top of `get_joystick_state()` in the script, and are easy to change.

A few things worth knowing:

- **One pilot at a time.** The first sender owns the sticks. Anyone else is ignored until the owner has been silent for a second (`--rc_owner_timeout_ms`).
- **No made-up sticks.** When RC stops coming in, crsfproxy stops sending it, and the receiver's failsafe takes over. `--rc-hold-ms` can repeat the last sample to ride out a short network hiccup; it is off by default.
- **Rate.** `--tx_rate` (100 by default) is the most RC frames per second sent to the module.

Not into Python? RC is a standard CRSF `RC_CHANNELS_PACKED` frame in a small UDP envelope, so any program can drive it. See [PROTOCOL.md](PROTOCOL.md).

## 📊 Telemetry

Tell crsfproxy where to send telemetry. You can list as many destinations as you like:

```bash
python3 crsfproxy.py --telemetry_raw 127.0.0.1:40042
mwp -d udp://:40042 -a
```

`--telemetry_raw` sends bare CRSF frames, for MWP and anything else that already speaks CRSF. `--telemetry` sends the same frames inside crsfproxy's envelope, plus a status packet five times a second: RC age, who has the sticks, when the module last spoke, and counters for dropped frames. That's how you can tell from the other end that the link is alive.

## 🌐 Module somewhere else

The module doesn't have to be plugged into the machine running crsfproxy. Any transparent TCP-to-serial bridge works:

```bash
python3 crsfproxy.py --device socket://192.168.1.50:60000
```

The bridge sets the serial baud itself, so `--baud` is ignored here.

## 📻 mLRS

```bash
python3 crsfproxy.py --radio mlrs
```

mLRS runs at **400000 baud** on the handset side, and that's the default for `--radio mlrs`. Configuration uses mLRS's own mBridge protocol over CRSF (frame types `0x81`/`0x82`, replies to `0xEA`), the same one its Lua configurator uses. The same `config_client.py` commands work:

```bash
python3 config_client.py get "Tx Ch Source"
python3 config_client.py set "Tx Ch Source" crsf
python3 config_client.py command Save --confirm
```

mLRS changes take effect immediately, but are lost at power-off unless you run **Save**.

## 🧪 INAV SITL

INAV SITL exposes its UARTs as TCP ports (UART1 is 5760, UART2 5761, UART3 5762, ...). Set one up as a CRSF serial receiver and point crsfproxy straight at it:

```bash
python3 crsfproxy.py --device socket://127.0.0.1:5762
python3 joystick_crsf.py --target 127.0.0.1
```

No PTY or `socat` needed.

For a fuller fake receiver, use `crsf_rx_sim.py`. It sends link statistics as well as RC, and can simulate RC timeout, RF loss and serial loss separately. You can run two of them for dual-receiver testing. See [SITL_RECEIVER.md](SITL_RECEIVER.md) and [examples/dual_receiver_commands.sh](examples/dual_receiver_commands.sh).

## 🔀 Multiple modules

One proxy per module, each on its own port:

```bash
python3 crsfproxy.py --device /dev/ttyACM0 --port 60000
python3 crsfproxy.py --device /dev/ttyUSB0 --port 60010
```

## ⚙️ Options

| Flag | Default | Description |
|---|---|---|
| `--radio` | `elrs` | `elrs` or `mlrs` |
| `--device` | `/dev/ttyUSB0` | serial port, or `socket://host:port` |
| `--baud` | `115200` (mLRS `400000`) | handset-side baud; ignored over `socket://` |
| `--host` | `0.0.0.0` | address to listen on |
| `--port` | `60000` | UDP port |
| `--tx_rate` | `100` | max RC frames per second |
| `--loop_hz` | `250` | main loop rate; keep it above `--tx_rate` |
| `--rc-hold-ms` | `0` | repeat the last RC sample this long after input stops |
| `--rc_owner_timeout_ms` | `1000` | silence before someone else can take the sticks |
| `--telemetry` | | telemetry + status destination, repeatable |
| `--telemetry_raw` | | bare CRSF telemetry destination, repeatable (MWP) |
| `--status_hz` | `5` | status packets per second |
| `--debug` | off | log every frame, request and drop |

## 📝 Notes & gotchas

- **Baud:** ELRS locks onto the first baud it hears after power-up. If you change `--baud`, power-cycle the module.
- **Fast packet rates:** at 115200 ELRS hides rates like 333 Hz and 500 Hz because they don't fit through the serial link. Use 921600 to see them all. 400000 is the easy-going choice for configuring.
- 🐛 Some CP2102 USB adapters misbehave between 400k and 460k baud.
- ⏱️ `--tx_rate` can't beat `--loop_hz`: with the default loop, asking for 300 gets about 244. Raise both together.
- **Two different packet counters:** in `info`, `packets_good/bad` is the serial link from crsfproxy to the module. The `Bad/Good` setting is the radio link from the module to the receiver.
- **Receiver Link Mode = MAVLink:** the receiver then sends RC to the flight controller as MAVLink at 460800, so set that UART to MAVLink with Serial RX off. crsfproxy itself always sends CRSF.

> [!WARNING]
> There is **no authentication**. Bind `--host` to a trusted interface, run it over a VPN, and firewall the port.

## 📌 Version compatibility

**crsfproxy v0.4, verified with ExpressLRS 4.0.1** on a RadioMaster Nomad X-Band with a 900 MHz receiver on an INAV flight controller. mLRS support is implemented; check it against your firmware.

Configuration only depends on the parts of CRSF that are fixed by the spec: the frame codec and the read/write/command exchange. The settings themselves are read from the module, so new, renamed or reordered settings in another ELRS release need no changes here. What *would* need a code change is a change to the protocol itself, like a new parameter type. That's rare, and it's why the README names the version that has actually been tested rather than claiming everything. If your version reads and writes correctly, it works.

**Upgrading from v0.3:** v0.4 changed the wire format. Configuration now shares the RC port instead of using port + 1, RC is a real CRSF frame instead of the old 40-byte packet, `--telemetry_udp` became `--telemetry_raw`, and a single RC owner is enforced. Old v0.3 clients won't talk to v0.4. v0.4 also fixes reading from `socket://` devices. The details are in [PROTOCOL.md](PROTOCOL.md#changes-from-v03).

## 🗂️ Repository layout

```text
crsfproxy.py          the proxy
joystick_crsf.py      joystick RC client
config_client.py      configuration client (CLI + TUI)
crsf_rx_sim.py        virtual CRSF receiver for INAV SITL

lib/                  server loop, CRSF codec, ELRS and mLRS backends
tests/                unit tests
examples/             launch scripts
```

```bash
python3 -m unittest discover -s tests -v
```

## 📄 License

[GPL-3.0](LICENSE), the same as ExpressLRS.

## Attribution

- Yours truly
- Claude Fable 5, Opus 5.5
- Deepseek 4.1 Flash, Flash 1017
- Codex Sol 5.6

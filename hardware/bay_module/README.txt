# ESP32C6 Bay module
This simple module allows you to directly tunnel to the transmitter from your EdgeTX controller. 

* 2.4GHz WIFI
* 5.8GHz WIFI
* Bluetooth
* USB cable


## C6 bay module notes (discussion only, nothing built)

- **Role:** a virtual TX module in the radio's JR bay, on the module side of the half-duplex bus. It speaks PROTOCOL.md as a client and must be a `--telemetry` destination to get the module's replies.
- **Reference:** ExpressLRS `src/lib/Handset/CRSFHandset.cpp`:
  - reply after each good frame;
  - byte budget baud / 10 / rate × 87 %;
  - long frames split across gaps;
  - `0x3A` timing every 200 ms.
- **Pad layer:** reuse the P4's `main/crsf_wire.c` (or the `esp32-crsf-duplex` sketches) and add listen-then-reply.
- **Handled locally:**
  - `0x3A` timing, using the rate read from the remote module's `0x3A`;
  - the baud negotiation command.
- **Own settings:**
  - Answer as CRSF device `0x12` (`CRSF_ADDRESS_BLUETOOTH_WIFI`); `elrs.lua` lists it under "Other Devices".
  - STRING fields are not editable there, so the WiFi password has to be set another way.
- **Hardware:**
  - a buck regulator from VBAT, not a linear one;
  - measure the S.Port idle voltage first;
  - a C5 (5 GHz WiFi) if the RF link is 2.4 GHz.
- **Open:**
  - radio and bay type;
  - C6 or C5;
  - repo (the owner's placeholder is `hardware/bay_module`);
  - licence (GPL-3 if code is ported from ELRS);
  - VPN membership for an ESP32 (NetBird has no ESP32 client; plain WireGuard exists).

#!/usr/bin/env python3
"""Remote ELRS Lua configuration client for crsfproxy.

  python3 config_client.py --port 60001 --sys-id 1 info
  python3 config_client.py --host radio.local --port 60001 --sys-id 2 get "Packet Rate"
  python3 config_client.py --host radio.local --port 60001 set "Packet Rate" "333Hz Full(-105dBm)"
  python3 config_client.py --host radio.local --port 60001 command Bind --confirm
  python3 config_client.py --host radio.local --port 60001 tui
"""

import argparse
import curses
import json
import socket
import time

from udp_mux import SYS_ID_BROADCAST, unwrap, validate_target_sys_id, wrap


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 60001
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_BROADCAST_WAIT_S = 2.0
UDP_RESPONSE_LIMIT = 65507
WRITABLE_TYPES = {"UINT8", "INT8", "UINT16", "INT16", "UINT32", "INT32",
                  "UINT64", "INT64", "FLOAT", "SELECTION"}


def _request_payload(sys_id: int, request: dict) -> bytes:
    payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
    return wrap(validate_target_sys_id(sys_id), payload)


def _decode_response(response: bytes) -> tuple[int, dict]:
    source_sys_id, payload = unwrap(response)
    if source_sys_id == SYS_ID_BROADCAST:
        raise ValueError("configuration response used reserved source sys_id 0")
    return source_sys_id, json.loads(payload)


def udp_request(host: str, port: int, timeout: float, sys_id: int, request: dict) -> dict:
    target_sys_id = validate_target_sys_id(sys_id)
    if target_sys_id == SYS_ID_BROADCAST:
        raise ValueError("udp_request requires a specific sys_id; use udp_request_all for 0")
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(_request_payload(target_sys_id, request), (host, port))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no configuration response from sys_id {target_sys_id}")
            sock.settimeout(remaining)
            try:
                response, sender = sock.recvfrom(UDP_RESPONSE_LIMIT)
            except socket.timeout as error:
                raise TimeoutError(
                    f"no configuration response from sys_id {target_sys_id}"
                ) from error
            source_sys_id, decoded = _decode_response(response)
            if source_sys_id != target_sys_id:
                continue
            if not decoded["ok"]:
                raise RuntimeError(
                    f"proxy={sender[0]}:{sender[1]} sys_id={source_sys_id} "
                    f"error={decoded['error']}"
                )
            return decoded["result"]


def udp_request_all(host: str, port: int, timeout: float, broadcast_wait: float,
                    request: dict) -> dict[int, dict]:
    responses: dict[int, dict] = {}
    first_deadline = time.monotonic() + timeout
    quiet_deadline = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(_request_payload(SYS_ID_BROADCAST, request), (host, port))
        while True:
            deadline = first_deadline if quiet_deadline is None else quiet_deadline
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                response, _sender = sock.recvfrom(UDP_RESPONSE_LIMIT)
            except socket.timeout:
                break
            source_sys_id, decoded = _decode_response(response)
            responses[source_sys_id] = decoded
            quiet_deadline = time.monotonic() + broadcast_wait
    if not responses:
        raise TimeoutError("no configuration responses to broadcast sys_id 0")
    return responses


def print_result(result: dict) -> None:
    if "device" in result:
        device = result["device"]
        print(
            f"model={device['name']!r} version={device['version']} "
            f"software={device['software_version']} hardware={device['hardware_version']} "
            f"serial={device['serial']!r} parameters={device['parameter_count']}"
        )
    if "radio_rate_hz" in result:
        print(f"handset_rate_hz={result['radio_rate_hz']}")
    if "current_band" in result:
        print(
            f"band={result['current_band']!r} packet_rate={result['packet_rate']!r} "
            f"mode={result['mode']!r} model_id={result['model_id']!r} "
            f"telemetry_ratio={result['telemetry_ratio']!r} "
            f"firmware_hash={result['firmware_hash']!r}"
        )
    if result.get("binding") is not None:
        binding = result["binding"]
        print(
            f"connected={binding['connected']} flags={binding['flags']} "
            f"packets_good={binding['packets_good']} packets_bad={binding['packets_bad']} "
            f"message={binding['message']!r}"
        )
    if "configuration" in result:
        for name, value in result["configuration"].items():
            print(f"{name}={value!r}")
    if "devices" in result:
        for device in result["devices"]:
            print(
                f"address=0x{device['address']:02X} model={device['name']!r} "
                f"version={device['version']} parameters={device['parameter_count']}"
            )
    if "parameters" in result:
        for parameter in result["parameters"]:
            print(
                f"id={parameter['id']} parent={parameter['parent']} "
                f"type={parameter['type']} hidden={int(parameter['hidden'])} "
                f"name={parameter['name']!r} value={parameter['value']!r} "
                f"options={';'.join(parameter['options'])!r}"
            )
    if "parameter" in result:
        parameter = result["parameter"]
        print(
            f"id={parameter['id']} type={parameter['type']} name={parameter['name']!r} "
            f"value={parameter['value']!r} options={';'.join(parameter['options'])!r}"
        )
    if "verified" in result:
        print(f"old={result['old_value']!r} verified={int(result['verified'])}")


def choose_option(screen, parameter: dict) -> str | None:
    options = [
        (raw_index, option)
        for raw_index, option in enumerate(parameter["options"])
        if option
    ]
    selected = next(
        index for index, (raw_index, _) in enumerate(options)
        if raw_index == parameter["raw_value"])
    while True:
        screen.erase()
        screen.addstr(
            0, 0,
            f"{parameter['name']} — Up/Down select, Right/Enter apply, Left/Escape back")
        height, width = screen.getmaxyx()
        top = max(0, selected - height + 3)
        for row, (_, option) in enumerate(options[top:top + height - 2], 1):
            index = top + row - 1
            marker = "> " if index == selected else "  "
            screen.addnstr(row, 0, marker + option, width - 1,
                           curses.A_REVERSE if index == selected else curses.A_NORMAL)
        key = screen.getch()
        if key in (curses.KEY_LEFT, 27):
            return None
        if key == curses.KEY_UP:
            selected = (selected - 1) % len(options)
        if key == curses.KEY_DOWN:
            selected = (selected + 1) % len(options)
        if key in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13):
            return options[selected][1]


def prompt_value(screen, parameter: dict) -> str:
    screen.erase()
    screen.addstr(0, 0, f"Set {parameter['name']} (current {parameter['value']!r}): ")
    curses.echo()
    value = screen.getstr(0, len(f"Set {parameter['name']} (current {parameter['value']!r}): "))
    curses.noecho()
    return value.decode("utf-8")


def run_tui(screen, host: str, port: int, timeout: float, sys_id: int) -> None:
    curses.curs_set(0)
    selected = 0
    status = ""
    parent_stack = [0]
    while True:
        result = udp_request(host, port, timeout, sys_id, {"command": "params"})
        if "binding" not in result:
            status_result = udp_request(host, port, timeout, sys_id, {"command": "info"})
            result["binding"] = status_result["binding"]
        all_parameters = result["parameters"]
        while True:
            parameters = [
                parameter for parameter in all_parameters
                if not parameter["hidden"] and parameter["parent"] == parent_stack[-1]
            ]
            selected = min(selected, len(parameters) - 1)
            screen.erase()
            height, width = screen.getmaxyx()
            folder_name = result["device"]["name"]
            if parent_stack[-1]:
                folder_name = next(
                    parameter["name"] for parameter in all_parameters
                    if parameter["id"] == parent_stack[-1])
            title = (
                f"{folder_name} — Up/Down select, Right/Enter open, Left/Escape back"
            )
            screen.addnstr(0, 0, title, width - 1, curses.A_BOLD)
            binding = result.get("binding")
            if binding is None:
                banner = "ELRS status unavailable"
            else:
                state = "Connected" if binding["connected"] else "Not connected"
                banner = binding["message"] or state
                banner += (
                    f"  [{binding['flags']}, good={binding['packets_good']}, "
                    f"bad={binding['packets_bad']}]"
                )
            screen.addnstr(1, 0, banner, width - 1, curses.A_BOLD)
            top = max(0, selected - height + 5)
            for row, parameter in enumerate(parameters[top:top + height - 4], 2):
                index = top + row - 2
                value = parameter["value"]
                if parameter["type"] == "COMMAND":
                    value = parameter["command_info"] or "run"
                if parameter["type"] == "FOLDER":
                    value = ">"
                line = f"{parameter['id']:2d} {parameter['name']:<24.24} {value!s:<28.28} {parameter['type']}"
                screen.addnstr(row, 0, line, width - 1,
                               curses.A_REVERSE if index == selected else curses.A_NORMAL)
            screen.addnstr(height - 1, 0, status, width - 1)
            key = screen.getch()
            if key in (curses.KEY_LEFT, 27):
                if len(parent_stack) == 1:
                    return
                parent_stack.pop()
                selected = 0
                status = ""
                continue
            if key == curses.KEY_UP:
                selected = (selected - 1) % len(parameters)
            if key == curses.KEY_DOWN:
                selected = (selected + 1) % len(parameters)
            if key == ord("r"):
                break
            if key in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13):
                parameter = parameters[selected]
                if parameter["type"] == "FOLDER":
                    parent_stack.append(parameter["id"])
                    selected = 0
                    status = ""
                    continue
                if parameter["type"] == "SELECTION":
                    value = choose_option(screen, parameter)
                    if value is None:
                        status = f"edit {parameter['name']!r} cancelled"
                        continue
                    response = udp_request(host, port, timeout, sys_id, {
                        "command": "set",
                        "parameter": str(parameter["id"]),
                        "value": value,
                    })
                    status = (
                        f"{parameter['name']}={response['parameter']['value']!r} "
                        f"verified={int(response['verified'])}"
                    )
                    break
                if parameter["type"] in WRITABLE_TYPES:
                    value = prompt_value(screen, parameter)
                    response = udp_request(host, port, timeout, sys_id, {
                        "command": "set",
                        "parameter": str(parameter["id"]),
                        "value": value,
                    })
                    status = (
                        f"{parameter['name']}={response['parameter']['value']!r} "
                        f"verified={int(response['verified'])}"
                    )
                    break
                if parameter["type"] == "COMMAND":
                    screen.erase()
                    screen.addstr(
                        0, 0,
                        f"Run {parameter['name']!r}? {parameter['command_info']} [y/N] ")
                    confirm = screen.getch() in (ord("y"), ord("Y"))
                    if confirm:
                        udp_request(host, port, timeout, sys_id, {
                            "command": "command",
                            "parameter": str(parameter["id"]),
                            "confirm": True,
                        })
                        status = f"command {parameter['name']!r} completed"
                        break
                status = f"{parameter['name']!r} is read-only"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--sys-id", "--sys_id", dest="sys_id", type=int, default=1,
                        help="Target system ID: 0=all, 1..254=specific proxy")
    parser.add_argument("--broadcast-wait", type=float, default=DEFAULT_BROADCAST_WAIT_S,
                        help="Seconds of quiet after the last reply before broadcast collection ends")
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info")
    commands.add_parser("devices")
    commands.add_parser("params")
    get_parser = commands.add_parser("get")
    get_parser.add_argument("parameter")
    set_parser = commands.add_parser("set")
    set_parser.add_argument("parameter")
    set_parser.add_argument("value")
    command_parser = commands.add_parser("command")
    command_parser.add_argument("parameter")
    command_parser.add_argument("--confirm", action="store_true")
    commands.add_parser("tui")
    args = parser.parse_args()
    try:
        sys_id = validate_target_sys_id(args.sys_id)
    except ValueError as error:
        parser.error(str(error))
    if args.broadcast_wait <= 0:
        parser.error("--broadcast-wait must be greater than 0")

    if args.command == "tui":
        if sys_id == SYS_ID_BROADCAST:
            parser.error("tui requires --sys-id 1..254")
        curses.wrapper(run_tui, args.host, args.port, args.timeout, sys_id)
        return 0

    request = {"command": args.command}
    if hasattr(args, "parameter"):
        request["parameter"] = args.parameter
    if hasattr(args, "value"):
        request["value"] = args.value
    if hasattr(args, "confirm"):
        request["confirm"] = args.confirm
    if sys_id == SYS_ID_BROADCAST:
        responses = udp_request_all(
            args.host, args.port, args.timeout, args.broadcast_wait, request)
        if args.json:
            print(json.dumps({str(key): value for key, value in sorted(responses.items())}, indent=2))
        else:
            for source_sys_id, response in sorted(responses.items()):
                print(f"sys_id={source_sys_id}")
                if response["ok"]:
                    print_result(response["result"])
                else:
                    print(f"error={response['error']}")
        for response in responses.values():
            if not response["ok"]:
                return 2
            result = response["result"]
            if "verified" in result and not result["verified"]:
                return 2
        return 0

    result = udp_request(args.host, args.port, args.timeout, sys_id, request)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_result(result)
    if "verified" in result and not result["verified"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

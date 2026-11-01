#!/usr/bin/env python3
"""Remote crsfproxy protocol v1 CONTROL client.

  python3 config_client.py info
  python3 config_client.py --host radio.local get "Packet Rate"
  python3 config_client.py --host radio.local set "Packet Rate" "333Hz Full(-105dBm)"
  python3 config_client.py --host radio.local command Bind --confirm
  python3 config_client.py --host radio.local tui

The server's single protocol port defaults to 60000/udp. Requests are
Kind.CONTROL envelopes; replies are Kind.CONTROL_REPLY envelopes matched by
session and seq.
"""

import argparse
import curses
import json
import socket
import sys
import time

from lib import envelope
from lib.crsf_protocol import CommandStatus


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 60000
DEFAULT_TIMEOUT_S = 30.0
CONTROL_RETRY_INTERVAL_S = 1.0
UDP_RESPONSE_LIMIT = 65507
SPINNER = "|/-\\"
WRITABLE_TYPES = {"UINT8", "INT8", "UINT16", "INT16", "UINT32", "INT32",
                  "UINT64", "INT64", "FLOAT", "SELECTION"}


class ControlError(RuntimeError):
    """A CONTROL request failed or timed out."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ControlClient:
    """One-session CONTROL client on one connected UDP socket."""

    RETRY_INTERVAL_S = CONTROL_RETRY_INTERVAL_S

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = DEFAULT_TIMEOUT_S,
                 retry_interval_s: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retry_interval_s = (
            self.RETRY_INTERVAL_S if retry_interval_s is None else float(retry_interval_s))
        if self.retry_interval_s <= 0:
            raise ValueError(f"retry_interval_s={self.retry_interval_s} must be positive")
        self.session = envelope.new_session()
        self.seq = 0
        self._closed = False
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("", 0))
        self._socket.connect((host, port))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._socket.close()

    def request(self, request: dict, timeout: float | None = None) -> dict:
        """Send one request, retrying the same datagram, and return its result."""
        if self._closed:
            raise ValueError("ControlClient is closed")
        effective_timeout = self.timeout if timeout is None else float(timeout)
        if effective_timeout <= 0:
            raise ValueError(f"timeout={effective_timeout} must be positive")
        seq = self.seq
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
        datagram = envelope.encode(
            envelope.Kind.CONTROL, self.session, seq, payload)
        deadline = time.monotonic() + effective_timeout
        retry_at = time.monotonic()
        last_socket_error: str | None = None

        while True:
            now = time.monotonic()
            if now >= deadline:
                self.seq = (seq + 1) & 0xFFFFFFFF
                message = f"no CONTROL_REPLY within {effective_timeout:g}s"
                if last_socket_error:
                    message += f" (last socket error: {last_socket_error})"
                raise ControlError("timeout", message)
            if now >= retry_at:
                try:
                    self._socket.send(datagram)
                except OSError as error:
                    last_socket_error = str(error)
                retry_at = now + self.retry_interval_s
            wait = min(retry_at, deadline) - time.monotonic()
            if wait <= 0:
                continue
            self._socket.settimeout(wait)
            try:
                data = self._socket.recv(UDP_RESPONSE_LIMIT)
            except socket.timeout:
                continue
            except OSError as error:
                last_socket_error = str(error)
                continue
            try:
                message = envelope.decode(data)
            except ValueError:
                continue
            if (message.kind != envelope.Kind.CONTROL_REPLY
                    or message.session != self.session
                    or message.seq != seq):
                continue
            try:
                reply = json.loads(message.payload)
                if not isinstance(reply, dict):
                    raise ValueError("CONTROL_REPLY payload is not a JSON object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self.seq = (seq + 1) & 0xFFFFFFFF
                raise ControlError("bad_reply", str(error)) from error
            if reply.get("ok") is True:
                self.seq = (seq + 1) & 0xFFFFFFFF
                result = reply.get("result")
                return result if isinstance(result, dict) else {}
            error = reply.get("error")
            if isinstance(error, dict):
                code = str(error.get("code", "failed"))
                error_message = str(error.get("message", ""))
            else:
                code = "failed"
                error_message = str(error)
            if code == "busy":
                continue
            self.seq = (seq + 1) & 0xFFFFFFFF
            raise ControlError(code, error_message)


def fetch_all_params(client: ControlClient) -> dict:
    """Fetch every compact parameter page and return one combined result."""
    parameters: list[dict] = []
    combined: dict = {}
    offset = 0
    while True:
        page = client.request({"command": "params", "offset": offset})
        page_parameters = page.get("parameters")
        if not isinstance(page_parameters, list):
            raise ControlError("bad_reply", "params reply has no parameters list")
        for key, value in page.items():
            if key not in ("parameters", "offset", "total", "more"):
                combined[key] = value
        parameters.extend(page_parameters)
        if not page.get("more"):
            combined["parameters"] = parameters
            combined["offset"] = 0
            combined["total"] = page.get("total", len(parameters))
            combined["more"] = False
            return combined
        if not page_parameters:
            raise ControlError(
                "bad_reply",
                f"params page offset={offset} more=1 but returned no parameters")
        offset += len(page_parameters)


def command_request(client: ControlClient, parameter: str,
                    step: CommandStatus, timeout: float | None = None) -> dict:
    """Send one COMMAND step (crsf_protocol.COMMAND_STEPS); returns the parameter's new state."""
    return client.request({
        "command": "command",
        "parameter": parameter,
        "step": step.name.lower(),
    }, timeout)["parameter"]


def poll_delay_s(state: dict) -> float:
    """Wait before the next POLL: the device's command_timeout, in 10 ms units (elrs.lua field.timeout)."""
    return (state["command_timeout"] or 10) / 100


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
    if "devices" in result:
        for device in result["devices"]:
            print(
                f"address=0x{device['address']:02X} model={device['name']!r} "
                f"version={device['version']} parameters={device['parameter_count']}"
            )
    if "parameters" in result:
        for parameter in result["parameters"]:
            options = parameter.get("options")
            suffix = f" options={';'.join(options)!r}" if options is not None else ""
            print(
                f"id={parameter['id']} parent={parameter['parent']} "
                f"type={parameter['type']} hidden={int(parameter['hidden'])} "
                f"name={parameter['name']!r} value={parameter['value']!r}{suffix}"
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


def draw_popup(screen, lines: list[str]) -> None:
    screen.erase()
    width = screen.getmaxyx()[1]
    for row, text in enumerate(lines):
        screen.addnstr(row, 0, text, width - 1, curses.A_BOLD if row == 0 else curses.A_NORMAL)
    screen.refresh()


def run_command_popup(screen, client: ControlClient, timeout: float, parameter: dict) -> str:
    """Run a COMMAND like elrs.lua's popup; returns the status-line text.

    Shows the device's own confirmation text and progress info, polls every
    command_timeout, and sends CANCEL when the user presses Esc/Left (RTN in
    elrs.lua) while it runs.
    """
    name = parameter["name"]
    target = str(parameter["id"])
    cancelled = False
    spin = 0
    state = command_request(client, target, CommandStatus.START, timeout)
    while state["command_status"] != CommandStatus.READY:
        if state["command_status"] == CommandStatus.CONFIRMATION_NEEDED:
            draw_popup(screen, [name, state["command_info"], "", "[y] confirm   [any other key] cancel"])
            step = CommandStatus.CONFIRM if screen.getch() in (ord("y"), ord("Y")) else CommandStatus.CANCEL
            cancelled = step == CommandStatus.CANCEL
            state = command_request(client, target, step, timeout)
            continue
        draw_popup(screen, [name, f"{state['command_info']} [{SPINNER[spin % len(SPINNER)]}]", "",
                            "[Esc] cancel"])
        spin += 1
        screen.timeout(int(poll_delay_s(state) * 1000))
        key = screen.getch()
        screen.timeout(-1)
        if key in (27, curses.KEY_LEFT):
            cancelled = True
            state = command_request(client, target, CommandStatus.CANCEL, timeout)
        else:
            state = command_request(client, target, CommandStatus.POLL, timeout)
    if cancelled:
        return f"command {name!r} cancelled"
    info = state["command_info"].strip()
    return f"command {name!r} finished" + (f": {info}" if info else "")


def run_tui(screen, client: ControlClient, timeout: float) -> None:
    curses.curs_set(0)
    curses.set_escdelay(25)
    selected = 0
    status = ""
    parent_stack = [0]
    while True:
        result = fetch_all_params(client)
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
                # A failed action (CONTROL error reply, or no reply in time) is
                # shown on the status line; the TUI keeps running.
                try:
                    if parameter["type"] == "SELECTION":
                        full_parameter = client.request({
                            "command": "get",
                            "parameter": str(parameter["id"]),
                        }, timeout)["parameter"]
                        value = choose_option(screen, full_parameter)
                        if value is None:
                            status = f"edit {parameter['name']!r} cancelled"
                            continue
                        response = client.request({
                            "command": "set",
                            "parameter": str(parameter["id"]),
                            "value": value,
                        }, timeout)
                        status = (
                            f"{parameter['name']}={response['parameter']['value']!r} "
                            f"verified={int(response['verified'])}"
                        )
                        break
                    if parameter["type"] in WRITABLE_TYPES:
                        value = prompt_value(screen, parameter)
                        response = client.request({
                            "command": "set",
                            "parameter": str(parameter["id"]),
                            "value": value,
                        }, timeout)
                        status = (
                            f"{parameter['name']}={response['parameter']['value']!r} "
                            f"verified={int(response['verified'])}"
                        )
                        break
                    if parameter["type"] == "COMMAND":
                        status = run_command_popup(screen, client, timeout, parameter)
                        break
                except ControlError as error:
                    status = f"{parameter['name']!r} failed: {error.code}: {error.message}"
                    continue
                status = f"{parameter['name']!r} is read-only"


def run_command_cli(client: ControlClient, timeout: float, name_or_id: str,
                    assume_yes: bool) -> tuple[dict, int]:
    """Run a COMMAND from the shell; returns (final state, exit code).

    Same loop as the TUI popup: the device's confirmation text is asked on
    stdin (or answered yes by --confirm), progress is printed on one line,
    and Ctrl-C sends CANCEL before exiting.
    """
    target = str(client.request({"command": "get", "parameter": name_or_id}, timeout)
                 ["parameter"]["id"])
    try:
        state = command_request(client, target, CommandStatus.START, timeout)
        while state["command_status"] != CommandStatus.READY:
            if state["command_status"] == CommandStatus.CONFIRMATION_NEEDED:
                if assume_yes:
                    answer = "y"
                else:
                    try:
                        answer = input(f"{state['command_info']} [y/N] ")
                    except EOFError:
                        answer = ""
                if answer.strip().casefold() != "y":
                    return command_request(client, target, CommandStatus.CANCEL, timeout), 1
                state = command_request(client, target, CommandStatus.CONFIRM, timeout)
                continue
            print(f"\r{state['command_info']} (Ctrl-C to cancel)", end="", flush=True)
            time.sleep(poll_delay_s(state))
            state = command_request(client, target, CommandStatus.POLL, timeout)
    except KeyboardInterrupt:
        print()
        return command_request(client, target, CommandStatus.CANCEL, timeout), 130
    print()
    return state, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
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
    command_parser.add_argument("--confirm", action="store_true",
                                help="Answer yes to the device's confirmation prompt")
    commands.add_parser("tui")
    args = parser.parse_args()

    client = ControlClient(args.host, args.port, args.timeout)
    try:
        if args.command == "tui":
            curses.wrapper(run_tui, client, args.timeout)
            return 0
        if args.command == "command":
            state, code = run_command_cli(client, args.timeout, args.parameter, args.confirm)
            print(json.dumps(state, indent=2) if args.json else
                  f"command {state['name']!r} {'finished' if code == 0 else 'cancelled'} "
                  f"status={state['command_status']} info={state['command_info'].strip()!r}")
            return code

        if args.command == "params":
            result = fetch_all_params(client)
        else:
            request = {"command": args.command}
            if hasattr(args, "parameter"):
                request["parameter"] = args.parameter
            if hasattr(args, "value"):
                request["value"] = args.value
            result = client.request(request, args.timeout)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print_result(result)
        if "verified" in result and not result["verified"]:
            return 2
        return 0
    except KeyboardInterrupt:
        return 130
    except ControlError as error:
        print(f"error[{error.code}]: {error.message}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

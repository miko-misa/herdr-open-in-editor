#!/usr/bin/env python3
"""Open a Herdr workspace locally or through an SSH reverse relay."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote, urlsplit


PROTOCOL_VERSION = 1
DEFAULT_REMOTE_PORT = 47831
MAX_MESSAGE_BYTES = 64 * 1024
EDITORS = ("auto", "vscode", "zed")

MIRROR_STATE_DIR = Path.home() / ".local" / "state" / "herdr-mirror"
MIRROR_HOSTS_FILE = Path.home() / ".config" / "herdr-mirror" / "hosts.toml"
MIRROR_SSH_TIMEOUT_SECONDS = 15


class OpenEditorError(RuntimeError):
    """Expected user-facing failure."""


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or "\x00" in value:
        return None
    return value


def select_workspace_path(context: dict[str, Any]) -> str:
    worktree = context.get("worktree")
    checkout_path = (
        _nonempty_string(worktree.get("checkout_path"))
        if isinstance(worktree, dict)
        else None
    )
    candidates = (
        checkout_path,
        _nonempty_string(context.get("workspace_cwd")),
        _nonempty_string(context.get("focused_pane_cwd")),
    )
    path = next((candidate for candidate in candidates if candidate), None)
    if path is None:
        raise OpenEditorError("Herdr did not provide a workspace or pane path")
    if not Path(path).is_absolute():
        raise OpenEditorError(f"refusing non-absolute workspace path: {path!r}")
    return path


def load_plugin_context(env: dict[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    raw = source.get("HERDR_PLUGIN_CONTEXT_JSON", "")
    if not raw:
        raise OpenEditorError("HERDR_PLUGIN_CONTEXT_JSON is missing")
    try:
        context = json.loads(raw)
    except json.JSONDecodeError as error:
        raise OpenEditorError(f"invalid HERDR_PLUGIN_CONTEXT_JSON: {error}") from error
    if not isinstance(context, dict):
        raise OpenEditorError("HERDR_PLUGIN_CONTEXT_JSON must contain an object")
    return context


def is_probably_remote_environment(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return any(source.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))


def load_mirror_maps(state_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Read herdr-mirror's `<host>-map.json` state files, keyed by host name.

    Absent, unreadable or malformed files are skipped rather than raised on:
    this whole path is an opportunistic enhancement, and a mirror that is not
    installed must leave the plugin behaving exactly as it did before.
    """
    directory = MIRROR_STATE_DIR if state_dir is None else state_dir
    maps: dict[str, dict[str, Any]] = {}
    try:
        entries = sorted(directory.glob("*-map.json"))
    except OSError:
        return maps
    for entry in entries:
        try:
            parsed = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(parsed, dict):
            maps[entry.name[: -len("-map.json")]] = parsed
    return maps


def _mirrored_workspace_agrees(
    host_map: dict[str, Any],
    remote_pane_id: str,
    local_workspace_id: str | None,
) -> bool:
    """Whether the map's workspace entry corroborates a pane match.

    Herdr reuses pane ids once a pane is closed, so a map left stale by a
    stopped daemon can name a local id that some unrelated pane now answers to.
    Both halves of the map would have to be stale and agree for that to survive
    this check, and a wrong match here opens somebody's unrelated directory
    without saying anything. Absent data is not evidence of a mismatch, so an
    incomplete map still passes.
    """
    if local_workspace_id is None:
        return True
    workspaces = host_map.get("workspaces")
    if not isinstance(workspaces, dict):
        return True
    entry = workspaces.get(remote_pane_id.split(":", 1)[0])
    if not isinstance(entry, dict):
        return True
    mapped = _nonempty_string(entry.get("localId"))
    return mapped is None or mapped == local_workspace_id


def find_mirrored_pane(
    local_pane_id: str,
    maps: dict[str, dict[str, Any]],
    local_workspace_id: str | None = None,
) -> tuple[str, str] | None:
    """Resolve a local pane id to the `(host, remote pane id)` it mirrors.

    `None` means the pane is an ordinary local one, which is the common case
    and must stay indistinguishable from mirror not being installed at all.
    """
    if not local_pane_id:
        return None
    for host in sorted(maps):
        panes = maps[host].get("panes")
        if not isinstance(panes, dict):
            continue
        for remote_pane_id, entry in sorted(panes.items()):
            if not isinstance(entry, dict):
                continue
            if _nonempty_string(entry.get("localId")) != local_pane_id:
                continue
            if _mirrored_workspace_agrees(maps[host], remote_pane_id, local_workspace_id):
                return host, remote_pane_id
    return None


def mirror_ssh_target(host: str, hosts_file: Path | None = None) -> str:
    """SSH target for a mirror host name.

    `hosts.toml` lets a host be keyed by a label distinct from its SSH target,
    so the key is only the fallback. `tomllib` is 3.11+; on older interpreters
    the key is all we have, which is right whenever the two agree.
    """
    path = MIRROR_HOSTS_FILE if hosts_file is None else hosts_file
    try:
        import tomllib
    except ImportError:
        return host
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return host
    hosts = config.get("hosts")
    entry = hosts.get(host) if isinstance(hosts, dict) else None
    target = _nonempty_string(entry.get("target")) if isinstance(entry, dict) else None
    return target or host


def remote_pane_list_command(target: str) -> list[str]:
    """Argv that asks the mirrored host's Herdr for its panes.

    `BatchMode=yes` matters: this runs detached from any terminal the user can
    answer, so a host that would prompt for a password has to fail rather than
    hang forever holding the action open.
    """
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={MIRROR_SSH_TIMEOUT_SECONDS}",
        "--",
        normalized_ssh_authority(target),
        "herdr pane list",
    ]


def parse_remote_pane_cwd(output: str, remote_pane_id: str) -> str:
    """Pull one pane's working directory out of `herdr pane list` output.

    Scanned line by line because a login shell may print a banner ahead of the
    JSON, and the reply is one object per line.
    """
    answered = False
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        result = message.get("result") if isinstance(message, dict) else None
        panes = result.get("panes") if isinstance(result, dict) else None
        if not isinstance(panes, list):
            continue
        answered = True
        for pane in panes:
            if not isinstance(pane, dict) or pane.get("pane_id") != remote_pane_id:
                continue
            cwd = _nonempty_string(pane.get("cwd")) or _nonempty_string(
                pane.get("foreground_cwd")
            )
            if cwd is None:
                raise OpenEditorError(
                    f"mirrored pane {remote_pane_id} reported no working directory"
                )
            if not cwd.startswith("/"):
                raise OpenEditorError(f"refusing non-absolute remote path: {cwd!r}")
            return cwd
    if not answered:
        raise OpenEditorError(
            "the mirrored host did not answer `herdr pane list`; "
            "check that herdr is on its PATH for non-interactive SSH"
        )
    raise OpenEditorError(
        f"mirrored pane {remote_pane_id} is gone from the remote Herdr; "
        "`herdr-mirror once` will resync the map"
    )


def resolve_mirror_location(
    context: dict[str, Any],
    runner: Callable[[list[str]], str] | None = None,
    state_dir: Path | None = None,
    hosts_file: Path | None = None,
) -> tuple[str, str] | None:
    """`(ssh target, remote path)` when the focused pane is a herdr-mirror pane.

    A mirror pane shows a remote terminal, but every path Herdr reports for it
    describes the local placeholder the stream is painted into, so the ordinary
    workspace path is meaningless here. The remote host is the only thing that
    knows where the pane actually is, so it is asked.

    `None` when the pane is not mirrored, and that is deliberately quiet.
    """
    local_pane_id = _nonempty_string(context.get("focused_pane_id"))
    if local_pane_id is None:
        return None
    mirrored = find_mirrored_pane(
        local_pane_id,
        load_mirror_maps(state_dir),
        _nonempty_string(context.get("workspace_id")),
    )
    if mirrored is None:
        return None
    host, remote_pane_id = mirrored
    target = mirror_ssh_target(host, hosts_file)
    run = _run_remote_pane_list if runner is None else runner
    output = run(remote_pane_list_command(target))
    return target, parse_remote_pane_cwd(output, remote_pane_id)


def _run_remote_pane_list(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=MIRROR_SSH_TIMEOUT_SECONDS * 2,
        )
    except FileNotFoundError as error:
        raise OpenEditorError("ssh is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise OpenEditorError(f"timed out asking {command[-2]} for its panes") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        raise OpenEditorError(
            f"could not reach {command[-2]}: {detail[-1] if detail else 'ssh failed'}"
        )
    return completed.stdout


def normalized_ssh_authority(target: str) -> str:
    target = target.strip()
    if target.startswith("ssh://"):
        parsed = urlsplit(target)
        authority = parsed.netloc
    else:
        authority = target
    if (
        not authority
        or authority.startswith("-")
        or any(character.isspace() or ord(character) < 32 for character in authority)
    ):
        raise OpenEditorError(f"invalid SSH target: {target!r}")
    return authority


def remote_editor_command(
    editor: str,
    target: str,
    path: str,
    executable: str | None = None,
) -> list[str]:
    authority = normalized_ssh_authority(target)
    if editor == "vscode":
        return [
            executable or "code",
            "--remote",
            f"ssh-remote+{authority}",
            path,
        ]
    if editor == "zed":
        encoded_path = quote(path, safe="/~:@!$&'()*+,;=-._")
        return [executable or "zed", f"ssh://{authority}{encoded_path}"]
    raise OpenEditorError(f"unsupported editor: {editor}")


def local_editor_command(
    editor: str,
    path: str,
    executable: str | None = None,
) -> list[str]:
    if editor == "vscode":
        return [executable or "code", "--reuse-window", path]
    if editor == "zed":
        return [executable or "zed", path]
    raise OpenEditorError(f"unsupported editor: {editor}")


def resolve_editor(editor: str, preferred: str = "auto") -> str:
    requested = preferred if editor == "auto" else editor
    if requested != "auto":
        if requested not in EDITORS:
            raise OpenEditorError(f"unsupported editor: {requested}")
        return requested
    for candidate, executable in (("zed", "zed"), ("vscode", "code")):
        if shutil.which(executable):
            return candidate
    raise OpenEditorError(
        "no supported editor CLI found; install `zed` or `code`, "
        "or choose one explicitly"
    )


def launch_detached(command: Sequence[str]) -> None:
    executable = shutil.which(command[0])
    if executable is None and not Path(command[0]).is_file():
        raise OpenEditorError(f"editor executable not found: {command[0]}")
    try:
        subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise OpenEditorError(f"failed to launch {command[0]}: {error}") from error


def _read_json_line(connection: socket.socket) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = connection.recv(min(4096, MAX_MESSAGE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_MESSAGE_BYTES:
            raise OpenEditorError("relay request is too large")
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise OpenEditorError("relay request is empty")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OpenEditorError(f"invalid relay JSON: {error}") from error
    if not isinstance(value, dict):
        raise OpenEditorError("relay message must be an object")
    return value


def _write_json_line(connection: socket.socket, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    connection.sendall(payload + b"\n")


def send_relay_request(
    host: str,
    port: int,
    path: str,
    editor: str,
    timeout: float = 1.0,
) -> None:
    request = {
        "version": PROTOCOL_VERSION,
        "action": "open",
        "path": path,
        "editor": editor,
    }
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            _write_json_line(connection, request)
            response = _read_json_line(connection)
    except OSError as error:
        raise OpenEditorError(f"editor relay is unavailable at {host}:{port}: {error}") from error
    if response.get("ok") is not True:
        message = _nonempty_string(response.get("error")) or "relay rejected the request"
        raise OpenEditorError(message)


class RelayServer:
    def __init__(
        self,
        target: str,
        preferred_editor: str,
        editor_executables: dict[str, str] | None = None,
        launch: Callable[[Sequence[str]], None] = launch_detached,
    ) -> None:
        self.target = target
        self.preferred_editor = preferred_editor
        self.editor_executables = editor_executables or {}
        self.launch = launch
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.25)
        self.local_port = int(self._socket.getsockname()[1])
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stopping.set()
        self._socket.close()
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    request = _read_json_line(connection)
                    self._handle(request)
                    _write_json_line(connection, {"ok": True})
                except Exception as error:
                    _write_json_line(connection, {"ok": False, "error": str(error)})

    def _handle(self, request: dict[str, Any]) -> None:
        if request.get("version") != PROTOCOL_VERSION:
            raise OpenEditorError("unsupported relay protocol version")
        if request.get("action") != "open":
            raise OpenEditorError("unsupported relay action")
        path = _nonempty_string(request.get("path"))
        if path is None or not Path(path).is_absolute():
            raise OpenEditorError("relay path must be absolute")
        requested_editor = _nonempty_string(request.get("editor")) or "auto"
        if requested_editor not in EDITORS:
            raise OpenEditorError(f"unsupported editor: {requested_editor}")
        editor = resolve_editor(requested_editor, self.preferred_editor)
        command = remote_editor_command(
            editor,
            self.target,
            path,
            self.editor_executables.get(editor),
        )
        self.launch(command)


class ReverseTunnel:
    def __init__(
        self,
        target: str,
        remote_port: int,
        local_port: int,
        ssh_bin: str = "ssh",
    ) -> None:
        self.target = target
        self.ssh_bin = ssh_bin
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="herdr-open-editor-"
        )
        self.control_path = str(Path(self._temporary_directory.name) / "ctl")
        self.start_command = reverse_tunnel_start_command(
            ssh_bin,
            self.control_path,
            target,
            remote_port,
            local_port,
        )

    def start(self) -> None:
        try:
            result = subprocess.run(self.start_command, check=False)
        except OSError as error:
            raise OpenEditorError(f"failed to start SSH relay: {error}") from error
        if result.returncode != 0:
            raise OpenEditorError(
                "SSH reverse forwarding failed; verify plain SSH access and "
                "choose another --remote-port if the port is already in use"
            )

    def close(self) -> None:
        subprocess.run(
            [
                self.ssh_bin,
                "-S",
                self.control_path,
                "-O",
                "exit",
                self.target,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self._temporary_directory.cleanup()


def reverse_tunnel_start_command(
    ssh_bin: str,
    control_path: str,
    target: str,
    remote_port: int,
    local_port: int,
) -> list[str]:
    if not 1 <= remote_port <= 65535:
        raise OpenEditorError("remote port must be between 1 and 65535")
    if not 1 <= local_port <= 65535:
        raise OpenEditorError("local port must be between 1 and 65535")
    normalized_ssh_authority(target)
    return [
        ssh_bin,
        "-M",
        "-S",
        control_path,
        "-fNT",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        "-R",
        f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}",
        target,
    ]


def run_request(args: argparse.Namespace) -> int:
    context = load_plugin_context()

    # Ahead of the relay, because the two answer different questions. The relay
    # carries a path from a Herdr running on the far side back to the editor
    # here; a mirror pane is the reverse, a Herdr running here showing a pane
    # that lives on the far side. Handing a mirror's placeholder path to the
    # relay would open the wrong directory rather than fail.
    mirrored = resolve_mirror_location(context)
    if mirrored is not None:
        target, remote_path = mirrored
        editor = resolve_editor(args.editor)
        launch_detached(remote_editor_command(editor, target, remote_path))
        return 0

    path = select_workspace_path(context)
    relay_error: OpenEditorError | None = None
    try:
        send_relay_request(args.relay_host, args.relay_port, path, args.editor)
        return 0
    except OpenEditorError as error:
        relay_error = error

    if is_probably_remote_environment():
        raise OpenEditorError(
            f"{relay_error}; attach through `open_in_editor.py attach ...` "
            "to open the remote checkout locally"
        )

    editor = resolve_editor(args.editor)
    launch_detached(local_editor_command(editor, path))
    return 0


def run_attach(args: argparse.Namespace) -> int:
    relay = RelayServer(
        target=args.target,
        preferred_editor=args.editor,
        editor_executables={
            editor: executable
            for editor, executable in (
                ("vscode", args.vscode_bin),
                ("zed", args.zed_bin),
            )
            if executable
        },
    )
    relay.start()
    tunnel = ReverseTunnel(
        target=args.target,
        remote_port=args.remote_port,
        local_port=relay.local_port,
        ssh_bin=args.ssh_bin,
    )
    try:
        tunnel.start()
        command = [
            args.herdr_bin,
            "--remote",
            args.target,
            "--remote-keybindings",
            args.remote_keybindings,
            *args.herdr_args,
        ]
        try:
            return subprocess.run(command, check=False).returncode
        except OSError as error:
            raise OpenEditorError(f"failed to start Herdr: {error}") from error
    finally:
        tunnel.close()
        relay.close()


def run_relay(args: argparse.Namespace) -> int:
    relay = RelayServer(
        target=args.target,
        preferred_editor=args.editor,
        editor_executables={
            editor: executable
            for editor, executable in (
                ("vscode", args.vscode_bin),
                ("zed", args.zed_bin),
            )
            if executable
        },
    )
    relay.start()
    tunnel = ReverseTunnel(
        target=args.target,
        remote_port=args.remote_port,
        local_port=relay.local_port,
        ssh_bin=args.ssh_bin,
    )
    try:
        tunnel.start()
        print(
            f"editor relay ready for {args.target} on remote port {args.remote_port}",
            flush=True,
        )
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        tunnel.close()
        relay.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open Herdr workspaces in local or SSH-aware editors"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    request = subparsers.add_parser("request", help="run as a Herdr plugin action")
    request.add_argument("--editor", choices=EDITORS, default="auto")
    request.add_argument(
        "--relay-host",
        default=os.environ.get("HERDR_OPEN_EDITOR_RELAY_HOST", "127.0.0.1"),
    )
    request.add_argument(
        "--relay-port",
        type=int,
        default=int(
            os.environ.get("HERDR_OPEN_EDITOR_RELAY_PORT", DEFAULT_REMOTE_PORT)
        ),
    )
    request.set_defaults(handler=run_request)

    for name, help_text in (
        ("attach", "start a relay and attach Herdr to a remote host"),
        ("relay", "run only the relay and SSH reverse tunnel"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("target", help="SSH config alias or user@host")
        command.add_argument("--editor", choices=EDITORS, default="auto")
        command.add_argument("--vscode-bin")
        command.add_argument("--zed-bin")
        command.add_argument("--remote-port", type=int, default=DEFAULT_REMOTE_PORT)
        command.add_argument("--ssh-bin", default="ssh")
        if name == "attach":
            command.add_argument("--herdr-bin", default="herdr")
            command.add_argument(
                "--remote-keybindings",
                choices=("local", "server"),
                default="server",
                help="server is required for remote plugin-action keybindings",
            )
            command.set_defaults(handler=run_attach)
        else:
            command.set_defaults(handler=run_relay)

    return parser


def parse_cli_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args, remaining = parser.parse_known_args(argv)
    if args.command != "attach":
        if remaining:
            parser.error(f"unrecognized arguments: {' '.join(remaining)}")
        return args

    if remaining[:1] == ["--"]:
        remaining = remaining[1:]
    args.herdr_args = remaining
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_cli_args(argv)
    try:
        return int(args.handler(args))
    except OpenEditorError as error:
        print(f"open-in-editor: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

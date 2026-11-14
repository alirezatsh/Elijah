"""SSH-backed Docker inspection and event sync for remote Elijah servers."""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from elijah_models import RemoteServer


class RemoteError(RuntimeError):
    """An SSH connection or remote Docker action could not complete."""


CONTAINER_ACTIONS = frozenset({"start", "stop", "restart"})


@dataclass(frozen=True)
class RemoteContainer:
    name: str
    state: str
    image: str
    container_id: str


def _ssh_args(server: RemoteServer, command: str) -> list[str]:
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-p", str(server.port)]
    if server.identity_file:
        args.extend(("-i", server.identity_file))
    args.extend((f"{server.user}@{server.host}", command))
    return args


def _run(server: RemoteServer, command: str) -> str:
    try:
        result = subprocess.run(_ssh_args(server, command), text=True, capture_output=True, check=False)
    except OSError as error:
        raise RemoteError("OpenSSH client is not installed or is unavailable on PATH.") from error
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "SSH command failed."
        raise RemoteError(message)
    return result.stdout


def list_containers(server: RemoteServer) -> list[RemoteContainer]:
    """List Docker containers without opening a shell on the remote host."""
    output = _run(server, "docker ps -a --format '{{.Names}}\\t{{.Status}}\\t{{.Image}}\\t{{.ID}}'")
    containers: list[RemoteContainer] = []
    for line in output.splitlines():
        values = line.split("\t", 3)
        if len(values) == 4:
            containers.append(RemoteContainer(*values))
    return containers


def read_container_logs(server: RemoteServer, name: str, *, tail: int = 300) -> str:
    """Read a bounded, timestamped log tail for one remote container."""
    safe_name = shlex.quote(name)
    return _run(server, f"docker logs --tail {tail} --timestamps {safe_name} 2>&1")


def manage_container(server: RemoteServer, name: str, action: str) -> str:
    """Apply one explicit lifecycle action to a remote Docker container over SSH."""
    if action not in CONTAINER_ACTIONS:
        raise RemoteError(f"Unsupported container action: {action}")
    return _run(server, f"docker {action} {shlex.quote(name)}")


def read_command_events(server: RemoteServer) -> list[dict]:
    """Pull a remote Elijah command timeline into the central local dashboard."""
    output = _run(server, "cat ~/.elijah-investigator/command-events.json 2>/dev/null || printf '[]'")
    try:
        events = json.loads(output)
    except json.JSONDecodeError as error:
        raise RemoteError("Remote Elijah command timeline is not valid JSON.") from error
    if not isinstance(events, list):
        raise RemoteError("Remote Elijah command timeline has an unexpected format.")
    return [event for event in events if isinstance(event, dict)]

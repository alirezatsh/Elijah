"""Safe, local Docker inspection helpers used by the Elijah TUI."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from elijah_models import DockerTarget


class DockerError(RuntimeError):
    """Docker was unavailable or did not return usable container data."""


CONTAINER_ACTIONS = frozenset({"start", "stop", "restart"})


@dataclass(frozen=True)
class DockerSnapshot:
    target_id: str
    container_ref: str
    status: str
    health: str
    restart_count: int
    image: str
    logs: str = ""
    error: str = ""

    # Handle the is healthy workflow for this component.
    @property
    def is_healthy(self) -> bool:
        return self.status == "running" and self.health not in {"unhealthy", "none"}


# Perform the internal run step for this workflow.
def _run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    except OSError as error:
        raise DockerError("Docker CLI is not installed or is not available on PATH.") from error


# Perform the internal compose container id step for this workflow.
def _compose_container_id(target: DockerTarget) -> str:
    """Resolve a Compose service to its current container ID when possible."""
    if not target.compose_file:
        return ""
    compose_file = Path(target.compose_file).expanduser()
    result = _run(
        ["docker", "compose", "-f", str(compose_file), "ps", "--all", "--quiet", target.name],
        cwd=compose_file.parent,
    )
    return result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else ""


# Perform the internal inspect step for this workflow.
def _inspect(reference: str) -> dict:
    result = _run(["docker", "inspect", reference])
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"Docker could not inspect {reference!r}."
        raise DockerError(message)
    try:
        payload = json.loads(result.stdout)
        return payload[0]
    except (json.JSONDecodeError, IndexError, TypeError) as error:
        raise DockerError("Docker returned an unexpected inspect response.") from error


# Handle the read snapshot workflow for this component.
def read_snapshot(target: DockerTarget, *, include_logs: bool = False, tail: int = 300) -> DockerSnapshot:
    """Inspect a target and optionally collect a bounded tail of combined logs."""
    reference = _compose_container_id(target) or target.name
    try:
        data = _inspect(reference)
    except DockerError as error:
        return DockerSnapshot(target.id, reference, "unavailable", "unknown", 0, "", error=str(error))
    state = data.get("State", {})
    health_data = state.get("Health") or {}
    health = health_data.get("Status", "none")
    image = data.get("Config", {}).get("Image", "unknown")
    logs = ""
    if include_logs:
        result = _run(["docker", "logs", "--tail", str(tail), "--timestamps", reference])
        # Docker commonly writes logs to stderr; both streams are evidence.
        logs = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part).strip()
        if result.returncode != 0:
            logs = logs or "Docker could not read recent logs."
    return DockerSnapshot(
        target.id,
        reference,
        state.get("Status", "unknown"),
        health,
        int(data.get("RestartCount", 0)),
        image,
        logs,
    )


# Handle the manage container workflow for this component.
def manage_container(target: DockerTarget, action: str) -> DockerSnapshot:
    """Apply one explicit lifecycle action, then return the updated Docker state."""
    if action not in CONTAINER_ACTIONS:
        raise DockerError(f"Unsupported container action: {action}")
    reference = _compose_container_id(target) or target.name
    result = _run(["docker", action, reference])
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"Docker could not {action} {reference!r}."
        raise DockerError(message)
    return read_snapshot(target)


# Handle the describe snapshot workflow for this component.
def describe_snapshot(snapshot: DockerSnapshot, *, include_logs: bool = False) -> str:
    """Create bounded, AI-ready evidence without claiming that logs were analyzed locally."""
    lines = [
        f"Container target: {snapshot.container_ref}",
        f"Status: {snapshot.status}",
        f"Health check: {snapshot.health}",
        f"Restart count: {snapshot.restart_count}",
        f"Image: {snapshot.image or 'unknown'}",
    ]
    if snapshot.error:
        lines.extend(("", "Docker inspection error:", snapshot.error))
    if include_logs:
        lines.extend(("", "Recent Docker logs:", snapshot.logs or "(no log lines returned)"))
    return "\n".join(lines)

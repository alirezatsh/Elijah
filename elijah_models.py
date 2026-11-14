"""Small, serializable domain models used by Elijah."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Incident:
    id: str
    title: str
    command: str
    evidence: str
    source: str
    created: float
    status: str = "new"
    analysis: str = ""
    model: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class FixNote:
    id: str
    title: str
    command: str
    failure: str
    resolution: str
    prevention: str
    created: float
    tags: list[str] = field(default_factory=list)
    status: str = "active"


@dataclass
class CommandEvent:
    command: str
    cwd: str
    exit_code: int
    duration_ms: int
    created: float


@dataclass
class DockerTarget:
    """A container or Compose service that Elijah should remember locally."""

    id: str
    name: str
    compose_file: str
    created: float
    enabled: bool = True


@dataclass
class RemoteServer:
    """An SSH-reachable Linux server managed from the local Elijah dashboard."""

    id: str
    name: str
    host: str
    user: str
    port: int
    identity_file: str
    created: float

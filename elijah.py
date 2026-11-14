from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
from shell_setup import detect_shell, install as install_other_shell, uninstall as uninstall_other_shell
from elijah_docker import DockerError, DockerSnapshot, describe_snapshot, manage_container as manage_local_container, read_snapshot
from elijah_models import CommandEvent, DockerTarget, FixNote, Incident, RemoteServer
from elijah_remote import RemoteContainer, RemoteError, list_containers, manage_container as manage_remote_container, read_command_events, read_container_logs

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Container, Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, TabbedContent, TabPane, TextArea


APP_DIR = Path.home() / ".elijah-investigator"
INCIDENTS_FILE = APP_DIR / "incidents.json"
FIX_NOTES_FILE = APP_DIR / "fix-notes.json"
CONFIG_FILE = APP_DIR / "config.json"
INBOX_DIR = APP_DIR / "inbox"
EVENTS_FILE = APP_DIR / "command-events.json"
DOCKER_TARGETS_FILE = APP_DIR / "docker-targets.json"
REMOTE_SERVERS_FILE = APP_DIR / "remote-servers.json"

SECRET_PATTERNS = (
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(api[_-]?key[=:]\s*)[^\s'\"]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(password[=:]\s*)[^\s'\"]+", re.I), r"\1[REDACTED]"),
)


def redact(value: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


class SettingsScreen(ModalScreen[dict | None]):
    """Stores settings locally; the API key never appears in the incident archive."""

    CSS = """
    SettingsScreen { align: center middle; background: rgba(3, 7, 15, 0.82); }
    #settings { width: 78; height: auto; border: heavy #64d9ff; background: #10192e; padding: 1 2; }
    #settings-title { color: #64d9ff; text-style: bold; margin-bottom: 1; }
    #settings Input { margin-bottom: 1; }
    #settings-actions { height: auto; grid-size: 2; grid-gutter: 1 1; }
    #settings-actions Button { width: 1fr; }
    .small-note { color: #94a5c8; margin: 1 0; }
    """

    def __init__(self, config: dict) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        with Container(id="settings"):
            yield Label("AI CONNECTION", id="settings-title")
            yield Label("Works with any OpenAI-compatible Chat Completions endpoint.", classes="small-note")
            yield Input(self.config.get("base_url", "https://api.openai.com/v1"), placeholder="Base URL (…/v1)", id="base-url")
            yield Input(self.config.get("model", ""), placeholder="Model name", id="model")
            yield Input(self.config.get("api_key", ""), placeholder="API key", password=True, id="api-key")
            with Grid(id="settings-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save securely", id="save", variant="success")

    @on(Button.Pressed)
    def save_settings(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        values = {
            "base_url": self.query_one("#base-url", Input).value.strip().rstrip("/"),
            "model": self.query_one("#model", Input).value.strip(),
            "api_key": self.query_one("#api-key", Input).value.strip(),
        }
        if not all(values.values()):
            self.notify("Base URL, model, and API key are required.", severity="warning")
            return
        self.dismiss(values)


class CommandQuestion(ModalScreen[str | None]):
    """Ask a focused question about a recorded command without leaving the TUI."""
    CSS = """
    CommandQuestion { align: center middle; background: rgba(3, 7, 15, 0.82); }
    #question-box { width: 78; height: auto; border: heavy #7de2d1; background: #10192e; padding: 1 2; }
    #question-box Input { margin: 1 0; }
    #question-actions { height: auto; grid-size: 2; grid-gutter: 1 1; }
    #question-actions Button { width: 1fr; }
    """

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

    def compose(self) -> ComposeResult:
        with Container(id="question-box"):
            yield Label("ASK COMMAND ADVISOR")
            yield Static(f"[dim]{self.command[:180]}[/]")
            yield Input(placeholder="e.g. Explain this, make it safer, or adapt it for production…", id="command-question")
            with Grid(id="question-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Ask AI", id="ask", variant="success")

    @on(Button.Pressed)
    def submit(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        question = self.query_one("#command-question", Input).value.strip()
        if question:
            self.dismiss(question)
        else:
            self.notify("Write a question first.", severity="warning")


class ContainerActionScreen(ModalScreen[bool]):
    """Make lifecycle changes intentional, for both local and remote Docker."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    ContainerActionScreen { align: center middle; background: rgba(3, 7, 15, 0.82); }
    #container-action { width: 72; height: auto; border: heavy #ffbf69; background: #10192e; padding: 1 2; }
    #container-action-title { color: #ffbf69; text-style: bold; margin-bottom: 1; }
    #container-action-message { color: #dbe4f8; margin-bottom: 1; }
    #container-action-actions { height: auto; grid-size: 2; grid-gutter: 1 1; }
    #container-action-actions Button { width: 1fr; }
    """

    def __init__(self, action: str, target: str, location: str) -> None:
        super().__init__()
        self.action = action
        self.target = target
        self.location = location

    def compose(self) -> ComposeResult:
        with Container(id="container-action"):
            yield Label("CONFIRM CONTAINER ACTION", id="container-action-title")
            yield Static(
                f"Run [b]{self.action}[/b] for [b]{self.target}[/b]?\n"
                f"Target: {self.location}\n\nThis changes the live container state.",
                id="container-action-message",
            )
            with Grid(id="container-action-actions"):
                yield Button("Cancel", id="cancel")
                yield Button(f"Confirm {self.action}", id="confirm", variant="error")

    @on(Button.Pressed)
    def submit(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ClearDataScreen(ModalScreen[bool]):
    """Require an explicit confirmation before clearing a local Elijah collection."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    ClearDataScreen { align: center middle; background: rgba(3, 7, 15, 0.82); }
    #clear-data { width: 74; height: auto; border: heavy #ff6b8a; background: #10192e; padding: 1 2; }
    #clear-data-title { color: #ff6b8a; text-style: bold; margin-bottom: 1; }
    #clear-data-actions { height: auto; grid-size: 2; grid-gutter: 1 1; }
    #clear-data-actions Button { width: 1fr; }
    """

    def __init__(self, collection: str, detail: str) -> None:
        super().__init__()
        self.collection = collection
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Container(id="clear-data"):
            yield Label("CLEAR LOCAL DATA", id="clear-data-title")
            yield Static(
                f"Delete all local {self.collection}?\n\n{self.detail}\n\nThis cannot be undone.",
            )
            with Grid(id="clear-data-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete all", id="confirm", variant="error")

    @on(Button.Pressed)
    def submit(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class IncidentEditor(ModalScreen[dict | None]):
    """A small metadata editor for keeping the archive useful over time."""
    CSS = """
    IncidentEditor { align: center middle; background: rgba(3, 7, 15, 0.82); }
    #incident-editor { width: 74; height: auto; border: heavy #a996ff; background: #10192e; padding: 1 2; }
    #incident-editor Input, #incident-editor Select { margin: 1 0; }
    #incident-editor-actions { height: auto; grid-size: 2; grid-gutter: 1 1; }
    #incident-editor-actions Button { width: 1fr; }
    """

    def __init__(self, incident: Incident) -> None:
        super().__init__()
        self.incident = incident

    def compose(self) -> ComposeResult:
        with Container(id="incident-editor"):
            yield Label("EDIT INCIDENT")
            yield Input(self.incident.title, placeholder="Incident title", id="edit-title")
            yield Input(", ".join(self.incident.tags), placeholder="Tags, comma separated", id="edit-tags")
            yield Select([("Open", "new"), ("Investigated", "investigated"), ("Resolved", "resolved"), ("Archived", "archived")], value=self.incident.status if self.incident.status in {"new", "investigated", "resolved", "archived"} else "new", id="edit-status")
            with Grid(id="incident-editor-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="success")

    @on(Button.Pressed)
    def submit(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self.dismiss({"title": self.query_one("#edit-title", Input).value.strip(), "tags": self.query_one("#edit-tags", Input).value.strip(), "status": str(self.query_one("#edit-status", Select).value)})


class FixNoteEditor(ModalScreen[dict | None]):
    """Edit a saved solution without leaving the personal knowledge base."""

    BINDINGS = [("ctrl+enter", "save_note", "Save fix note"), ("escape", "cancel", "Cancel")]

    CSS = """
    FixNoteEditor { align: center middle; background: rgba(3, 7, 15, 0.82); }
    #fix-note-editor { width: 92; height: 27; border: heavy #7de2d1; background: #10192e; padding: 1 2; }
    #fix-note-editor Input { margin-bottom: 1; }
    #fix-note-editor TextArea { height: 3; margin-bottom: 1; }
    #fix-note-editor-actions { height: auto; grid-size: 2; grid-gutter: 1 1; }
    #fix-note-editor-actions Button { width: 1fr; }
    """

    def __init__(self, item: FixNote) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Container(id="fix-note-editor"):
            yield Label("EDIT FIX NOTE")
            yield Input(self.item.title, placeholder="Title", id="fix-note-title")
            yield Input(self.item.command, placeholder="Command / context", id="fix-note-command")
            yield TextArea(self.item.failure, id="fix-note-failure")
            yield TextArea(self.item.resolution, id="fix-note-resolution")
            yield Input(self.item.prevention, placeholder="Prevention / follow-up", id="fix-note-prevention")
            with Grid(id="fix-note-editor-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="success")

    @on(Button.Pressed)
    def submit(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._dismiss_values()

    def action_save_note(self) -> None:
        self._dismiss_values()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _dismiss_values(self) -> None:
        self.dismiss({
            "title": self.query_one("#fix-note-title", Input).value.strip(),
            "command": self.query_one("#fix-note-command", Input).value.strip(),
            "failure": self.query_one("#fix-note-failure", TextArea).text.strip(),
            "resolution": self.query_one("#fix-note-resolution", TextArea).text.strip(),
            "prevention": self.query_one("#fix-note-prevention", Input).value.strip(),
        })


class Elijah(App):
    """A private incident notebook, not an autonomous shell controller."""

    TITLE = "ELIJAH INVESTIGATOR"
    SUB_TITLE = "Terminal incident intelligence"
    CSS_PATH = "elijah.tcss"
    BINDINGS = [
        ("ctrl+enter", "analyze", "Analyze"),
        ("ctrl+n", "clear_capture", "New capture"),
        ("ctrl+s", "settings", "AI settings"),
        ("a", "archive", "Archive"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.incidents: list[Incident] = []
        self.config: dict[str, str] = {}
        self.events: list[CommandEvent] = []
        self.selected_event_index: int | None = None
        self.pulse_frame = 0
        self.selected_ai_id: str | None = None
        self.fix_notes: list[FixNote] = []
        self.selected_fix_note_id: str | None = None
        self.docker_targets: list[DockerTarget] = []
        self.docker_snapshots: dict[str, DockerSnapshot] = {}
        self.selected_docker_id: str | None = None
        self.remote_servers: list[RemoteServer] = []
        self.remote_containers: list[RemoteContainer] = []
        self.selected_remote_id: str | None = None
        self.selected_remote_container: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="hero"):
            with Vertical(id="brand"):
                yield Static("ELIJAH", id="logo")
                yield Static("INCIDENT INVESTIGATOR", id="product-label")
            yield Static("AI CASES\n--", id="open-stat", classes="stat")
            yield Static("FIX NOTES\n--", id="fix-note-stat", classes="stat")
            yield Static("AI CONNECTION\nSETUP REQUIRED", id="ai-stat", classes="stat")
            yield Static("CAPTURE STATUS\n◐ WATCHING", id="signal-stat", classes="stat signal")
        with TabbedContent(initial="ai-workspace"):
            with TabPane("AI investigator", id="ai-workspace"):
                with Horizontal(id="capture-layout"):
                    with Vertical(id="capture-form"):
                        yield Label("ASK AI TO INVESTIGATE", classes="section-label")
                        yield Input(placeholder="Short title — e.g. API returns 502 from payment service", id="ai-title")
                        yield Input(placeholder="Command / curl / context (optional)", id="ai-command")
                        yield Select([("Terminal output", "terminal"), ("Docker / container logs", "docker"), ("System log", "system"), ("Application log", "app")], value="terminal", id="ai-source")
                        yield TextArea("", id="ai-evidence")
                        with Grid(id="capture-actions"):
                            yield Button("Clear", id="clear-ai")
                            yield Button("Ask AI  Ctrl+Enter", id="analyze-ai", variant="success")
                    with Vertical(id="live-guide"):
                        yield Label("AI ONLY", classes="section-label")
                        yield Static("This workspace sends evidence to your configured OpenAI-compatible provider only after you choose Ask AI.", id="cli-guide")
                        yield Static("Configure the provider with Ctrl+S. Use Fix Notes to save proven solutions without an AI request.", id="privacy-note")
            with TabPane("AI archive", id="ai-archive"):
                with Horizontal(id="archive-layout"):
                    with Vertical(id="ai-list"):
                        yield Select([("All AI cases", "all"), ("Active", "active"), ("Resolved", "resolved"), ("Archived", "archived")], value="all", id="ai-filter")
                        yield DataTable(id="ai-incident-table", cursor_type="row")
                    with Vertical(id="detail"):
                        yield Label("AI INVESTIGATION", id="ai-detail-label")
                        yield Static("Select an AI investigation to inspect it.", id="ai-detail-content")
                        yield Label("ARCHIVE CONTROLS", classes="action-label")
                        with Grid(id="ai-primary-actions"):
                            yield Button("New", id="new-ai", variant="success")
                            yield Button("Run again", id="reanalyze-ai")
                            yield Button("Edit", id="edit-ai")
                            yield Button("Resolve", id="resolve-ai", variant="success")
                            yield Button("Archive", id="archive-ai")
                            yield Button("Delete", id="delete-ai", variant="error")
                        with Grid(id="ai-danger-actions"):
                            yield Button("Clear archive", id="delete-all-ai", variant="error")
            with TabPane("AI command advisor", id="timeline"):
                yield Static("Command metadata is captured locally. Ask AI only when you explicitly submit a question.", id="timeline-note")
                with Horizontal(id="timeline-layout"):
                    yield DataTable(id="event-table", cursor_type="row")
                    with Vertical(id="event-detail"):
                        yield Label("COMMAND ADVISOR", classes="section-label")
                        yield Static("Select a command to inspect it or ask AI about it.", id="event-content")
                        with Grid(id="event-actions"):
                            yield Button("Ask AI about this command", id="ask-command", variant="success")
                            yield Button("Clear timeline", id="delete-all-events", variant="error")
            with TabPane("Container watchlist", id="docker-watchlist"):
                with Horizontal(id="docker-add-row"):
                    yield Input(placeholder="Container name or Compose service", id="docker-name")
                    yield Input(placeholder="Optional path to compose.yml", id="docker-compose-file")
                    yield Button("Remember container", id="save-docker-target", variant="success")
                yield Static("Elijah remembers these containers locally. Refresh checks Docker state only; Analyze reads a bounded tail of recent logs and sends it to AI only after you choose it.", id="docker-guide")
                with Horizontal(id="docker-layout"):
                    with Vertical(id="docker-list"):
                        yield DataTable(id="docker-table", cursor_type="row")
                    with Vertical(id="docker-detail"):
                        yield Label("CONTAINER CONTEXT", classes="section-label")
                        yield Static("Add or select a container to inspect its Docker state.", id="docker-detail-content")
                        yield Label("INSPECT & EVIDENCE", classes="action-group-label")
                        with Grid(id="docker-primary-actions"):
                            yield Button("Refresh", id="refresh-docker")
                            yield Button("Capture", id="capture-docker")
                            yield Button("Analyze", id="analyze-docker", variant="success")
                        yield Label("CONTAINER LIFECYCLE", classes="action-group-label")
                        with Grid(id="docker-control-actions"):
                            yield Button("Start", id="start-docker", variant="success")
                            yield Button("Stop", id="stop-docker", variant="error")
                            yield Button("Restart", id="restart-docker")
                        yield Label("WATCHLIST", classes="action-group-label")
                        with Grid(id="docker-danger-actions"):
                            yield Button("Remove watch", id="delete-docker", variant="error")
                            yield Button("Clear watchlist", id="delete-all-docker", variant="error")
            with TabPane("Servers", id="remote-servers"):
                with Horizontal(id="remote-add-row"):
                    yield Input(placeholder="Server label", id="remote-name")
                    yield Input(placeholder="Host / IP address", id="remote-host")
                    yield Input(placeholder="SSH user", id="remote-user")
                    yield Input("22", placeholder="Port", id="remote-port")
                    yield Input(placeholder="Optional SSH private-key path", id="remote-key")
                    yield Button("Connect", id="save-remote", variant="success")
                yield Static("Use SSH keys or your local SSH agent. Elijah does not store server passwords. Select a server to list its Docker containers or sync its Elijah command timeline.", id="remote-guide")
                with Horizontal(id="remote-layout"):
                    with Vertical(id="remote-server-list"):
                        yield DataTable(id="remote-server-table", cursor_type="row")
                    with Vertical(id="remote-container-list"):
                        yield DataTable(id="remote-container-table", cursor_type="row")
                    with Vertical(id="remote-detail"):
                        yield Label("REMOTE SERVER", classes="section-label")
                        yield Static("Add a server to connect over SSH.", id="remote-detail-content")
                        yield Label("SERVER", classes="action-group-label")
                        with Grid(id="remote-server-actions"):
                            yield Button("Sync commands", id="sync-remote", variant="success")
                            yield Button("Remove server", id="delete-remote", variant="error")
                        with Grid(id="remote-danger-actions"):
                            yield Button("Clear servers", id="delete-all-remote", variant="error")
                        yield Label("THIS SERVER'S COMMANDS", classes="action-label")
                        yield DataTable(id="remote-event-table", cursor_type="row")
                        yield Label("SELECTED CONTAINER", classes="action-group-label")
                        with Grid(id="remote-primary-actions"):
                            yield Button("Refresh", id="refresh-remote")
                            yield Button("Capture", id="capture-remote")
                            yield Button("Analyze", id="analyze-remote", variant="success")
                        yield Label("LIFECYCLE", classes="action-group-label")
                        with Grid(id="remote-control-actions"):
                            yield Button("Start", id="start-remote", variant="success")
                            yield Button("Stop", id="stop-remote", variant="error")
                            yield Button("Restart", id="restart-remote")
            with TabPane("Fix notes", id="fix-note-workspace"):
                with Horizontal(id="capture-layout"):
                    with Vertical(id="capture-form"):
                        yield Label("WRITE WHAT YOU LEARNED", classes="section-label")
                        yield Input(placeholder="Title — e.g. Payment API token had the wrong audience", id="fix-note-title")
                        yield Input(placeholder="Command / context (optional)", id="fix-note-command")
                        yield Label("WHAT FAILED?", classes="field-label")
                        yield TextArea("", id="fix-note-failure")
                        yield Label("WHAT FIXED IT?", classes="field-label")
                        yield TextArea("", id="fix-note-resolution")
                        yield Input(placeholder="Prevention / follow-up (optional)", id="fix-note-prevention")
                        with Grid(id="capture-actions"):
                            yield Button("Clear", id="clear-fix-note")
                            yield Button("Save fix note", id="save-fix-note", variant="success")
                    with Vertical(id="live-guide"):
                        yield Label("YOUR PRIVATE RUNBOOK", classes="section-label")
                        yield Static("Record the actual failure, the fix that worked, and the guardrail that would prevent it. This is a local knowledge base; it never calls AI.", id="fix-note-guide")
                        yield Static("Use it for the details an AI response cannot know: what changed in your environment, what you verified, and the final outcome.", id="fix-note-note")
            with TabPane("Fix notes library", id="fix-note-library"):
                with Horizontal(id="archive-layout"):
                    with Vertical(id="fix-note-list"):
                        yield Select([("All fix notes", "all"), ("Active", "active"), ("Archived", "archived")], value="all", id="fix-note-filter")
                        yield DataTable(id="fix-note-table", cursor_type="row")
                    with Vertical(id="detail"):
                        yield Label("SAVED LESSON", id="fix-note-detail-label")
                        yield Static("Select a fix note to inspect the failure and its proven resolution.", id="fix-note-detail-content")
                        with Grid(id="detail-actions"):
                            yield Button("Edit", id="edit-fix-note")
                            yield Button("Archive", id="archive-fix-note")
                            yield Button("Delete", id="delete-fix-note", variant="error")
                        with Grid(id="fix-danger-actions"):
                            yield Button("Clear library", id="delete-all-fix-notes", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#ai-incident-table", DataTable).add_columns("Investigation", "Source", "State", "Captured")
        self.query_one("#fix-note-table", DataTable).add_columns("Lesson", "Context", "State", "Saved")
        self.query_one("#event-table", DataTable).add_columns("Command", "Exit", "Duration", "Directory", "When")
        self.query_one("#docker-table", DataTable).add_columns("Container", "Compose", "State", "Health", "Restarts")
        self.query_one("#remote-server-table", DataTable).add_columns("Server", "Address", "Status")
        self.query_one("#remote-container-table", DataTable).add_columns("Container", "State", "Image")
        self.query_one("#remote-event-table", DataTable).add_columns("Command", "Exit", "When")
        self.incidents = self._load_incidents()
        self.fix_notes = self._load_fix_notes()
        self.events = self._load_events()
        self.docker_targets = self._load_docker_targets()
        self.remote_servers = self._load_remote_servers()
        self.config = self._load_config()
        self.set_interval(2, self.ingest_inbox)
        self.set_interval(0.35, self._animate_signal)
        self.ingest_inbox()
        self._refresh()

    @on(TabbedContent.TabActivated)
    def refresh_tab_actions(self) -> None:
        self._update_action_states()

    # ---- workspaces --------------------------------------------------
    def action_clear_capture(self) -> None:
        self.query_one("#ai-title", Input).value = ""
        self.query_one("#ai-command", Input).value = ""
        self.query_one("#ai-evidence", TextArea).text = ""
        self.query_one("#ai-title", Input).focus()

    @on(Button.Pressed, "#clear-ai")
    def clear_ai_button(self) -> None:
        self.action_clear_capture()

    def action_analyze(self) -> None:
        if not self._is_configured():
            self.notify("Configure an AI provider with Ctrl+S before using AI Investigator.", severity="warning")
            return
        title = self.query_one("#ai-title", Input).value.strip()
        command = redact(self.query_one("#ai-command", Input).value.strip())
        evidence = redact(self.query_one("#ai-evidence", TextArea).text.strip())
        source = str(self.query_one("#ai-source", Select).value)
        if not evidence:
            self.notify("Paste an error, log, response, or command output first.", severity="warning")
            return
        incident = Incident(id=uuid4().hex, title=title or self._infer_title(evidence, command), command=command, evidence=evidence, source=source, created=time.time(), status="queued")
        self.incidents.insert(0, incident)
        self.selected_ai_id = incident.id
        self._save_incidents()
        self._refresh()
        self.analyze_incident(incident.id)

    @on(Button.Pressed, "#analyze-ai")
    def analyze_ai_button(self) -> None:
        self.action_analyze()

    @on(Button.Pressed, "#clear-fix-note")
    def clear_fix_note(self) -> None:
        for widget_id in ("fix-note-title", "fix-note-command", "fix-note-prevention"):
            self.query_one(f"#{widget_id}", Input).value = ""
        for widget_id in ("fix-note-failure", "fix-note-resolution"):
            self.query_one(f"#{widget_id}", TextArea).text = ""
        self.query_one("#fix-note-title", Input).focus()

    @on(Button.Pressed, "#save-fix-note")
    def save_fix_note(self) -> None:
        title = self.query_one("#fix-note-title", Input).value.strip()
        failure = redact(self.query_one("#fix-note-failure", TextArea).text.strip())
        resolution = redact(self.query_one("#fix-note-resolution", TextArea).text.strip())
        if not title or not failure or not resolution:
            self.notify("Title, failure, and resolution are required.", severity="warning")
            return
        item = FixNote(id=uuid4().hex, title=title, command=redact(self.query_one("#fix-note-command", Input).value.strip()), failure=failure, resolution=resolution, prevention=redact(self.query_one("#fix-note-prevention", Input).value.strip()), created=time.time())
        self.fix_notes.insert(0, item)
        self.selected_fix_note_id = item.id
        self._save_fix_notes()
        self._refresh()
        self.clear_fix_note()
        self.notify("Fix note saved locally.")

    # ---- container watchlist ---------------------------------------
    @on(Button.Pressed, "#save-docker-target")
    def save_docker_target(self) -> None:
        name = self.query_one("#docker-name", Input).value.strip()
        compose_value = self.query_one("#docker-compose-file", Input).value.strip()
        if not name:
            self.notify("Enter a container name or Compose service first.", severity="warning")
            return
        compose_file = ""
        if compose_value:
            candidate = Path(compose_value).expanduser()
            if not candidate.is_file():
                self.notify("Compose path must point to an existing compose file.", severity="warning")
                return
            compose_file = str(candidate.resolve())
        existing = next(
            (item for item in self.docker_targets if item.name == name and item.compose_file == compose_file),
            None,
        )
        if existing:
            self.selected_docker_id = existing.id
            self.notify("That container is already in the watchlist.")
            return
        self.validate_docker_target(DockerTarget(uuid4().hex, name, compose_file, time.time()))

    @work(thread=True, exclusive=True, group="docker", exit_on_error=False)
    def validate_docker_target(self, target: DockerTarget) -> None:
        snapshot = read_snapshot(target)
        self.call_from_thread(self._finish_docker_target_validation, target, snapshot)

    def _finish_docker_target_validation(self, target: DockerTarget, snapshot: DockerSnapshot) -> None:
        if snapshot.error:
            self.notify(f"Container was not added: {snapshot.error}", severity="error")
            return
        self.docker_targets.insert(0, target)
        self.docker_snapshots[target.id] = snapshot
        self.selected_docker_id = target.id
        self._save_docker_targets()
        self._fill_docker_targets()
        self._update_action_states()
        self.query_one("#docker-name", Input).value = ""
        self.query_one("#docker-compose-file", Input).value = ""
        self.notify("Container verified and saved locally.")

    @on(DataTable.RowHighlighted, "#docker-table")
    def select_docker_target(self, event: DataTable.RowHighlighted) -> None:
        self.selected_docker_id = str(event.row_key.value)
        self._render_docker_detail()
        self._update_action_states()

    @on(Button.Pressed, "#refresh-docker")
    def refresh_docker(self) -> None:
        target = self._selected_docker_target()
        if target:
            self.refresh_docker_target(target.id, include_logs=False, action="refresh")

    @on(Button.Pressed, "#capture-docker")
    def capture_docker_logs(self) -> None:
        target = self._selected_docker_target()
        if target:
            self.refresh_docker_target(target.id, include_logs=True, action="capture")

    @on(Button.Pressed, "#analyze-docker")
    def analyze_docker_logs(self) -> None:
        if not self._is_configured():
            self.notify("Configure an AI provider with Ctrl+S before analyzing Docker logs.", severity="warning")
            return
        target = self._selected_docker_target()
        if target:
            self.refresh_docker_target(target.id, include_logs=True, action="analyze")

    @on(Button.Pressed, "#start-docker")
    def start_docker(self) -> None:
        self._request_local_container_action("start")

    @on(Button.Pressed, "#stop-docker")
    def stop_docker(self) -> None:
        self._request_local_container_action("stop")

    @on(Button.Pressed, "#restart-docker")
    def restart_docker(self) -> None:
        self._request_local_container_action("restart")

    def _request_local_container_action(self, action: str) -> None:
        target = self._selected_docker_target()
        if not target:
            self.notify("Select a container first.", severity="warning")
            return
        self.push_screen(
            ContainerActionScreen(action, target.name, "local Docker"),
            lambda confirmed: self._confirm_local_container_action(target.id, action, confirmed),
        )

    def _confirm_local_container_action(self, target_id: str, action: str, confirmed: bool) -> None:
        if confirmed:
            self.run_local_container_action(target_id, action)

    @work(thread=True, exclusive=True, group="docker", exit_on_error=False)
    def run_local_container_action(self, target_id: str, action: str) -> None:
        target = next((item for item in self.docker_targets if item.id == target_id), None)
        if not target:
            return
        try:
            snapshot = manage_local_container(target, action)
            error = ""
        except DockerError as exception:
            snapshot = None
            error = str(exception)
        self.call_from_thread(self._finish_local_container_action, target_id, action, snapshot, error)

    def _finish_local_container_action(
        self, target_id: str, action: str, snapshot: DockerSnapshot | None, error: str
    ) -> None:
        if error:
            self.notify(f"Could not {action} container: {error}", severity="error")
            return
        if snapshot is None:
            return
        self.docker_snapshots[target_id] = snapshot
        self._fill_docker_targets()
        action_name = {
            "start": "started",
            "stop": "stopped",
            "restart": "restarted",
        }[action]
        self.notify(f"Container {snapshot.container_ref} {action_name}.")

    @on(Button.Pressed, "#delete-docker")
    def delete_docker_target(self) -> None:
        target = self._selected_docker_target()
        if not target:
            return
        self.docker_targets = [item for item in self.docker_targets if item.id != target.id]
        self.docker_snapshots.pop(target.id, None)
        self.selected_docker_id = self.docker_targets[0].id if self.docker_targets else None
        self._save_docker_targets()
        self._fill_docker_targets()
        self._update_action_states()
        self.notify("Container removed from the local watchlist.")

    @on(Button.Pressed, "#delete-all-docker")
    def delete_all_docker_targets(self) -> None:
        self._request_clear("docker", "remembered container targets", "This only removes the local watchlist; Docker containers are not deleted.")

    @work(thread=True, exclusive=True, group="docker", exit_on_error=False)
    def refresh_docker_target(self, target_id: str, *, include_logs: bool, action: str) -> None:
        target = next((item for item in self.docker_targets if item.id == target_id), None)
        if not target:
            return
        snapshot = read_snapshot(target, include_logs=include_logs)
        self.call_from_thread(self._handle_docker_snapshot, target_id, snapshot, action)

    def _handle_docker_snapshot(self, target_id: str, snapshot: DockerSnapshot, action: str) -> None:
        self.docker_snapshots[target_id] = snapshot
        self._fill_docker_targets()
        self._update_action_states()
        if action == "refresh":
            self.notify("Docker state refreshed." if not snapshot.error else "Docker is unavailable — see container detail.", severity="warning" if snapshot.error else "information")
            return
        target = next((item for item in self.docker_targets if item.id == target_id), None)
        if not target:
            return
        incident = Incident(
            id=uuid4().hex,
            title=f"Docker logs: {target.name}",
            command=f"docker logs --tail 300 --timestamps {snapshot.container_ref}",
            evidence=redact(describe_snapshot(snapshot, include_logs=True)),
            source="docker", created=time.time(), status="captured",
        )
        self.incidents.insert(0, incident)
        self.selected_ai_id = incident.id
        self._save_incidents()
        self._refresh()
        if action == "capture":
            self.notify("Recent Docker logs saved to the local archive.")
            return
        incident.status = "queued"
        self._save_incidents()
        self._refresh()
        self.analyze_incident(incident.id)

    # ---- remote servers --------------------------------------------
    @on(Button.Pressed, "#save-remote")
    def save_remote_server(self) -> None:
        name = self.query_one("#remote-name", Input).value.strip()
        host = self.query_one("#remote-host", Input).value.strip()
        user = self.query_one("#remote-user", Input).value.strip()
        key_value = self.query_one("#remote-key", Input).value.strip()
        try:
            port = int(self.query_one("#remote-port", Input).value.strip())
        except ValueError:
            self.notify("SSH port must be a number.", severity="warning")
            return
        if not name or not host or not user or not 0 < port < 65536:
            self.notify("Server label, host, SSH user, and a valid port are required.", severity="warning")
            return
        identity_file = ""
        if key_value:
            candidate = Path(key_value).expanduser()
            if not candidate.is_file():
                self.notify("SSH private-key path does not exist.", severity="warning")
                return
            identity_file = str(candidate.resolve())
        if any(item.host == host and item.user == user and item.port == port for item in self.remote_servers):
            self.notify("That SSH server is already registered.", severity="warning")
            return
        server = RemoteServer(uuid4().hex, name, host, user, port, identity_file, time.time())
        self.validate_remote_server(server)

    @work(thread=True, exclusive=True, group="remote", exit_on_error=False)
    def validate_remote_server(self, server: RemoteServer) -> None:
        try:
            containers = list_containers(server)
            error = ""
        except RemoteError as exception:
            containers = []
            error = str(exception)
        self.call_from_thread(self._finish_remote_server_validation, server, containers, error)

    def _finish_remote_server_validation(self, server: RemoteServer, containers: list[RemoteContainer], error: str) -> None:
        if error:
            self.notify(f"Server was not added: {error}", severity="error")
            return
        self.remote_servers.insert(0, server)
        self.remote_containers = containers
        self.selected_remote_id = server.id
        self._save_remote_servers()
        self._fill_remote_servers()
        self._update_action_states()
        for widget_id in ("remote-name", "remote-host", "remote-user", "remote-key"):
            self.query_one(f"#{widget_id}", Input).value = ""
        self.query_one("#remote-port", Input).value = "22"
        self.notify("Server verified and added to the central dashboard.")

    @on(DataTable.RowHighlighted, "#remote-server-table")
    def select_remote_server(self, event: DataTable.RowHighlighted) -> None:
        server_id = str(event.row_key.value)
        # Rebuilding a DataTable re-emits RowHighlighted for its current row.
        # Ignore that synthetic event or it schedules another SSH refresh forever.
        if server_id == self.selected_remote_id:
            return
        self.selected_remote_id = server_id
        self.selected_remote_container = None
        self.remote_containers = []
        self._fill_remote_servers()
        self._update_action_states()
        server = self._selected_remote_server()
        if server:
            self.refresh_remote_server(server.id)

    @on(DataTable.RowHighlighted, "#remote-container-table")
    def select_remote_container(self, event: DataTable.RowHighlighted) -> None:
        self.selected_remote_container = str(event.row_key.value)
        self._render_remote_detail()
        self._update_action_states()

    @on(Button.Pressed, "#refresh-remote")
    def refresh_remote_button(self) -> None:
        server = self._selected_remote_server()
        if server:
            self.refresh_remote_server(server.id)

    @work(thread=True, exclusive=True, group="remote", exit_on_error=False)
    def refresh_remote_server(self, server_id: str) -> None:
        server = next((item for item in self.remote_servers if item.id == server_id), None)
        if not server:
            return
        try:
            containers = list_containers(server)
            error = ""
        except RemoteError as exception:
            containers = []
            error = str(exception)
        self.call_from_thread(self._finish_remote_refresh, server_id, containers, error)

    def _finish_remote_refresh(self, server_id: str, containers: list[RemoteContainer], error: str) -> None:
        if server_id != self.selected_remote_id:
            return
        self.remote_containers = containers
        self._fill_remote_servers(error)
        self._update_action_states()
        self.notify("Remote Docker state refreshed." if not error else f"Remote Docker unavailable: {error}", severity="warning" if error else "information")

    @on(Button.Pressed, "#sync-remote")
    def sync_remote_button(self) -> None:
        server = self._selected_remote_server()
        if server:
            self.sync_remote_events(server.id)

    @work(thread=True, exclusive=True, group="remote", exit_on_error=False)
    def sync_remote_events(self, server_id: str) -> None:
        server = next((item for item in self.remote_servers if item.id == server_id), None)
        if not server:
            return
        try:
            events = read_command_events(server)
            error = ""
        except RemoteError as exception:
            events = []
            error = str(exception)
        self.call_from_thread(self._finish_remote_sync, server, events, error)

    def _finish_remote_sync(self, server: RemoteServer, events: list[dict], error: str) -> None:
        if error:
            self.notify(f"Could not sync remote commands: {error}", severity="error")
            return
        fingerprints = {(event.command, event.cwd, event.created) for event in self.events}
        imported = 0
        for event in events:
            try:
                command = str(event["command"])
                cwd = f"{server.name}:{event['cwd']}"
                created = float(event["created"])
                fingerprint = (command, cwd, created)
                if fingerprint in fingerprints:
                    continue
                self.events.append(CommandEvent(command, cwd, int(event["exit_code"]), int(event["duration_ms"]), created))
                fingerprints.add(fingerprint)
                imported += 1
            except (KeyError, TypeError, ValueError):
                continue
        self.events.sort(key=lambda event: event.created, reverse=True)
        self.events = self.events[:2000]
        self._save_events()
        self._refresh()
        self.notify(f"Synced {imported} new command events from {server.name}.")

    @on(Button.Pressed, "#capture-remote")
    def capture_remote_logs(self) -> None:
        self.fetch_remote_logs(analyze=False)

    @on(Button.Pressed, "#analyze-remote")
    def analyze_remote_logs(self) -> None:
        if not self._is_configured():
            self.notify("Configure an AI provider with Ctrl+S before analyzing remote logs.", severity="warning")
            return
        self.fetch_remote_logs(analyze=True)

    @on(Button.Pressed, "#start-remote")
    def start_remote_container(self) -> None:
        self._request_remote_container_action("start")

    @on(Button.Pressed, "#stop-remote")
    def stop_remote_container(self) -> None:
        self._request_remote_container_action("stop")

    @on(Button.Pressed, "#restart-remote")
    def restart_remote_container(self) -> None:
        self._request_remote_container_action("restart")

    def _request_remote_container_action(self, action: str) -> None:
        server = self._selected_remote_server()
        container = self.selected_remote_container
        if not server or not container:
            self.notify("Select a remote server and container first.", severity="warning")
            return
        self.push_screen(
            ContainerActionScreen(action, container, f"{server.name} ({server.user}@{server.host})"),
            lambda confirmed: self._confirm_remote_container_action(server.id, container, action, confirmed),
        )

    def _confirm_remote_container_action(
        self, server_id: str, container: str, action: str, confirmed: bool
    ) -> None:
        if confirmed:
            self.run_remote_container_action(server_id, container, action)

    @work(thread=True, exclusive=True, group="remote", exit_on_error=False)
    def run_remote_container_action(self, server_id: str, container: str, action: str) -> None:
        server = next((item for item in self.remote_servers if item.id == server_id), None)
        if not server:
            return
        try:
            manage_remote_container(server, container, action)
            error = ""
        except RemoteError as exception:
            error = str(exception)
        self.call_from_thread(self._finish_remote_container_action, server_id, container, action, error)

    def _finish_remote_container_action(self, server_id: str, container: str, action: str, error: str) -> None:
        if error:
            self.notify(f"Could not {action} remote container: {error}", severity="error")
            return
        action_name = {
            "start": "started",
            "stop": "stopped",
            "restart": "restarted",
        }[action]
        self.notify(f"Remote container {container} {action_name}.")
        self.refresh_remote_server(server_id)

    def fetch_remote_logs(self, *, analyze: bool) -> None:
        server = self._selected_remote_server()
        if not server or not self.selected_remote_container:
            self.notify("Select a remote server and container first.", severity="warning")
            return
        self.fetch_remote_container_logs(server.id, self.selected_remote_container, analyze)

    @work(thread=True, exclusive=True, group="remote", exit_on_error=False)
    def fetch_remote_container_logs(self, server_id: str, container: str, analyze: bool) -> None:
        server = next((item for item in self.remote_servers if item.id == server_id), None)
        if not server:
            return
        try:
            logs = read_container_logs(server, container)
            error = ""
        except RemoteError as exception:
            logs = ""
            error = str(exception)
        self.call_from_thread(self._finish_remote_logs, server, container, logs, analyze, error)

    def _finish_remote_logs(self, server: RemoteServer, container: str, logs: str, analyze: bool, error: str) -> None:
        if error:
            self.notify(f"Could not read remote logs: {error}", severity="error")
            return
        incident = Incident(
            id=uuid4().hex,
            title=f"Remote Docker logs: {server.name} / {container}",
            command=f"ssh {server.user}@{server.host} docker logs --tail 300 --timestamps {container}",
            evidence=redact(f"Remote server: {server.name} ({server.host})\nContainer: {container}\n\nRecent Docker logs:\n{logs}"),
            source="remote docker", created=time.time(), status="queued" if analyze else "captured",
        )
        self.incidents.insert(0, incident)
        self.selected_ai_id = incident.id
        self._save_incidents()
        self._refresh()
        if analyze:
            self.analyze_incident(incident.id)
        else:
            self.notify("Remote Docker logs saved to the local archive.")

    @on(Button.Pressed, "#delete-remote")
    def delete_remote_server(self) -> None:
        server = self._selected_remote_server()
        if not server:
            return
        self.remote_servers = [item for item in self.remote_servers if item.id != server.id]
        self.selected_remote_id = self.remote_servers[0].id if self.remote_servers else None
        self.remote_containers = []
        self.selected_remote_container = None
        self._save_remote_servers()
        self._fill_remote_servers()
        self._update_action_states()
        self.notify("Server removed from the local dashboard.")

    @on(Button.Pressed, "#delete-all-remote")
    def delete_all_remote_servers(self) -> None:
        self._request_clear("remote", "saved server connections", "This only removes Elijah's local server list; no remote server is changed.")

    # ---- archives ----------------------------------------------------
    @on(DataTable.RowHighlighted, "#ai-incident-table")
    def select_ai_incident(self, event: DataTable.RowHighlighted) -> None:
        self.selected_ai_id = str(event.row_key.value)
        self._render_ai_detail()
        self._update_action_states()

    @on(Select.Changed, "#ai-filter")
    def filter_ai_archive(self) -> None:
        self._fill_incidents()
        self._render_ai_detail()
        self._update_action_states()

    @on(Select.Changed, "#fix-note-filter")
    def filter_fix_notes(self) -> None:
        self._fill_fix_notes()
        self._render_fix_note_detail()
        self._update_action_states()

    @on(DataTable.RowHighlighted, "#fix-note-table")
    def select_fix_note(self, event: DataTable.RowHighlighted) -> None:
        self.selected_fix_note_id = str(event.row_key.value)
        self._render_fix_note_detail()
        self._update_action_states()

    @on(DataTable.RowHighlighted, "#event-table")
    def select_event(self, event: DataTable.RowHighlighted) -> None:
        try:
            self.selected_event_index = int(str(event.row_key.value).replace("event-", ""))
        except ValueError:
            self.selected_event_index = None
        self._render_event_detail()
        self._update_action_states()

    @on(Button.Pressed, "#ask-command")
    def ask_command(self) -> None:
        if not self._is_configured():
            self.notify("Configure an AI provider with Ctrl+S before asking the command advisor.", severity="warning")
            return
        event = self._selected_event()
        if not event:
            self.notify("Select a command from the timeline first.", severity="warning")
            return
        self.push_screen(CommandQuestion(event.command), lambda question: self._create_command_question(event, question))

    def _create_command_question(self, event: CommandEvent, question: str | None) -> None:
        if not question:
            return
        evidence = f"Recorded command: {event.command}\nWorking directory: {event.cwd}\nExit code: {event.exit_code}\nDuration: {event.duration_ms} ms\n\nUser question: {question}"
        incident = Incident(id=uuid4().hex, title=f"Command inquiry: {self._short(event.command, 68)}", command=event.command, evidence=evidence, source="command advisor", created=time.time(), status="queued")
        self.incidents.insert(0, incident)
        self.selected_ai_id = incident.id
        self._save_incidents()
        self._refresh()
        self.analyze_incident(incident.id)

    @on(Button.Pressed, "#reanalyze-ai")
    def reanalyze_ai_button(self) -> None:
        incident = self._selected_ai()
        if incident:
            self.analyze_incident(incident.id)

    @on(Button.Pressed, "#new-ai")
    def new_ai_investigation(self) -> None:
        self.query_one(TabbedContent).active = "ai-workspace"
        self.query_one("#ai-title", Input).focus()

    @on(Button.Pressed, "#edit-ai")
    def edit_ai_incident(self) -> None:
        incident = self._selected_ai()
        if incident:
            self.push_screen(IncidentEditor(incident), lambda values: self._update_incident(incident.id, values))

    def _update_incident(self, incident_id: str, values: dict | None) -> None:
        incident = self._by_id(incident_id)
        if not incident or not values:
            return
        incident.title = values["title"] or incident.title
        incident.tags = [tag.strip() for tag in values["tags"].split(",") if tag.strip()]
        incident.status = values["status"]
        self._save_incidents()
        self._refresh()
        self.notify("Incident metadata updated.")

    @on(Button.Pressed, "#resolve-ai")
    def resolve_ai_incident(self) -> None:
        incident = self._selected_ai()
        if not incident:
            return
        incident.status = "resolved"
        self._save_incidents()
        self._refresh()
        self.notify("Incident marked resolved.")

    def action_archive(self) -> None:
        incident = self._selected_ai()
        if incident:
            incident.status = "archived" if incident.status != "archived" else "resolved"
            self._save_incidents()
            self._refresh()

    @on(Button.Pressed, "#archive-ai")
    def archive_ai_button(self) -> None:
        incident = self._selected_ai()
        if not incident:
            return
        incident.status = "archived" if incident.status != "archived" else "resolved"
        self._save_incidents()
        self._refresh()
        self.notify("AI investigation archived." if incident.status == "archived" else "AI investigation restored.")

    @on(Button.Pressed, "#delete-ai")
    def delete_ai_incident(self) -> None:
        incident = self._selected_ai()
        if not incident:
            return
        self.incidents = [item for item in self.incidents if item.id != incident.id]
        self.selected_ai_id = self.incidents[0].id if self.incidents else None
        self._save_incidents()
        self._refresh()
        self.notify("Incident deleted.")

    @on(Button.Pressed, "#delete-all-ai")
    def delete_all_ai_incidents(self) -> None:
        self._request_clear("ai", "AI investigations", "Saved investigations and AI responses will be removed from this computer.")

    @on(Button.Pressed, "#edit-fix-note")
    def edit_fix_note(self) -> None:
        item = self._selected_fix_note()
        if item:
            self.push_screen(FixNoteEditor(item), lambda values: self._update_fix_note(item.id, values))

    def _update_fix_note(self, item_id: str, values: dict | None) -> None:
        item = next((record for record in self.fix_notes if record.id == item_id), None)
        if not item or not values:
            return
        for key in ("title", "command", "failure", "resolution", "prevention"):
            setattr(item, key, redact(values[key]))
        self._save_fix_notes()
        self._refresh()
        self.notify("Fix note updated.")

    @on(Button.Pressed, "#archive-fix-note")
    def archive_fix_note(self) -> None:
        item = self._selected_fix_note()
        if not item:
            return
        item.status = "archived" if item.status != "archived" else "active"
        self._save_fix_notes()
        self._refresh()
        self.notify("Fix note archived." if item.status == "archived" else "Fix note restored.")

    @on(Button.Pressed, "#delete-fix-note")
    def delete_fix_note(self) -> None:
        item = self._selected_fix_note()
        if not item:
            return
        self.fix_notes = [record for record in self.fix_notes if record.id != item.id]
        self.selected_fix_note_id = self.fix_notes[0].id if self.fix_notes else None
        self._save_fix_notes()
        self._refresh()
        self.notify("Fix note deleted.")

    @on(Button.Pressed, "#delete-all-fix-notes")
    def delete_all_fix_notes(self) -> None:
        self._request_clear("fix-notes", "fix notes", "Your locally saved problem-and-solution notes will be removed.")

    @on(Button.Pressed, "#delete-all-events")
    def delete_all_events(self) -> None:
        self._request_clear("events", "command timeline entries", "This clears local and imported remote command metadata only; it does not affect shell history or remote servers.")

    def _request_clear(self, collection: str, label: str, detail: str) -> None:
        self.push_screen(ClearDataScreen(label, detail), lambda confirmed: self._clear_collection(collection, confirmed))

    def _clear_collection(self, collection: str, confirmed: bool) -> None:
        if not confirmed:
            return
        if collection == "ai":
            self.incidents = []
            self.selected_ai_id = None
            self._save_incidents()
        elif collection == "events":
            self.events = []
            self.selected_event_index = None
            self._save_events()
        elif collection == "fix-notes":
            self.fix_notes = []
            self.selected_fix_note_id = None
            self._save_fix_notes()
        elif collection == "docker":
            self.docker_targets = []
            self.docker_snapshots = {}
            self.selected_docker_id = None
            self._save_docker_targets()
        elif collection == "remote":
            self.remote_servers = []
            self.remote_containers = []
            self.selected_remote_id = None
            self.selected_remote_container = None
            self._save_remote_servers()
        else:
            return
        self._refresh()
        self.notify("Local data deleted.")

    # ---- AI ----------------------------------------------------------
    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.config), self._save_settings)

    def _save_settings(self, config: dict | None) -> None:
        if not config:
            return
        self.config = config
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
        self._refresh()
        self.notify("AI connection saved locally.")

    @work(thread=True, exclusive=True, group="analysis", exit_on_error=False)
    def analyze_incident(self, incident_id: str) -> None:
        incident = next((item for item in self.incidents if item.id == incident_id), None)
        if not incident:
            return
        if not self._is_configured():
            self.call_from_thread(self.notify, "Set AI credentials first.", severity="warning")
            return
        self.call_from_thread(self._set_analyzing, incident_id)
        try:
            analysis = self._request_analysis(incident)
            self.call_from_thread(self._finish_analysis, incident_id, analysis, None)
        except Exception as error:
            self.call_from_thread(self._finish_analysis, incident_id, None, str(error))

    def _request_analysis(self, incident: Incident) -> str:
        endpoint = self.config["base_url"].rstrip("/") + "/chat/completions"
        prompt = f"""You are a senior production-debugging assistant. Analyze this terminal incident.
Be precise; do not invent commands, files, or facts not present in the evidence.
Return compact Markdown with exactly these sections:
## What happened
## Most likely cause
## Evidence
## Safe next checks
## Fix options
## Prevention

Title: {incident.title}
Source: {incident.source}
Command/context:\n{incident.command or '(none)'}
Evidence:\n{incident.evidence[:24000]}
"""
        body = json.dumps({
            "model": self.config["model"],
            "messages": [
                {"role": "system", "content": "You diagnose software incidents. Never claim you executed a command."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }).encode()
        request = urllib.request.Request(endpoint, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.config['api_key']}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=75) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            message = error.read().decode(errors="replace")[:800]
            raise RuntimeError(f"AI endpoint returned HTTP {error.code}: {message}") from error
        try:
            return payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("The endpoint returned an unexpected Chat Completions response.") from error

    def _set_analyzing(self, incident_id: str) -> None:
        incident = self._by_id(incident_id)
        if incident:
            incident.status = "analyzing"
            self._save_incidents()
            self._refresh()

    def _finish_analysis(self, incident_id: str, analysis: str | None, error: str | None) -> None:
        incident = self._by_id(incident_id)
        if not incident:
            return
        if error:
            incident.status = "analysis failed"
            incident.analysis = f"[b red]Analysis request failed[/]\n{error}"
            self.notify("Analysis failed — see incident detail.", severity="error")
        else:
            incident.status = "investigated"
            incident.analysis = analysis or ""
            incident.model = self.config.get("model", "")
            self.notify("Analysis saved to the incident archive.")
        self._save_incidents()
        self._refresh()

    # ---- CLI inbox ---------------------------------------------------
    def ingest_inbox(self) -> None:
        try:
            files = sorted(INBOX_DIR.glob("*.json"))
        except OSError:
            return
        changed = False
        for path in files:
            try:
                capture = json.loads(path.read_text())
                if capture.get("kind") == "command_event":
                    self.events.insert(0, CommandEvent(**{key: capture[key] for key in CommandEvent.__dataclass_fields__}))
                    self.events = self.events[:2000]
                    path.unlink()
                    changed = True
                    continue
                self.incidents.insert(0, Incident(
                    id=uuid4().hex,
                    title=capture.get("title") or self._infer_title(capture.get("evidence", ""), capture.get("command", "")),
                    command=redact(capture.get("command", "")), evidence=redact(capture.get("evidence", "")),
                    source=capture.get("source", "terminal"), created=time.time(), status="captured",
                ))
                path.unlink()
                changed = True
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        if changed:
            self._save_incidents()
            self._save_events()
            self._refresh()
            self.notify("Captured evidence imported from check-it.")

    # ---- presentation / storage -------------------------------------
    def _refresh(self) -> None:
        self._fill_incidents()
        self._fill_fix_notes()
        self._fill_events()
        self._fill_docker_targets()
        self._fill_remote_servers()
        self._render_ai_detail()
        self._render_fix_note_detail()
        self._update_action_states()
        ai_open = sum(item.status != "archived" for item in self.incidents)
        self.query_one("#open-stat", Static).update(f"AI CASES\n{ai_open} open")
        self.query_one("#fix-note-stat", Static).update(f"FIX NOTES\n{len(self.fix_notes)} saved")
        connection = "CONNECTED" if self._is_configured() else "SETUP REQUIRED"
        self.query_one("#ai-stat", Static).update(f"AI CONNECTION\n{connection}")

    def _animate_signal(self) -> None:
        frames = ("◐", "◓", "◑", "◒")
        self.pulse_frame = (self.pulse_frame + 1) % len(frames)
        queued = sum(item.status == "analyzing" for item in self.incidents)
        label = "ANALYZING" if queued else "WATCHING"
        self.query_one("#signal-stat", Static).update(f"CAPTURE STATUS\n{frames[self.pulse_frame]} {label}")

    def _fill_incidents(self) -> None:
        table = self.query_one("#ai-incident-table", DataTable)
        table.clear()
        filter_value = str(self.query_one("#ai-filter", Select).value)
        items = self._filtered_incidents(filter_value)
        visible_ids = {item.id for item in items}
        if self.selected_ai_id not in visible_ids:
            self.selected_ai_id = items[0].id if items else None
        for item in items:
            table.add_row(self._short(item.title, 42), item.source, item.status, self._age(item.created), key=item.id)

    def _fill_fix_notes(self) -> None:
        table = self.query_one("#fix-note-table", DataTable)
        table.clear()
        filter_value = str(self.query_one("#fix-note-filter", Select).value)
        items = [item for item in self.fix_notes if filter_value == "all" or item.status == filter_value]
        visible_ids = {item.id for item in items}
        if self.selected_fix_note_id not in visible_ids:
            self.selected_fix_note_id = items[0].id if items else None
        for item in items:
            table.add_row(self._short(item.title, 38), self._short(item.command or "manual note", 26), item.status, self._age(item.created), key=item.id)

    def _filtered_incidents(self, filter_value: str) -> list[Incident]:
        if filter_value == "all":
            return self.incidents
        if filter_value == "active":
            return [item for item in self.incidents if item.status not in {"archived", "resolved"}]
        return [item for item in self.incidents if item.status == filter_value]

    def _fill_events(self) -> None:
        table = self.query_one("#event-table", DataTable)
        table.clear()
        for index, item in enumerate(self.events[:500]):
            exit_value = "[green]0[/]" if item.exit_code == 0 else f"[red]{item.exit_code}[/]"
            table.add_row(self._short(item.command, 54), exit_value, f"{item.duration_ms} ms", self._short(item.cwd, 30), self._age(item.created), key=f"event-{index}")
        self._render_event_detail()

    def _fill_docker_targets(self) -> None:
        if not self.query("#docker-table"):
            return
        table = self.query_one("#docker-table", DataTable)
        table.clear()
        for target in self.docker_targets:
            snapshot = self.docker_snapshots.get(target.id)
            state = snapshot.status if snapshot else "not checked"
            health = snapshot.health if snapshot else "—"
            restarts = str(snapshot.restart_count) if snapshot else "—"
            compose = Path(target.compose_file).name if target.compose_file else "direct container"
            table.add_row(self._short(target.name, 25), self._short(compose, 22), state, health, restarts, key=target.id)
        self._render_docker_detail()

    def _render_docker_detail(self) -> None:
        if not self.query("#docker-detail-content"):
            return
        detail = self.query_one("#docker-detail-content", Static)
        target = self._selected_docker_target()
        if not target:
            detail.update("Add or select a container to inspect its Docker state.")
            return
        snapshot = self.docker_snapshots.get(target.id)
        compose = target.compose_file or "Direct container lookup"
        if not snapshot:
            detail.update(
                f"[b #f1f4ff]{target.name}[/]\n[dim]{compose}[/]\n\n"
                "This container is remembered locally but has not been checked yet. "
                "Choose Refresh for state only, Capture logs for a local incident, or Analyze logs to read recent logs and send that selected evidence to AI."
            )
            return
        detail.update(
            f"[b #f1f4ff]{target.name}[/]\n[dim]{compose}[/]\n\n"
            f"[b #64d9ff]STATE[/] {snapshot.status}\n"
            f"[b #64d9ff]HEALTH[/] {snapshot.health}\n"
            f"[b #64d9ff]RESTARTS[/] {snapshot.restart_count}\n"
            f"[b #64d9ff]IMAGE[/] {snapshot.image or 'unknown'}\n\n"
            + (f"[b red]DOCKER ERROR[/]\n{snapshot.error}" if snapshot.error else "")
        )

    def _fill_remote_servers(self, error: str = "") -> None:
        if not self.query("#remote-server-table"):
            return
        server_table = self.query_one("#remote-server-table", DataTable)
        server_table.clear()
        for server in self.remote_servers:
            status = "selected" if server.id == self.selected_remote_id else "ready"
            server_table.add_row(server.name, f"{server.user}@{server.host}:{server.port}", status, key=server.id)
        container_table = self.query_one("#remote-container-table", DataTable)
        container_table.clear()
        for container in self.remote_containers:
            container_table.add_row(container.name, container.state, self._short(container.image, 28), key=container.name)
        self._render_remote_detail(error)
        self._fill_remote_events()

    def _fill_remote_events(self) -> None:
        if not self.query("#remote-event-table"):
            return
        table = self.query_one("#remote-event-table", DataTable)
        table.clear()
        server = self._selected_remote_server()
        if not server:
            return
        prefix = f"{server.name}:"
        for index, item in enumerate(event for event in self.events if event.cwd.startswith(prefix)):
            exit_value = "[green]0[/]" if item.exit_code == 0 else f"[red]{item.exit_code}[/]"
            table.add_row(self._short(item.command, 32), exit_value, self._age(item.created), key=f"remote-event-{index}")

    def _render_remote_detail(self, error: str = "") -> None:
        if not self.query("#remote-detail-content"):
            return
        detail = self.query_one("#remote-detail-content", Static)
        server = self._selected_remote_server()
        if not server:
            detail.update("Add a server to connect over SSH.")
            return
        selected = next((item for item in self.remote_containers if item.name == self.selected_remote_container), None)
        container = f"\n\n[b #7de2d1]SELECTED CONTAINER[/]\n{selected.name}\n{selected.state}\n{selected.image}" if selected else "\n\n[dim]Select a container to capture or analyze its latest logs.[/]"
        problem = f"\n\n[b red]CONNECTION ERROR[/]\n{error}" if error else ""
        detail.update(
            f"[b #f1f4ff]{server.name}[/]\n[dim]{server.user}@{server.host}:{server.port}[/]\n\n"
            f"[b #64d9ff]DOCKER CONTAINERS[/]\n{len(self.remote_containers)} listed"
            f"{container}{problem}\n\n"
            "[dim]Sync this server's commands updates the timeline below. It only imports this selected server's local Elijah command metadata. SSH keys or an SSH agent are required.[/]"
        )

    def _selected_event(self) -> CommandEvent | None:
        if self.selected_event_index is None or self.selected_event_index >= len(self.events):
            return None
        return self.events[self.selected_event_index]

    def _selected_docker_target(self) -> DockerTarget | None:
        if self.selected_docker_id:
            return next((item for item in self.docker_targets if item.id == self.selected_docker_id), None)
        return self.docker_targets[0] if self.docker_targets else None

    def _selected_remote_server(self) -> RemoteServer | None:
        if self.selected_remote_id:
            return next((item for item in self.remote_servers if item.id == self.selected_remote_id), None)
        return self.remote_servers[0] if self.remote_servers else None

    def _update_action_states(self) -> None:
        """Keep contextual actions quiet until their required record is available."""
        ai_selected = self._selected_ai() is not None
        fix_selected = self._selected_fix_note() is not None
        docker_selected = self._selected_docker_target() is not None
        server_selected = self._selected_remote_server() is not None
        container_selected = server_selected and any(
            item.name == self.selected_remote_container for item in self.remote_containers
        )
        self._set_buttons_disabled(
            ("reanalyze-ai", "edit-ai", "resolve-ai", "archive-ai", "delete-ai"), not ai_selected
        )
        self._set_buttons_disabled(("delete-all-ai",), not bool(self.incidents))
        self._set_buttons_disabled(("ask-command",), self._selected_event() is None)
        self._set_buttons_disabled(("delete-all-events",), not bool(self.events))
        self._set_buttons_disabled(
            ("edit-fix-note", "archive-fix-note", "delete-fix-note"), not fix_selected
        )
        self._set_buttons_disabled(("delete-all-fix-notes",), not bool(self.fix_notes))
        self._set_buttons_disabled(
            (
                "refresh-docker", "capture-docker", "analyze-docker",
                "start-docker", "stop-docker", "restart-docker", "delete-docker",
            ),
            not docker_selected,
        )
        self._set_buttons_disabled(("delete-all-docker",), not bool(self.docker_targets))
        self._set_buttons_disabled(("sync-remote", "delete-remote", "refresh-remote"), not server_selected)
        self._set_buttons_disabled(("delete-all-remote",), not bool(self.remote_servers))
        self._set_buttons_disabled(
            (
                "capture-remote", "analyze-remote", "start-remote", "stop-remote",
                "restart-remote",
            ),
            not container_selected,
        )

    def _set_buttons_disabled(self, button_ids: tuple[str, ...], disabled: bool) -> None:
        for button_id in button_ids:
            for button in self.query(f"#{button_id}"):
                button.disabled = disabled

    def _render_event_detail(self) -> None:
        if not self.query("#event-content"):
            return
        detail = self.query_one("#event-content", Static)
        event = self._selected_event()
        if not event:
            detail.update("Select a command to inspect it or ask AI about it.")
            return
        result = "[green]Succeeded[/]" if event.exit_code == 0 else f"[red]Failed (exit {event.exit_code})[/]"
        detail.update(f"[b #f1f4ff]{event.command}[/]\n\n{result}\n[dim]{event.cwd}\n{event.duration_ms} ms · {self._age(event.created)}[/]\n\nAsk AI to explain the command, identify risks, propose a safer form, or adapt it to your goal.\n\n[dim]To include stdout/stderr, run commands inside elijah-shell; a full transcript is archived when that session ends.[/]")

    def _render_ai_detail(self) -> None:
        if not self.query("#ai-detail-content"):
            return
        incident = self._selected_ai()
        detail = self.query_one("#ai-detail-content", Static)
        if not incident:
            detail.update("Select an AI investigation to inspect it.")
            self.query_one("#archive-ai", Button).label = "Archive"
            return
        self.query_one("#archive-ai", Button).label = "Restore" if incident.status == "archived" else "Archive"
        command = f"[b #64d9ff]COMMAND / CONTEXT[/]\n{incident.command}\n\n" if incident.command else ""
        detail.update(f"[b #f1f4ff]{incident.title}[/]\n[dim]{incident.source} · {self._age(incident.created)} · {incident.status}[/]\n\n{command}[b #64d9ff]EVIDENCE[/]\n{self._short(incident.evidence, 1800)}\n\n[b #7de2d1]AI INVESTIGATION[/]\n{incident.analysis or '[dim]No AI response yet.[/]'}")

    def _render_fix_note_detail(self) -> None:
        if not self.query("#fix-note-detail-content"):
            return
        detail = self.query_one("#fix-note-detail-content", Static)
        item = self._selected_fix_note()
        if not item:
            detail.update("Select a fix note to inspect the failure and its proven resolution.")
            self.query_one("#archive-fix-note", Button).label = "Archive"
            return
        self.query_one("#archive-fix-note", Button).label = "Restore" if item.status == "archived" else "Archive"
        command = f"[b #64d9ff]COMMAND / CONTEXT[/]\n{item.command}\n\n" if item.command else ""
        prevention = f"\n\n[b #7de2d1]PREVENTION[/]\n{item.prevention}" if item.prevention else ""
        tags = f" · {', '.join(item.tags)}" if item.tags else ""
        detail.update(f"[b #f1f4ff]{item.title}[/]\n[dim]{self._age(item.created)} · {item.status}{tags}[/]\n\n{command}[b #ffcc80]WHAT FAILED[/]\n{item.failure}\n\n[b #7de2d1]WHAT FIXED IT[/]\n{item.resolution}{prevention}")

    def _selected_ai(self) -> Incident | None:
        return self._by_id(self.selected_ai_id) if self.selected_ai_id else (self.incidents[0] if self.incidents else None)

    def _selected_fix_note(self) -> FixNote | None:
        return next((item for item in self.fix_notes if item.id == self.selected_fix_note_id), None) if self.selected_fix_note_id else (self.fix_notes[0] if self.fix_notes else None)

    def _by_id(self, incident_id: str | None) -> Incident | None:
        return next((item for item in self.incidents if item.id == incident_id), None)

    def _is_configured(self) -> bool:
        return all(self.config.get(key) for key in ("base_url", "model", "api_key"))

    def _load_incidents(self) -> list[Incident]:
        try:
            return [Incident(**item) for item in json.loads(INCIDENTS_FILE.read_text())]
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def _save_incidents(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        INCIDENTS_FILE.write_text(json.dumps([asdict(item) for item in self.incidents], ensure_ascii=False, indent=2))

    @staticmethod
    def _load_fix_notes() -> list[FixNote]:
        try:
            return [FixNote(**item) for item in json.loads(FIX_NOTES_FILE.read_text())]
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def _save_fix_notes(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        FIX_NOTES_FILE.write_text(json.dumps([asdict(item) for item in self.fix_notes], ensure_ascii=False, indent=2))

    def _load_events(self) -> list[CommandEvent]:
        try:
            return [CommandEvent(**item) for item in json.loads(EVENTS_FILE.read_text())]
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def _save_events(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        EVENTS_FILE.write_text(json.dumps([asdict(item) for item in self.events], ensure_ascii=False, indent=2))

    @staticmethod
    def _load_docker_targets() -> list[DockerTarget]:
        try:
            return [DockerTarget(**item) for item in json.loads(DOCKER_TARGETS_FILE.read_text())]
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def _save_docker_targets(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        DOCKER_TARGETS_FILE.write_text(
            json.dumps([asdict(item) for item in self.docker_targets], ensure_ascii=False, indent=2)
        )

    @staticmethod
    def _load_remote_servers() -> list[RemoteServer]:
        try:
            return [RemoteServer(**item) for item in json.loads(REMOTE_SERVERS_FILE.read_text())]
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def _save_remote_servers(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        REMOTE_SERVERS_FILE.write_text(
            json.dumps([asdict(item) for item in self.remote_servers], ensure_ascii=False, indent=2)
        )

    @staticmethod
    def _load_config() -> dict:
        try:
            config = json.loads(CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            config = {}
        # Environment variables let package users keep credentials out of disk.
        for key, variable in (("base_url", "ELIJAH_AI_BASE_URL"), ("model", "ELIJAH_AI_MODEL"), ("api_key", "ELIJAH_AI_API_KEY")):
            if os.environ.get(variable):
                config[key] = os.environ[variable]
        return config

    @staticmethod
    def _infer_title(evidence: str, command: str) -> str:
        for line in evidence.splitlines():
            if any(word in line.lower() for word in ("error", "exception", "failed", "fatal", "traceback")):
                return line.strip()[:100]
        return (command or "Captured terminal incident")[:100]

    @staticmethod
    def _short(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[: limit - 1] + "…"

    @staticmethod
    def _age(timestamp: float) -> str:
        seconds = max(0, int(time.time() - timestamp))
        if seconds < 60: return "now"
        if seconds < 3600: return f"{seconds // 60}m ago"
        if seconds < 86_400: return f"{seconds // 3600}h ago"
        return f"{seconds // 86_400}d ago"


def install_zsh() -> None:
    """Install the zsh template; its source lives in elijah_shells/zsh.zsh."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    integration = APP_DIR / "elijah.zsh"
    template = (Path(__file__).parent / "elijah_shells" / "zsh.zsh").read_text()
    check_it = Path(sys.executable).parent / "check-it"
    integration.write_text(template.replace("__CHECK_IT__", str(check_it)))
    zshrc = Path.home() / ".zshrc"
    start, end = "# >>> elijah-investigator >>>", "# <<< elijah-investigator <<<"
    block = f"\n{start}\nsource {integration}\n{end}\n"
    existing = zshrc.read_text() if zshrc.exists() else ""
    if start not in existing:
        zshrc.write_text(existing.rstrip() + block)
    print(f"Elijah shell integration installed. Restart zsh or run: source {zshrc}")
    print("Default: failures only. For all commands: export ELIJAH_CAPTURE=all")


def uninstall_zsh() -> None:
    """Remove only Elijah's marked block from .zshrc; leave all other config intact."""
    zshrc = Path.home() / ".zshrc"
    start, end = "# >>> elijah-investigator >>>", "# <<< elijah-investigator <<<"
    if not zshrc.exists():
        print("No ~/.zshrc found; nothing changed.")
        return
    text = zshrc.read_text()
    before, marker, rest = text.partition(start)
    if not marker or end not in rest:
        print("Elijah integration block was not found; nothing changed.")
        return
    zshrc.write_text((before + rest.partition(end)[2]).lstrip("\n"))
    print("Elijah shell integration removed. Restart zsh or run: source ~/.zshrc")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "install-shell":
        shell = sys.argv[sys.argv.index("--shell") + 1] if "--shell" in sys.argv and len(sys.argv) > sys.argv.index("--shell") + 1 else detect_shell()
        if shell == "zsh": install_zsh()
        else: install_other_shell(shell, str(Path(sys.executable).parent / "check-it"))
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "uninstall-shell":
        shell = sys.argv[sys.argv.index("--shell") + 1] if "--shell" in sys.argv and len(sys.argv) > sys.argv.index("--shell") + 1 else detect_shell()
        if shell == "zsh": uninstall_zsh()
        else: uninstall_other_shell(shell)
        return
    Elijah().run()


if __name__ == "__main__":
    main()

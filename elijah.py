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
from elijah_models import CommandEvent, FixNote, Incident

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, TabbedContent, TabPane, TextArea


APP_DIR = Path.home() / ".elijah-investigator"
INCIDENTS_FILE = APP_DIR / "incidents.json"
FIX_NOTES_FILE = APP_DIR / "fix-notes.json"
CONFIG_FILE = APP_DIR / "config.json"
INBOX_DIR = APP_DIR / "inbox"
EVENTS_FILE = APP_DIR / "command-events.json"

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
    #settings-actions { height: 3; align: right middle; }
    #settings-actions Button { margin-left: 1; }
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
            with Horizontal(id="settings-actions"):
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
    #question-actions { height: 3; align: right middle; }
    #question-actions Button { margin-left: 1; }
    """

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

    def compose(self) -> ComposeResult:
        with Container(id="question-box"):
            yield Label("ASK COMMAND ADVISOR")
            yield Static(f"[dim]{self.command[:180]}[/]")
            yield Input(placeholder="e.g. Explain this, make it safer, or adapt it for production…", id="command-question")
            with Horizontal(id="question-actions"):
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


class IncidentEditor(ModalScreen[dict | None]):
    """A small metadata editor for keeping the archive useful over time."""
    CSS = """
    IncidentEditor { align: center middle; background: rgba(3, 7, 15, 0.82); }
    #incident-editor { width: 74; height: auto; border: heavy #a996ff; background: #10192e; padding: 1 2; }
    #incident-editor Input, #incident-editor Select { margin: 1 0; }
    #incident-editor-actions { height: 3; align: right middle; }
    #incident-editor-actions Button { margin-left: 1; }
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
            with Horizontal(id="incident-editor-actions"):
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
    #fix-note-editor-actions { height: 3; align: right middle; }
    #fix-note-editor-actions Button { margin-left: 1; }
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
            with Horizontal(id="fix-note-editor-actions"):
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
                        with Horizontal(id="capture-actions"):
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
                        with Horizontal(id="ai-primary-actions"):
                            yield Button("New", id="new-ai", variant="success")
                            yield Button("Run again", id="reanalyze-ai")
                            yield Button("Edit", id="edit-ai")
                            yield Button("Resolve", id="resolve-ai", variant="success")
                        with Horizontal(id="ai-secondary-actions"):
                            yield Button("Archive", id="archive-ai")
                            yield Button("Delete", id="delete-ai", variant="error")
            with TabPane("AI command advisor", id="timeline"):
                yield Static("Command metadata is captured locally. Ask AI only when you explicitly submit a question.", id="timeline-note")
                with Horizontal(id="timeline-layout"):
                    yield DataTable(id="event-table", cursor_type="row")
                    with Vertical(id="event-detail"):
                        yield Label("COMMAND ADVISOR", classes="section-label")
                        yield Static("Select a command to inspect it or ask AI about it.", id="event-content")
                        yield Button("Ask AI about this command", id="ask-command", variant="success")
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
                        with Horizontal(id="capture-actions"):
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
                        with Horizontal(id="detail-actions"):
                            yield Button("Edit", id="edit-fix-note")
                            yield Button("Archive", id="archive-fix-note")
                            yield Button("Delete", id="delete-fix-note", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#ai-incident-table", DataTable).add_columns("Investigation", "Source", "State", "Captured")
        self.query_one("#fix-note-table", DataTable).add_columns("Lesson", "Context", "State", "Saved")
        self.query_one("#event-table", DataTable).add_columns("Command", "Exit", "Duration", "Directory", "When")
        self.incidents = self._load_incidents()
        self.fix_notes = self._load_fix_notes()
        self.events = self._load_events()
        self.config = self._load_config()
        self.set_interval(2, self.ingest_inbox)
        self.set_interval(0.35, self._animate_signal)
        self.ingest_inbox()
        self._refresh()

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

    # ---- archives ----------------------------------------------------
    @on(DataTable.RowHighlighted, "#ai-incident-table")
    def select_ai_incident(self, event: DataTable.RowHighlighted) -> None:
        self.selected_ai_id = str(event.row_key.value)
        self._render_ai_detail()

    @on(Select.Changed, "#ai-filter")
    def filter_ai_archive(self) -> None:
        self._fill_incidents()

    @on(Select.Changed, "#fix-note-filter")
    def filter_fix_notes(self) -> None:
        self._fill_fix_notes()

    @on(DataTable.RowHighlighted, "#fix-note-table")
    def select_fix_note(self, event: DataTable.RowHighlighted) -> None:
        self.selected_fix_note_id = str(event.row_key.value)
        self._render_fix_note_detail()

    @on(DataTable.RowHighlighted, "#event-table")
    def select_event(self, event: DataTable.RowHighlighted) -> None:
        try:
            self.selected_event_index = int(str(event.row_key.value).replace("event-", ""))
        except ValueError:
            self.selected_event_index = None
        self._render_event_detail()

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
        self._render_ai_detail()
        self._render_fix_note_detail()
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
        for item in self._filtered_incidents(filter_value):
            table.add_row(self._short(item.title, 42), item.source, item.status, self._age(item.created), key=item.id)

    def _fill_fix_notes(self) -> None:
        table = self.query_one("#fix-note-table", DataTable)
        table.clear()
        filter_value = str(self.query_one("#fix-note-filter", Select).value)
        for item in self.fix_notes:
            if filter_value != "all" and item.status != filter_value:
                continue
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

    def _selected_event(self) -> CommandEvent | None:
        if self.selected_event_index is None or self.selected_event_index >= len(self.events):
            return None
        return self.events[self.selected_event_index]

    def _render_event_detail(self) -> None:
        detail = self.query_one("#event-content", Static)
        event = self._selected_event()
        if not event:
            detail.update("Select a command to inspect it or ask AI about it.")
            return
        result = "[green]Succeeded[/]" if event.exit_code == 0 else f"[red]Failed (exit {event.exit_code})[/]"
        detail.update(f"[b #f1f4ff]{event.command}[/]\n\n{result}\n[dim]{event.cwd}\n{event.duration_ms} ms · {self._age(event.created)}[/]\n\nAsk AI to explain the command, identify risks, propose a safer form, or adapt it to your goal.\n\n[dim]To include stdout/stderr, run commands inside elijah-shell; a full transcript is archived when that session ends.[/]")

    def _render_ai_detail(self) -> None:
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

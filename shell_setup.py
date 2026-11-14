from __future__ import annotations

import os
from pathlib import Path


APP_DIR = Path.home() / ".elijah-investigator"
TEMPLATE_DIR = Path(__file__).parent / "elijah_shells"
MARKERS = ("# >>> elijah-investigator >>>", "# <<< elijah-investigator <<<")
SHELL_FILES = {
    "bash": (Path.home() / ".bashrc", "bash.sh"),
    "fish": (Path.home() / ".config" / "fish" / "config.fish", "fish.fish"),
}


# Handle the detect shell workflow for this component.
def detect_shell() -> str:
    shell = Path(os.environ.get("SHELL", "")).name.lower()
    if shell in {"zsh", "bash", "fish"}:
        return shell
    return "bash"


# Handle the install workflow for this component.
def install(shell: str, check_it: str) -> None:
    """Install a non-zsh hook; zsh has its dedicated installer in elijah.py."""
    if shell not in SHELL_FILES:
        raise ValueError(f"Unsupported shell: {shell}")
    rc_file, template_name = SHELL_FILES[shell]
    destination = APP_DIR / f"elijah.{template_name}"
    template = (TEMPLATE_DIR / template_name).read_text()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(template.replace("__CHECK_IT__", check_it))
    _add_source_block(rc_file, destination)
    print(f"Elijah {shell} integration installed in {rc_file}. Restart that shell to activate it.")


# Handle the uninstall workflow for this component.
def uninstall(shell: str) -> None:
    if shell not in SHELL_FILES:
        raise ValueError(f"Unsupported shell: {shell}")
    rc_file, _ = SHELL_FILES[shell]
    if not rc_file.exists():
        print(f"No {rc_file} found; nothing changed.")
        return
    content = rc_file.read_text()
    before, marker, remainder = content.partition(MARKERS[0])
    if not marker or MARKERS[1] not in remainder:
        print("Elijah integration block was not found; nothing changed.")
        return
    rc_file.write_text((before + remainder.partition(MARKERS[1])[2]).lstrip("\n"))
    print(f"Elijah {shell} integration removed. Restart that shell.")


# Perform the internal add source block step for this workflow.
def _add_source_block(rc_file: Path, integration: Path) -> None:
    rc_file.parent.mkdir(parents=True, exist_ok=True)
    existing = rc_file.read_text() if rc_file.exists() else ""
    if MARKERS[0] in existing:
        return
    rc_file.write_text(existing.rstrip() + f"\n{MARKERS[0]}\nsource \"{integration}\"\n{MARKERS[1]}\n")

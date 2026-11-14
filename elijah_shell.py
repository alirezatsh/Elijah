"""Run an explicit, full-terminal forensic recording session for Elijah Investigator."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from check_it import queue_capture


# Run the command-line entrypoint and dispatch the requested workflow.
def main() -> None:
    shell = os.environ.get("SHELL", "/bin/zsh")
    sessions = Path.home() / ".elijah-investigator" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    transcript = sessions / f"session-{time.strftime('%Y%m%d-%H%M%S')}.log"
    if not shutil_which("script"):
        print("elijah-shell needs the 'script' command (usually util-linux).", file=sys.stderr)
        raise SystemExit(1)
    environment = os.environ | {"ELIJAH_FORENSIC": "1", "ELIJAH_SESSION_FILE": str(transcript)}
    print(f"Elijah forensic session: {transcript}\nExit this shell to archive the full transcript.")
    code = subprocess.call(["script", "-q", "-f", "-c", f"{shell} -i", str(transcript)], env=environment)
    if transcript.exists() and transcript.stat().st_size:
        queue_capture("Forensic terminal session", "elijah-shell", transcript.read_text(errors="replace"), "forensic session")
    raise SystemExit(code)


# Handle the shutil which workflow for this component.
def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


if __name__ == "__main__":
    main()

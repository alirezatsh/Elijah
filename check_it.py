"""The small, explicit CLI bridge for Elijah Investigator."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

INBOX_DIR = Path.home() / ".elijah-investigator" / "inbox"


# Handle the queue capture workflow for this component.
def queue_capture(title: str, command: str, evidence: str, source: str) -> None:
    """Write a capture atomically enough for the TUI's polling inbox."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    target = INBOX_DIR / f"{int(time.time())}-{uuid4().hex[:8]}.json"
    target.write_text(json.dumps({"title": title, "command": command, "evidence": evidence, "source": source}, ensure_ascii=False))
    print(f"Queued for Elijah Investigator: {target.name}")


# Handle the queue event workflow for this component.
def queue_event(command: str, cwd: str, exit_code: int, duration_ms: int) -> None:
    """Queue a lightweight shell event; no terminal output is read here."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    target = INBOX_DIR / f"{int(time.time())}-{uuid4().hex[:8]}.json"
    target.write_text(json.dumps({
        "kind": "command_event", "command": command, "cwd": cwd,
        "exit_code": exit_code, "duration_ms": duration_ms, "created": time.time(),
    }, ensure_ascii=False))


# Run the command-line entrypoint and dispatch the requested workflow.
def main() -> None:
    # `check-it run -- curl -i …` is intentionally argv-only: no hidden shell.
    if len(sys.argv) > 2 and sys.argv[1] == "run":
        command = sys.argv[2:]
        try:
            completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as error:
            print(f"Could not run command: {error}", file=sys.stderr)
            raise SystemExit(127) from error
        output = completed.stdout or "(command produced no output)"
        queue_capture("", " ".join(command), output, "terminal")
        print(output, end="" if output.endswith("\n") else "\n")
        raise SystemExit(completed.returncode)

    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        parser = argparse.ArgumentParser(description="Internal Elijah shell hook")
        parser.add_argument("--command", required=True)
        parser.add_argument("--cwd", required=True)
        parser.add_argument("--exit-code", type=int, required=True)
        parser.add_argument("--duration-ms", type=int, default=0)
        parser.add_argument("--capture", choices=("all", "fail"), default="fail")
        args = parser.parse_args(sys.argv[2:])
        if args.capture == "all" or args.exit_code != 0:
            queue_event(args.command, args.cwd, args.exit_code, args.duration_ms)
        return

    parser = argparse.ArgumentParser(description="Queue terminal evidence for Elijah Investigator")
    parser.add_argument("--command", default="", help="Command that produced this output")
    parser.add_argument("--title", default="", help="Optional incident title")
    parser.add_argument("--stdin", action="store_true", help="Read evidence from stdin")
    parser.add_argument("--file", type=Path, help="Read evidence from a file")
    parser.add_argument("--journal-errors", action="store_true", help="Capture recent error-priority journal entries")
    parser.add_argument("--docker", metavar="CONTAINER", help="Capture the last 200 lines from a container")
    args = parser.parse_args()

    sources = sum(bool(item) for item in (args.stdin, args.file, args.journal_errors, args.docker))
    if sources != 1:
        parser.error("Choose exactly one evidence source: --stdin, --file, --journal-errors, or --docker.")
    try:
        if args.stdin:
            evidence, source = sys.stdin.read(), "terminal"
        elif args.file:
            evidence, source = args.file.read_text(errors="replace"), "application log"
        elif args.journal_errors:
            evidence = subprocess.check_output(["journalctl", "-p", "err", "-n", "200", "--no-pager"], text=True, stderr=subprocess.STDOUT)
            source = "system log"
        else:
            evidence = subprocess.check_output(["docker", "logs", "--tail", "200", args.docker], text=True, stderr=subprocess.STDOUT)
            source = "docker"
    except (OSError, subprocess.CalledProcessError) as error:
        parser.error(f"Could not capture evidence: {error}")
    if not evidence.strip():
        parser.error("No evidence was captured.")
    queue_capture(args.title, args.command, evidence, source)


if __name__ == "__main__":
    main()

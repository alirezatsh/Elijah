# Elijah Investigator

Elijah is a terminal incident workspace for deliberate requests to any OpenAI-compatible Chat Completions API, plus private **Fix Notes** where you record what actually failed and how you fixed it. Every AI workflow opens a prompt screen first, so nothing is sent until you write your own request and choose **Ask AI**.

## Features

- Command timeline: command, directory, exit code, and duration
- AI investigation archive with edit, resolve, archive, delete, and re-analysis actions
- Archive filters for active, resolved, and archived AI cases and Fix Notes
- AI Command Advisor for explaining or adapting recorded commands
- Explicit capture helpers for pipes, files, Docker logs, and system journal errors
- Full terminal-session transcripts through `elijah-shell`
- Persistent shell integration for zsh, bash, and fish
- Docker watchlist: remember containers or Compose services, inspect state, and analyze bounded recent logs on demand
- Compatible with OpenAI and other `/chat/completions` providers
- Local Fix Notes library for proven fixes, prevention notes, and credential redaction

## Requirements

- Python 3.10 or newer
- Linux or macOS
- A terminal supported by Textual
- `pipx` is recommended for a global installation
- Git is required only when installing from source

`elijah-shell` additionally needs the `script` utility, normally available through `util-linux` on Linux and many macOS installations.

## Install globally

`requirements.txt` is included for a quick local dependency install, but `pyproject.toml` remains intentionally. It gives `pip` and `pipx` the package name and defines the global commands (`elijah`, `check-it`, and `elijah-shell`). A requirements file can list Textual, but by itself it cannot create those commands. For the simplest global setup, use `pipx install .` below.

### From GitHub source

```bash
git clone https://github.com/YOUR-ACCOUNT/elijah-investigator.git
cd elijah-investigator
pipx install .
```

After installation, these commands are available from any directory:

```bash
elijah
check-it --help
elijah-shell
```

If the commands are not found, run `pipx ensurepath`, open a new terminal, and try again.

### Installing pipx

On Linux/macOS, use your system package manager or:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

Restart the terminal after running `ensurepath`.

## Run without pipx

Use a virtual environment when developing or when you do not want a global executable.

### Linux / macOS

```bash
git clone https://github.com/YOUR-ACCOUNT/elijah-investigator.git
cd elijah-investigator
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
elijah
```

### Quick local dependency install

If you only want to run the interface from the project folder, without installing global commands:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python elijah.py
```

## AI investigations and fix notes

**AI Investigator**, **AI archive**, and **AI command advisor** require a configured provider. `check-it`, shell capture, Docker, and journal imports create AI-ready captured records; an AI request happens only when you explicitly submit one.

**Fix Notes** is a separate on-device knowledge base. Record the command or context, what failed, the fix that actually worked, and the prevention or follow-up action. It never calls an AI provider.

## Connect an AI provider

Start Elijah, press `Ctrl+S`, and provide:

- Base URL, for example `https://api.openai.com/v1`
- Model name
- API key

The settings screen stores these values locally at `~/.elijah-investigator/config.json`, with user-only permissions where the OS supports them.

For servers, shared machines, and automation, use environment variables instead of saving an API key to disk:

```bash
export ELIJAH_AI_BASE_URL="https://api.openai.com/v1"
export ELIJAH_AI_MODEL="your-model-name"
export ELIJAH_AI_API_KEY="your-api-key"
```

Environment variables override saved settings for that process.

## Enable persistent command capture

Elijah's shell integration is opt-in. It records command metadata only; it does not automatically send logs or terminal content to an AI provider.

```bash
elijah install-shell
```

The installer detects your active shell. To choose one directly:

```bash
elijah install-shell --shell zsh
elijah install-shell --shell bash
elijah install-shell --shell fish
```

Restart that shell after installation. By default only failed commands are recorded. To record all commands:

```bash
export ELIJAH_CAPTURE=all
```

To pause capture without uninstalling:

```bash
export ELIJAH_CAPTURE=off
```

To remove the integration:

```bash
elijah uninstall-shell --shell zsh
```

Replace `zsh` with the shell you installed. Only Elijah's marked block is removed from the shell configuration file.

## Capture output and logs

A shell hook can observe exit codes, but it cannot transparently read every program's stdout and stderr. Use one of these explicit capture methods when output matters.

```bash
# Run a command, display its output, and queue that exact output
check-it run -- curl -i https://example.invalid

# Capture piped output
curl -i https://example.invalid 2>&1 | check-it --command "curl -i https://example.invalid" --stdin

# Capture a log file
check-it --file ./application.log --title "checkout failure"

# Capture the last 200 lines from a Docker container
check-it --docker my-service

# Capture the recent error-priority system journal entries (Linux)
check-it --journal-errors
```

Open Elijah to import queued captures into **AI archive**.

For a complete terminal transcript, start a deliberate forensic session:

```bash
elijah-shell
```

Run the commands you are investigating inside that shell. When it exits, Elijah queues the transcript as a forensic-session incident.

## Remember Docker containers

Open **Container watchlist** in Elijah and enter either a container name or a Compose service name. A Compose file path is optional; when supplied, Elijah resolves the service through `docker compose -f <file> ps --all --quiet <service>`, so stopped Compose containers can be started as well.

The watchlist is stored locally. **Refresh** reads Docker state, health-check status, restart count, and image metadata without reading logs. **Capture logs** saves the latest 300 timestamped log lines as a local captured incident. **Analyze logs** first asks what you want AI to investigate, then reads the same bounded log tail and sends that selected evidence together with your request.

The same panel can **Start**, **Stop**, or **Restart** the selected local container. Each lifecycle action opens a confirmation dialog before Elijah sends the Docker command; it does not remove containers, images, volumes, or networks.

Docker log output can contain secrets. Review your container logging policy and use Elijah's configured provider only when you intend to share the selected evidence.

## Central dashboard for remote servers

The **Servers** tab connects to Linux servers through the local OpenSSH client. Add a label, host or IP address, SSH user, port, and optionally a local private-key path. Elijah verifies the connection before saving a server, then lists that server's Docker containers. Select a remote container to capture or analyze its latest 300 log lines in the same local archive. The selected-container controls can Start, Stop, or Restart it over SSH, with a confirmation dialog before each action.

For security, Elijah does not store SSH passwords. Use an SSH key, an entry in `~/.ssh/config`, or a running SSH agent. The remote account needs permission to run `docker ps`, `docker logs`, `docker start`, `docker stop`, and `docker restart`.

## Clear local data

Each archive or saved-list view has a contextual **Clear** button with a confirmation dialog: AI archive, Command Advisor timeline, Fix Notes library, Container watchlist, and saved server connections. These actions only remove Elijah's local records. They never delete Docker containers, images, volumes, remote server files, shell history, or AI-provider credentials.

## Use the Command Advisor

Open **AI command advisor**, select a command, and choose **Ask AI about this command**. Useful prompts include:

- “Explain this command and each flag.”
- “Why may this have failed?”
- “Make this safer for production.”
- “Adapt it to target only the staging container.”

The question and answer are saved as a new incident. To include raw output in the AI context, capture it with `check-it` or use `elijah-shell`.

## Upgrade and uninstall

For a pipx installation from a cloned repository:

```bash
cd elijah-investigator
git pull
pipx reinstall elijah-investigator
```

To remove the application:

```bash
elijah uninstall-shell --shell zsh
pipx uninstall elijah-investigator
```

Runtime data is intentionally preserved at `~/.elijah-investigator/`. Delete that directory manually only if you also want to remove incidents, timeline data, settings, and transcripts.

## Security and local data

Elijah stores the following under `~/.elijah-investigator/`:

- `incidents.json` — AI investigations and responses
- `fix-notes.json` — private records of failures, proven fixes, and prevention notes
- `command-events.json` — command timeline metadata
- `docker-targets.json` — locally remembered container names and optional Compose file paths
- `remote-servers.json` — named SSH server connections and optional local key-file paths
- `config.json` — saved AI endpoint, model, and API key
- `inbox/` — pending captures
- `sessions/` — forensic transcripts

Elijah never sends command history, logs, or transcripts automatically. AI calls happen only after you explicitly choose **Analyze** or **Ask AI**, write a custom request, and confirm it in the prompt screen. The zsh integration skips commands containing obvious credential markers such as `Bearer`, `api_key`, `password`, and `secret`, unless you explicitly set `ELIJAH_CAPTURE_SECRETS=1`.

Do not commit API keys or runtime data, and rotate any token that is accidentally pasted into a terminal, transcript, chat, or incident.

# Gateway Guardian

Gateway Guardian is a standalone supervisor for local gateway profiles. It supports Hermes and OpenClaw profiles from one CLI, one TOML config file, and one background service.

Run the CLI through uv with `uv run python scripts/gateway_guardian.py`. The service reads `~/.config/gateway-guardian/config.toml` and monitors every enabled profile. Each profile keeps isolated state, logs, repair attempts, rollback metadata, cooldowns, and optional LLM repair output.

## Features

- **Multi-profile supervision** — one background service monitors all enabled Hermes and OpenClaw profiles
- **TOML configuration** — one config file at `~/.config/gateway-guardian/config.toml`
- **Health checks** — healthy profiles are checked every 5 minutes / 300 seconds by default
- **Auto-repair** — runs the target's repair command, such as `doctor --fix`, before rollback
- **Git rollback** — rolls a failed profile workspace back to its recorded last-known-good commit
- **Daily snapshots** — creates one automatic git snapshot per healthy profile per day
- **Optional alerts** — supports notification settings in config
- **Optional local LLM repair** — Codex or Claude can attempt last-resort repair when explicitly enabled

## Python and uv

This repository is a uv-managed Python project. Gateway Guardian requires Python 3.11 or newer and has no runtime dependencies outside the standard library.

```bash
uv sync
uv run python scripts/gateway_guardian.py --help
uv run python -m unittest tests.gateway_guardian_tests
```

Use `uv run python scripts/gateway_guardian.py ...` for local development and setup so the service is installed from the same Python environment.

## How It Works

```text
uv run python scripts/gateway_guardian.py run
        |
        v
read ~/.config/gateway-guardian/config.toml
        |
        v
start one isolated worker per enabled profile
        |
        v
health check every 300s by default
        |
        v
doctor repair -> rollback -> optional local LLM repair -> profile cooldown
```

The default healthy check interval is `300` seconds. Set `default_check_interval_seconds` globally, or `check_interval_seconds` on a profile to override it for that profile.

## Quick Start

Want an agent to do it for you? Paste a link to `references/setup.md` into your agent and ask them to set up Gateway Guardian for your Hermes or OpenClaw profiles.

Initialize git in the profile workspace before enabling rollback:

```bash
cd ~/.hermes/profiles/prod/workspace
git init
git config user.email "guardian@example.com"
git config user.name "Guardian"
git add -A && git commit -m "initial"
```

Configure a Hermes profile and start the single background service:

```bash
uv run python scripts/gateway_guardian.py setup \
  --target hermes \
  --profile prod \
  --workspace ~/.hermes/profiles/prod/workspace \
  --cli hermes \
  --profile-arg --profile \
  --profile-arg prod \
  --start

uv run python scripts/gateway_guardian.py status
```

## CLI Reference

```bash
uv run python scripts/gateway_guardian.py setup [--target hermes|openclaw] [--profile NAME] [--workspace PATH] [--cli PATH] [--profile-arg ARG]... [--start]
uv run python scripts/gateway_guardian.py run [--config PATH]
uv run python scripts/gateway_guardian.py start [--config PATH]
uv run python scripts/gateway_guardian.py stop [--config PATH]
uv run python scripts/gateway_guardian.py restart [--config PATH]
uv run python scripts/gateway_guardian.py reload [--config PATH]
uv run python scripts/gateway_guardian.py status [--config PATH]

uv run python scripts/gateway_guardian.py config show [--config PATH]
uv run python scripts/gateway_guardian.py config set [--config PATH] KEY=VALUE [...]

uv run python scripts/gateway_guardian.py profile add [--config PATH] --target hermes|openclaw --profile NAME --workspace PATH --cli PATH [--profile-arg ARG]...
uv run python scripts/gateway_guardian.py profile list [--config PATH]
uv run python scripts/gateway_guardian.py profile set [--config PATH] PROFILE_ID KEY=VALUE [...]
uv run python scripts/gateway_guardian.py profile remove [--config PATH] PROFILE_ID
```

`start` installs or starts one host service for the supervisor. `reload` asks that running supervisor to re-read the TOML config and add, update, disable, or remove profile workers without creating separate services.

## TOML Configuration

Default path:

```text
~/.config/gateway-guardian/config.toml
```

Example:

```toml
version = 1
default_check_interval_seconds = 300
max_repair_attempts = 3
cooldown_seconds = 300
state_dir = "~/.local/state/gateway-guardian"
log_dir = "~/.local/state/gateway-guardian/logs"

[notifications.discord]
webhook_url = ""

[llm]
enabled = false
provider = "codex"
timeout_seconds = 900

[llm.codex]
command = "codex"
model = ""
prompt = """
You are repairing a local {target} gateway profile named {profile}.
Work only in {workspace}. Diagnose the failed service, make the smallest safe fix,
and leave git history intact.
"""

[llm.claude]
command = "claude"
model = ""
prompt = """
You are repairing a local {target} gateway profile named {profile}.
Work only in {workspace}. Diagnose the failed service, make the smallest safe fix,
and leave git history intact.
"""

[[profiles]]
id = "hermes-prod"
enabled = true
target = "hermes"
profile = "prod"
workspace = "~/.hermes/profiles/prod/workspace"
command = "hermes"
args = ["--profile", "prod"]
check_interval_seconds = 300
rollback_enabled = true
llm_enabled = false

[[profiles]]
id = "openclaw-staging"
enabled = true
target = "openclaw"
profile = "staging"
workspace = "~/.openclaw/profiles/staging/workspace"
command = "openclaw"
args = ["--profile", "staging"]
check_interval_seconds = 300
rollback_enabled = true
llm_enabled = true
```

## Git Rollback

Rollback requires each profile workspace to be a git repository. Use repository-level git config in each workspace:

```bash
git -C ~/.hermes/profiles/prod/workspace config user.email "guardian@example.com"
git -C ~/.hermes/profiles/prod/workspace config user.name "Guardian"
```

Gateway Guardian records the current commit as last-known-good only while a profile is healthy, then rolls back to that exact commit if repair fails. Without git, Guardian can still monitor and run target repair, but rollback is skipped.

## Local LLM Repair

LLM repair is disabled by default and requires both global and per-profile opt-in:

```bash
uv run python scripts/gateway_guardian.py config set llm.enabled=true llm.provider=codex
uv run python scripts/gateway_guardian.py profile set hermes-prod llm_enabled=true
```

Supported providers:

- **Codex** — runs locally from the profile workspace with bypass/yolo approval mode
- **Claude** — runs locally from the profile workspace with bypass/yolo approval mode

Customize prompts in `[llm.codex].prompt` and `[llm.claude].prompt`. Gateway Guardian commits a pre-LLM checkpoint before invoking the provider, captures provider output in the profile state directory, rejects repairs that rewrite git history, and commits only repairs that restore health.

## Logs and Status

Use the CLI for service and profile state:

```bash
uv run python scripts/gateway_guardian.py status
uv run python scripts/gateway_guardian.py config show
uv run python scripts/gateway_guardian.py profile list
```

Supervisor and profile logs live under the configured `log_dir`, with per-profile state under `state_dir`.

## License

MIT

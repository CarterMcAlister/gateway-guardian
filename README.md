# Gateway Guardian

Gateway Guardian is a standalone supervisor for local gateway profiles. It supports Hermes and OpenClaw profiles from one CLI, one TOML config file, and one background service.

Install the CLI with `uv tool install .` or run it from a checkout with `uv run gateway-guardian`. The service reads `~/.config/gateway-guardian/config.toml` and monitors every enabled profile. Each profile keeps isolated state, logs, repair attempts, rollback metadata, cooldowns, and optional LLM repair output.

## Features

- **Multi-profile supervision** — one background service monitors all enabled Hermes and OpenClaw profiles
- **TOML configuration** — one config file at `~/.config/gateway-guardian/config.toml`
- **Health checks** — healthy profiles are checked every 5 minutes / 300 seconds by default; Hermes uses `gateway status --deep`
- **Auto-repair** — runs the target's repair command, such as `doctor --fix`, before rollback
- **Git rollback** — rolls a failed profile workspace back to its recorded last-known-good commit
- **Daily snapshots** — creates one automatic git snapshot per healthy profile per day
- **Optional Discord alerts** — sends incident, recovery, and repair-failure webhooks when configured
- **Optional local LLM repair** — Codex or Claude can attempt last-resort repair when explicitly enabled

## Quick Start

Want an agent to do it for you? Send this to your agent:
```
Read https://raw.githubusercontent.com/CarterMcAlister/gateway-guardian/refs/heads/main/references/setup.md and set it up for me
```

### Manual Setup

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
gateway-guardian setup \
  --target hermes \
  --profile prod \
  --workspace ~/.hermes/profiles/prod/workspace \
  --cli hermes \
  --profile-arg --profile \
  --profile-arg prod \
  --start

gateway-guardian status
```

## How It Works

```text
gateway-guardian run
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
doctor repair -> gateway start -> rollback -> optional local LLM repair -> profile cooldown
```

The default healthy check interval is `300` seconds. Set `default_check_interval_seconds` globally, or `check_interval_seconds` on a profile to override it for that profile. Hermes profiles are checked with `hermes [profile args] gateway status --deep`; stale service definitions, stopped/unloaded gateway services, and `agent-secrets exited 1` are treated as unhealthy even when Hermes exits zero.


## Python and uv

This repository is a uv-managed Python project. Gateway Guardian requires Python 3.11 or newer and has no runtime dependencies outside the standard library.

```bash
uv sync
uv run gateway-guardian --help
uv run python -m unittest tests.gateway_guardian_tests
```

Use `uv run gateway-guardian ...` for local development. For host installs, run `uv tool install .` from this checkout, then use `gateway-guardian ...`.

## CLI Reference

```bash
gateway-guardian setup [--target hermes|openclaw] [--profile NAME] [--workspace PATH] [--cli PATH] [--profile-arg ARG]... [--start]
gateway-guardian run [--config PATH]
gateway-guardian start [--config PATH]
gateway-guardian stop [--config PATH]
gateway-guardian restart [--config PATH]
gateway-guardian reload [--config PATH]
gateway-guardian status [--config PATH]

gateway-guardian config show [--config PATH]
gateway-guardian config set [--config PATH] KEY=VALUE [...]

gateway-guardian profile add [--config PATH] --target hermes|openclaw --profile NAME --workspace PATH --cli PATH [--profile-arg ARG]...
gateway-guardian profile list [--config PATH]
gateway-guardian profile set [--config PATH] PROFILE_ID KEY=VALUE [...]
gateway-guardian profile remove [--config PATH] PROFILE_ID
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

## Discord Alerts

Set a Discord webhook URL through the CLI:

```bash
gateway-guardian config set 'notifications.discord.webhook_url=https://discord.com/api/webhooks/...'
gateway-guardian reload
```

When `notifications.discord.webhook_url` is not empty, Gateway Guardian sends a webhook when:

- a previously healthy or unknown profile becomes unhealthy and repair starts;
- a previously unhealthy or failed profile recovers;
- doctor repair, rollback, and configured LLM repair all fail to restore health.

Repeated checks of an already-failed profile do not send duplicate failure alerts. Healthy startup checks do not send alerts.

## Local LLM Repair

LLM repair is disabled by default and requires both global and per-profile opt-in:

```bash
gateway-guardian config set llm.enabled=true llm.provider=codex
gateway-guardian profile set hermes-prod llm_enabled=true
```

Supported providers:

- **Codex** — runs locally from the profile workspace with bypass/yolo approval mode
- **Claude** — runs locally from the profile workspace with bypass/yolo approval mode

Customize prompts in `[llm.codex].prompt` and `[llm.claude].prompt`. Gateway Guardian commits a pre-LLM checkpoint before invoking the provider, captures provider output in the profile state directory, rejects repairs that rewrite git history, and commits only repairs that restore health.

## Logs and Status

Use the CLI for service and profile state:

```bash
gateway-guardian status
gateway-guardian config show
gateway-guardian profile list
```

Supervisor and profile logs live under the configured `log_dir`, with per-profile state under `state_dir`.

## License

MIT

## Credits

Inspired by [Openclaw Guardian](https://github.com/LeoYeAI/openclaw-guardian)

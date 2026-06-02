---
name: gateway-guardian
description: "Deploy and manage Gateway Guardian, a uv-managed Python supervisor for Hermes and OpenClaw gateway profiles. Uses one TOML config, isolated per-profile repair/rollback/cooldown/logs, and optional local Codex or Claude repair."
metadata: {"gatewayGuardian": {"requires": {"bins": ["uv", "git"], "optionalBins": ["hermes", "openclaw", "codex", "claude"]}, "config": "~/.config/gateway-guardian/config.toml"}}
---

# Gateway Guardian

Gateway Guardian keeps local Hermes and OpenClaw gateway profiles healthy. Agents should guide users through the `uv run python scripts/gateway_guardian.py` CLI, one TOML config file, and one background service that supervises all enabled profiles.

## Operating Model

- CLI: `uv run python scripts/gateway_guardian.py`
- Config: `~/.config/gateway-guardian/config.toml`
- Service: one background supervisor running `uv run python scripts/gateway_guardian.py run`
- Profiles: Hermes and OpenClaw entries in the same TOML config
- Isolation: each profile has separate worker state, logs, rollback metadata, cooldown, and LLM repair output
- Default healthy check interval: 5 minutes / 300 seconds
- Python environment: uv project, Python 3.11+, no runtime dependencies outside the standard library

The global `default_check_interval_seconds` controls the default check interval. A profile may override it with `check_interval_seconds`.

Before setup, run `uv sync` from the repository root. Use `uv run python scripts/gateway_guardian.py ...` for all CLI commands so setup and service installation use the project Python environment.

## Information to Collect

Before setup, collect:

1. Target: `hermes` or `openclaw`
2. Profile name, such as `prod` or `staging`
3. Workspace path, which should be a git repository for rollback
4. CLI path, such as `hermes`, `openclaw`, or an absolute executable path
5. Profile arguments required by the target CLI, such as `--profile prod`
6. Rollback preference: enabled or disabled
7. Alerting preference, including any Discord webhook or no alerts
8. LLM repair preference: disabled, or explicit opt-in to local Codex or Claude

Do not instruct users to create per-profile services, load shell-based profile configuration, add process-matching patterns, or edit shell scripts manually. Use `setup`, `profile add`, `profile set`, and `config set`.

## Quick Start

Initialize git in the profile workspace if rollback should be available:

```bash
cd ~/.hermes/profiles/prod/workspace
git init
git config user.email "guardian@example.com"
git config user.name "Guardian"
git add -A && git commit -m "initial"
```

Configure a Hermes profile and start the supervisor:

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

Add more profiles to the same config:

```bash
uv run python scripts/gateway_guardian.py profile add \
  --target openclaw \
  --profile staging \
  --workspace ~/.openclaw/profiles/staging/workspace \
  --cli openclaw \
  --profile-arg --profile \
  --profile-arg staging

uv run python scripts/gateway_guardian.py reload
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

Use `start` once to install/start the background supervisor. Use `reload` after config changes when the supervisor is already running.

## TOML Example

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

## LLM Repair Guidance

LLM repair is a last-resort escalation and is disabled by default. Enable it only after explicit user consent:

```bash
uv run python scripts/gateway_guardian.py config set llm.enabled=true llm.provider=codex
uv run python scripts/gateway_guardian.py profile set hermes-prod llm_enabled=true
```

Codex and Claude run locally from the profile workspace in bypass/yolo mode so the repair attempt is noninteractive. Gateway Guardian commits a pre-LLM checkpoint first, captures prompt/output in profile state, rejects history rewrites, verifies health after repair, and commits only a successful repair.

## Agent Rules

- Prefer `setup` for the first profile.
- Prefer `profile add` for additional Hermes or OpenClaw profiles.
- Prefer `config set` and `profile set` for changes after setup.
- Keep all profiles in the single TOML config.
- Start one background service; do not create per-profile services.
- Do not require Codex or Claude unless the user explicitly enables LLM repair.

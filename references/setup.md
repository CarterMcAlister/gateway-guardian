# Gateway Guardian Agent Setup Guide

Use this guide when a user asks you to install or configure Gateway Guardian for Hermes or OpenClaw. Gateway Guardian is a uv-managed Python supervisor with one TOML config file and one background service that monitors every enabled profile.

## Agent operating rules

- Run commands from the repository root.
- Use `gateway-guardian ...` after `uv tool install .`, or `uv run gateway-guardian ...` while developing from a checkout.
- Do not edit the TOML file by hand unless the CLI cannot express the change.
- Do not create one service per profile. Gateway Guardian uses one supervisor service for all profiles.
- Do not ask the user for process patterns, shell env files, or health hooks.
- Do not enable LLM repair unless the user explicitly agrees.

## Information to collect

Ask for or infer:

1. target: `hermes` or `openclaw`;
2. profile name, such as `prod` or `staging`;
3. workspace path;
4. CLI executable, usually `hermes` or `openclaw`;
5. profile arguments, if needed, such as `--profile prod`;
6. whether rollback should be enabled;
7. alerting preference, including optional Discord webhook;
8. whether local Codex or Claude repair should be enabled.

## Prepare the project

```bash
uv sync
uv run gateway-guardian --help
uv tool install . --force
gateway-guardian --help
```

Gateway Guardian requires Python 3.11 or newer and currently uses only the Python standard library.

## Prepare each workspace for rollback

Rollback requires a git repository in the profile workspace. Use repository-local git config:

```bash
git -C ~/.hermes/profiles/prod/workspace init
git -C ~/.hermes/profiles/prod/workspace config user.email "guardian@example.com"
git -C ~/.hermes/profiles/prod/workspace config user.name "Guardian"
git -C ~/.hermes/profiles/prod/workspace add -A
git -C ~/.hermes/profiles/prod/workspace commit -m "initial"
```

Skip rollback only if the user does not want git-based recovery.

## Configure the first profile

Hermes example:

```bash
gateway-guardian setup \
  --target hermes \
  --profile prod \
  --workspace ~/.hermes/profiles/prod/workspace \
  --cli hermes \
  --profile-arg --profile \
  --profile-arg prod
```

OpenClaw example:

```bash
gateway-guardian setup \
  --target openclaw \
  --profile staging \
  --workspace ~/.openclaw/profiles/staging/workspace \
  --cli openclaw \
  --profile-arg --profile \
  --profile-arg staging
```

Config is written to:

```text
~/.config/gateway-guardian/config.toml
```

## Add more profiles

```bash
gateway-guardian profile add \
  --target hermes \
  --profile staging \
  --workspace ~/.hermes/profiles/staging/workspace \
  --cli hermes \
  --profile-arg --profile \
  --profile-arg staging
```

List profiles:

```bash
gateway-guardian profile list
```

## Start and manage the supervisor

Start the single background service:

```bash
gateway-guardian start
```

Check status:

```bash
gateway-guardian status
```

Reload after config changes:

```bash
gateway-guardian reload
```

Stop or restart:

```bash
gateway-guardian stop
gateway-guardian restart
```

The service backend is auto-detected: user systemd on Linux, LaunchAgent on macOS, otherwise a recorded `nohup` process.

## Change config through the CLI

```bash
# Show effective config
gateway-guardian config show

# Change global healthy-check interval to 10 minutes
gateway-guardian config set default_check_interval_seconds=600

# Override one profile's interval
gateway-guardian profile set hermes-prod check_interval_seconds=120

# Disable a profile
gateway-guardian profile set hermes-prod enabled=false
```

Healthy profiles are checked every 300 seconds by default. `default_check_interval_seconds` sets the global default; profile `check_interval_seconds` overrides it. Hermes profiles are checked with `hermes [profile args] gateway status --deep`; stale service definitions, stopped/unloaded gateway services, and `agent-secrets exited 1` are treated as unhealthy and repaired with `hermes [profile args] gateway start`.

## Configure Discord alerts

If the user provides a Discord webhook URL, set it through the CLI and reload the single supervisor:

```bash
gateway-guardian config set 'notifications.discord.webhook_url=https://discord.com/api/webhooks/...'
gateway-guardian reload
```

When configured, Gateway Guardian sends Discord webhooks when a profile first becomes unhealthy and repair starts, when a previously unhealthy or failed profile recovers, and when all repair paths fail. It does not send healthy startup alerts or repeated failure alerts for the same unresolved incident.

## Enable local LLM repair only with consent

LLM repair is disabled by default and requires both global and per-profile opt-in.

Codex:

```bash
gateway-guardian config set llm.enabled=true llm.provider=codex
gateway-guardian profile set hermes-prod llm_enabled=true
```

Claude:

```bash
gateway-guardian config set llm.enabled=true llm.provider=claude
gateway-guardian profile set hermes-prod llm_enabled=true
```

Gateway Guardian invokes Codex or Claude from the profile workspace in bypass/yolo mode. Before invocation it commits a pre-LLM checkpoint, records the branch and commit, and rejects repairs that rewrite history or switch branches. It commits LLM changes only after the normal health check passes.

Customize prompts through config keys:

```bash
gateway-guardian config set 'llm.codex.prompt=Repair {target} profile {profile} in {workspace}. Do not rewrite git history.'
gateway-guardian config set 'llm.claude.prompt=Repair {target} profile {profile} in {workspace}. Do not rewrite git history.'
```

## Verify setup

Run:

```bash
uv run python -m unittest tests.gateway_guardian_tests
gateway-guardian status
```

Confirm:

- the config contains every intended profile;
- one supervisor service is running;
- each enabled profile appears in `status`;
- each workspace has a git repository if rollback is enabled;
- LLM repair is disabled unless the user opted in;
- Discord webhook URL is empty unless the user provided one.

#!/usr/bin/env python3
"""Gateway Guardian CLI: multi-profile gateway supervisor."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+ expected.
    tomllib = None

VERSION = 1
TARGETS = {"hermes", "openclaw"}
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SCRIPT = Path(__file__).resolve()

DEFAULT_CODEX_PROMPT = """You are repairing a local {target} gateway profile named {profile}.
Work only in {workspace}. Diagnose the failed service, make the smallest safe fix,
and leave git history intact.
"""
DEFAULT_CLAUDE_PROMPT = DEFAULT_CODEX_PROMPT

GLOBAL_KEYS = {
    "version": int,
    "default_check_interval_seconds": int,
    "max_repair_attempts": int,
    "cooldown_seconds": int,
    "state_dir": str,
    "log_dir": str,
    "notifications.discord.webhook_url": str,
    "llm.enabled": bool,
    "llm.provider": str,
    "llm.timeout_seconds": int,
    "llm.codex.command": str,
    "llm.codex.model": str,
    "llm.codex.prompt": str,
    "llm.claude.command": str,
    "llm.claude.model": str,
    "llm.claude.prompt": str,
}
PROFILE_KEYS = {
    "id": str,
    "enabled": bool,
    "target": str,
    "profile": str,
    "workspace": str,
    "command": str,
    "args": list,
    "check_interval_seconds": int,
    "rollback_enabled": bool,
    "llm_enabled": bool,
}


def die(message: str) -> None:
    print(f"gateway-guardian: {message}", file=sys.stderr)
    raise SystemExit(1)


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gateway-guardian" / "config.toml"


def default_state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return str(Path(base) / "gateway-guardian")


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def default_config() -> dict[str, Any]:
    state = default_state_dir()
    return {
        "version": VERSION,
        "default_check_interval_seconds": 300,
        "max_repair_attempts": 3,
        "cooldown_seconds": 300,
        "state_dir": state,
        "log_dir": str(Path(state) / "logs"),
        "notifications": {"discord": {"webhook_url": ""}},
        "llm": {
            "enabled": False,
            "provider": "codex",
            "timeout_seconds": 900,
            "codex": {"command": "codex", "model": "", "prompt": DEFAULT_CODEX_PROMPT},
            "claude": {"command": "claude", "model": "", "prompt": DEFAULT_CLAUDE_PROMPT},
        },
        "profiles": [],
    }


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_config()
    if tomllib is None:
        die("Python tomllib is unavailable; Python 3.11 or newer is required")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    cfg = default_config()
    deep_merge(cfg, data)
    validate_config(cfg)
    return cfg


def deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_merge(dst[key], value)
        else:
            dst[key] = value


def toml_quote(value: str) -> str:
    return json.dumps(value)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, str) and "\n" in value:
        return '"""\n' + value.replace('"""', '\\"\\"\\"') + '"""'
    return toml_quote(str(value))


def dump_toml(cfg: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in ("version", "default_check_interval_seconds", "max_repair_attempts", "cooldown_seconds", "state_dir", "log_dir"):
        lines.append(f"{key} = {toml_value(cfg[key])}")
    lines.append("")
    lines.append("[notifications.discord]")
    lines.append(f"webhook_url = {toml_value(cfg['notifications']['discord'].get('webhook_url', ''))}")
    lines.append("")
    lines.append("[llm]")
    llm = cfg["llm"]
    for key in ("enabled", "provider", "timeout_seconds"):
        lines.append(f"{key} = {toml_value(llm[key])}")
    lines.append("")
    lines.append("[llm.codex]")
    for key in ("command", "model", "prompt"):
        lines.append(f"{key} = {toml_value(llm['codex'][key])}")
    lines.append("")
    lines.append("[llm.claude]")
    for key in ("command", "model", "prompt"):
        lines.append(f"{key} = {toml_value(llm['claude'][key])}")
    for profile in cfg.get("profiles", []):
        lines.append("")
        lines.append("[[profiles]]")
        for key in ("id", "enabled", "target", "profile", "workspace", "command", "args", "check_interval_seconds", "rollback_enabled", "llm_enabled"):
            lines.append(f"{key} = {toml_value(profile[key])}")
    return "\n".join(lines) + "\n"


def atomic_write_config(path: Path, cfg: dict[str, Any]) -> None:
    validate_config(cfg)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(path, path.with_name(path.name + f".bak-{stamp}"))
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(dump_toml(cfg))
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def get_dotted(cfg: dict[str, Any], key: str) -> Any:
    node: Any = cfg
    for part in key.split("."):
        node = node[part]
    return node


def set_dotted(cfg: dict[str, Any], key: str, value: Any) -> None:
    node: Any = cfg
    parts = key.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def parse_value(raw: str, expected: type) -> Any:
    if expected is str:
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            return raw[1:-1]
        return raw
    if expected is bool:
        lowered = raw.lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        die(f"invalid boolean value {raw!r}")
    if expected is int:
        try:
            value = int(raw, 10)
        except ValueError:
            die(f"invalid number value {raw!r}")
        if value < 0:
            die("number values must be non-negative")
        return value
    if expected is list:
        try:
            parsed = tomllib.loads("v = " + raw)["v"] if tomllib else None
        except Exception:
            parsed = [item for item in raw.split(",") if item]
        if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
            die("args must be an array of strings")
        return parsed
    die("unsupported value type")


def apply_assignments(cfg: dict[str, Any], assignments: list[str], allowed: dict[str, type], profile: dict[str, Any] | None = None) -> None:
    target = profile if profile is not None else cfg
    for assignment in assignments:
        if "=" not in assignment:
            die(f"assignment must be KEY=VALUE: {assignment}")
        key, raw = assignment.split("=", 1)
        if key not in allowed:
            die(f"unknown key: {key}")
        value = parse_value(raw, allowed[key])
        if profile is None:
            set_dotted(cfg, key, value)
        else:
            target[key] = value
    validate_config(cfg)


def profile_id(target: str, profile: str) -> str:
    return f"{target}-{profile}".lower().replace("/", "-").replace(" ", "-")


def validate_profile(p: dict[str, Any]) -> None:
    for key, expected in PROFILE_KEYS.items():
        if key not in p:
            die(f"profile missing {key}")
        if not isinstance(p[key], expected):
            die(f"profile {p.get('id', '<unknown>')} has invalid {key}")
    if p["target"] not in TARGETS:
        die(f"unsupported target: {p['target']}")
    if not PROFILE_ID_RE.match(p["id"]):
        die(f"invalid profile id: {p['id']}")
    if not p["workspace"]:
        die(f"profile {p['id']} missing workspace")
    if not p["command"]:
        die(f"profile {p['id']} missing command")
    for key in ("check_interval_seconds",):
        if p[key] <= 0:
            die(f"profile {p['id']} {key} must be positive")


def validate_config(cfg: dict[str, Any]) -> None:
    if cfg.get("version") != VERSION:
        die("unsupported config version")
    for key in ("default_check_interval_seconds", "max_repair_attempts", "cooldown_seconds"):
        if not isinstance(cfg.get(key), int) or cfg[key] < 0:
            die(f"{key} must be a non-negative number")
    if cfg["default_check_interval_seconds"] <= 0:
        die("default_check_interval_seconds must be positive")
    if cfg["llm"].get("provider") not in {"codex", "claude"}:
        die("llm.provider must be codex or claude")
    ids: set[str] = set()
    for p in cfg.get("profiles", []):
        validate_profile(p)
        if p["id"] in ids:
            die(f"duplicate profile id: {p['id']}")
        ids.add(p["id"])


def ensure_state_dirs(cfg: dict[str, Any]) -> None:
    state = expand_path(cfg["state_dir"])
    logs = expand_path(cfg["log_dir"])
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    for p in cfg.get("profiles", []):
        (state / "profiles" / p["id"]).mkdir(parents=True, exist_ok=True)


def make_profile(args: argparse.Namespace) -> dict[str, Any]:
    target = args.target
    profile = args.profile
    command = args.cli or target
    workspace = args.workspace or f"~/.{target}/profiles/{profile}/workspace"
    return {
        "id": profile_id(target, profile),
        "enabled": True,
        "target": target,
        "profile": profile,
        "workspace": workspace,
        "command": command,
        "args": args.profile_arg or [],
        "check_interval_seconds": 300,
        "rollback_enabled": True,
        "llm_enabled": False,
    }


def find_profile(cfg: dict[str, Any], pid: str) -> dict[str, Any]:
    for p in cfg.get("profiles", []):
        if p["id"] == pid:
            return p
    die(f"profile not found: {pid}")


def cmd_setup(args: argparse.Namespace) -> None:
    target = args.target or input("Target (hermes/openclaw): ").strip()
    if target not in TARGETS:
        die("target must be hermes or openclaw")
    profile = args.profile or input("Profile name: ").strip()
    if not profile:
        die("profile is required")
    args.target, args.profile = target, profile
    if args.workspace is None:
        entered = input("Workspace path: ").strip()
        args.workspace = entered or f"~/.{target}/profiles/{profile}/workspace"
    cfg = load_config(args.config)
    p = make_profile(args)
    cfg["profiles"] = [existing for existing in cfg.get("profiles", []) if existing["id"] != p["id"]]
    cfg["profiles"].append(p)
    atomic_write_config(args.config, cfg)
    ensure_state_dirs(cfg)
    if args.start:
        service_start(args)


def cmd_config_show(args: argparse.Namespace) -> None:
    print(dump_toml(load_config(args.config)), end="")


def cmd_config_set(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    apply_assignments(cfg, args.assignments, GLOBAL_KEYS)
    atomic_write_config(args.config, cfg)


def cmd_profile_add(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    p = make_profile(args)
    if any(existing["id"] == p["id"] for existing in cfg.get("profiles", [])):
        die(f"duplicate profile id: {p['id']}")
    cfg.setdefault("profiles", []).append(p)
    atomic_write_config(args.config, cfg)
    ensure_state_dirs(cfg)


def cmd_profile_list(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    for p in cfg.get("profiles", []):
        print(f"{p['id']}\t{p['target']}\t{p['profile']}\t{'enabled' if p['enabled'] else 'disabled'}")


def cmd_profile_set(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    p = find_profile(cfg, args.profile_id)
    apply_assignments(cfg, args.assignments, PROFILE_KEYS, p)
    atomic_write_config(args.config, cfg)


def cmd_profile_remove(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    before = len(cfg.get("profiles", []))
    cfg["profiles"] = [p for p in cfg.get("profiles", []) if p["id"] != args.profile_id]
    if len(cfg["profiles"]) == before:
        die(f"profile not found: {args.profile_id}")
    atomic_write_config(args.config, cfg)


def run_cmd(argv: list[str], cwd: Path | None = None, timeout: int | None = None, output: Path | None = None) -> subprocess.CompletedProcess[str]:
    if output is None:
        return subprocess.run(argv, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    with output.open("a", encoding="utf-8") as out:
        return subprocess.run(argv, cwd=str(cwd) if cwd else None, text=True, stdout=out, stderr=out, timeout=timeout, check=False)


def capture_cmd(argv: list[str], cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)


def append_captured_output(path: Path, result: subprocess.CompletedProcess[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if result.stdout:
            fh.write(result.stdout)
            if not result.stdout.endswith("\n"):
                fh.write("\n")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def post_discord_webhook(url: str, payload: dict[str, Any], *, timeout: int = 10) -> tuple[bool, str]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "gateway-guardian/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, exc.__class__.__name__
    if 200 <= status < 300:
        return True, ""
    return False, f"HTTP {status}"


class Worker:
    def __init__(self, cfg: dict[str, Any], profile: dict[str, Any]):
        self.cfg = cfg
        self.profile = profile
        self.workspace = expand_path(profile["workspace"])
        self.state = expand_path(cfg["state_dir"]) / "profiles" / profile["id"]
        self.log = self.state / "repair.log"
        self.gateway_pid = self.state / "gateway.pid"
        self.last_good = self.state / "last-good-commit"
        self.last_backup = self.state / "last-backup-date"
        self.notification_status = self.state / "notification-status"
        self.stop = False

    def log_line(self, message: str) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}\n")

    def discord_webhook_url(self) -> str:
        return str(self.cfg.get("notifications", {}).get("discord", {}).get("webhook_url", "")).strip()

    def read_notification_status(self) -> str:
        try:
            return self.notification_status.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def write_notification_status(self, status: str) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        self.notification_status.write_text(status + "\n", encoding="utf-8")

    def notify_discord(self, title: str, description: str, *, color: int) -> None:
        url = self.discord_webhook_url()
        if not url:
            return
        payload = {
            "username": "Gateway Guardian",
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                    "fields": [
                        {"name": "Profile", "value": self.profile["id"], "inline": True},
                        {"name": "Target", "value": self.profile["target"], "inline": True},
                        {"name": "Workspace", "value": str(self.workspace), "inline": False},
                    ],
                }
            ],
        }
        ok, detail = post_discord_webhook(url, payload)
        if not ok:
            self.log_line(f"discord webhook failed: {detail}")

    def mark_unhealthy(self) -> None:
        previous = self.read_notification_status()
        if previous == "failed":
            return
        if previous not in {"unhealthy", "failed"}:
            self.notify_discord(
                "Gateway Guardian detected an unhealthy profile",
                "Health check failed and automatic repair is starting.",
                color=0xFEE75C,
            )
        self.write_notification_status("unhealthy")

    def mark_healthy(self, detail: str) -> None:
        previous = self.read_notification_status()
        if previous in {"unhealthy", "failed"}:
            self.notify_discord(
                "Gateway Guardian recovered a profile",
                detail,
                color=0x57F287,
            )
        self.write_notification_status("healthy")

    def mark_repair_failed(self) -> None:
        previous = self.read_notification_status()
        if previous != "failed":
            self.notify_discord(
                "Gateway Guardian could not repair a profile",
                "Doctor repair, rollback, and configured LLM repair did not restore health.",
                color=0xED4245,
            )
        self.write_notification_status("failed")

    def target_cmd(self, *tail: str) -> list[str]:
        return [self.profile["command"], *self.profile["args"], *tail]

    def is_hermes_healthy(self) -> bool:
        result = capture_cmd(self.target_cmd("gateway", "status", "--deep"), cwd=self.workspace, timeout=60)
        append_captured_output(self.log, result)
        output = result.stdout or ""
        unhealthy_markers = (
            "Service definition is stale relative to the current Hermes install",
            "agent-secrets exited ",
            "Gateway service is not loaded",
            "Gateway service not loaded",
            "Gateway service is not running",
            "Gateway service not running",
            "Status:       ✗",
            "Status:       not running",
        )
        for marker in unhealthy_markers:
            if marker in output:
                self.log_line(f"hermes gateway health failed: {marker}")
                return False
        if result.returncode == 0:
            return True
        self.log_line(f"hermes gateway health command exited {result.returncode}")
        return False

    def is_healthy(self) -> bool:
        if self.profile["target"] == "hermes":
            try:
                return self.is_hermes_healthy()
            except Exception as exc:
                self.log_line(f"hermes gateway status check failed: {exc}")
                return False
        allow_pid_fallback = True
        try:
            status = run_cmd(self.target_cmd("status"), cwd=self.workspace, timeout=20, output=self.log)
            if status.returncode == 0:
                return True
            allow_pid_fallback = status.returncode in {2, 126, 127}
        except Exception as exc:
            self.log_line(f"status check failed: {exc}")
        if allow_pid_fallback and self.gateway_pid.exists():
            try:
                return pid_alive(int(self.gateway_pid.read_text(encoding="utf-8").strip()))
            except Exception:
                return False
        return False

    def start_gateway(self) -> None:
        self.stop_gateway()
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self.profile["target"] == "hermes":
            result = run_cmd(self.target_cmd("gateway", "start"), cwd=self.workspace, timeout=120, output=self.log)
            self.log_line(f"started hermes gateway service exit {result.returncode}")
            return
        out = self.log.open("a", encoding="utf-8")
        proc = subprocess.Popen(self.target_cmd("gateway"), cwd=str(self.workspace), stdout=out, stderr=out, start_new_session=True)
        out.close()
        self.gateway_pid.write_text(str(proc.pid), encoding="utf-8")
        self.log_line(f"started gateway pid {proc.pid}")

    def stop_gateway(self) -> None:
        if not self.gateway_pid.exists():
            return
        try:
            pid = int(self.gateway_pid.read_text(encoding="utf-8").strip())
        except Exception:
            self.gateway_pid.unlink(missing_ok=True)
            return
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    if not pid_alive(pid):
                        break
                    time.sleep(0.1)
                if pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.gateway_pid.unlink(missing_ok=True)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return capture_cmd(["git", *args], cwd=self.workspace, timeout=120)

    def current_branch(self) -> str:
        result = self.git("rev-parse", "--abbrev-ref", "HEAD")
        branch = result.stdout.strip()
        if result.returncode == 0 and branch and branch != "HEAD":
            return branch
        result = self.git("branch", "--show-current")
        return result.stdout.strip() if result.returncode == 0 else ""

    def record_last_good(self) -> None:
        result = self.git("rev-parse", "HEAD")
        if result.returncode == 0:
            self.last_good.write_text(result.stdout.strip() + "\n", encoding="utf-8")

    def daily_backup(self) -> None:
        today = dt.date.today().isoformat()
        if self.last_backup.exists() and self.last_backup.read_text(encoding="utf-8").strip() == today:
            return
        if self.git("rev-parse", "--is-inside-work-tree").returncode != 0:
            return
        status = self.git("status", "--porcelain")
        if status.returncode != 0:
            return
        if not status.stdout.strip():
            self.last_backup.write_text(today + "\n", encoding="utf-8")
            return
        if self.git("add", "-A").returncode != 0:
            return
        commit = self.git("commit", "-m", f"Gateway Guardian daily backup {today}")
        if commit.returncode == 0:
            self.last_backup.write_text(today + "\n", encoding="utf-8")

    def doctor(self) -> bool:
        result = run_cmd(self.target_cmd("doctor", "--fix"), cwd=self.workspace, timeout=300, output=self.log)
        return result.returncode == 0

    def rollback(self) -> bool:
        if not self.profile.get("rollback_enabled", True) or not self.last_good.exists():
            return False
        commit = self.last_good.read_text(encoding="utf-8").strip()
        if not commit or self.git("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
            return False
        if self.git("reset", "--hard", commit).returncode != 0:
            return False
        self.start_gateway()
        return self.is_healthy()

    def llm_repair(self) -> bool:
        llm = self.cfg["llm"]
        if not (llm.get("enabled") and self.profile.get("llm_enabled")):
            return False
        if self.git("rev-parse", "--is-inside-work-tree").returncode != 0:
            return False
        provider = llm["provider"]
        provider_cfg = llm[provider]
        branch_name = self.current_branch()
        if not branch_name:
            return False
        self.git("add", "-A")
        checkpoint_msg = f"Gateway Guardian pre-LLM checkpoint {self.profile['id']}"
        if self.git("commit", "--allow-empty", "-m", checkpoint_msg).returncode != 0:
            return False
        pre = self.git("rev-parse", "HEAD").stdout.strip()
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        safety = f"guardian/pre-llm-{self.profile['id']}-{stamp}"
        if self.git("branch", safety, pre).returncode != 0:
            if self.git("tag", safety.replace("/", "-"), pre).returncode != 0:
                return False
        (self.state / "llm-checkpoint.json").write_text(json.dumps({"branch": branch_name, "commit": pre, "safety": safety}) + "\n", encoding="utf-8")
        prompt = provider_cfg["prompt"].format(target=self.profile["target"], profile=self.profile["profile"], workspace=str(self.workspace))
        prompt += "\nSuccess criteria: restore the gateway health check without rewriting history or making unrelated changes.\n"
        prompt_file = self.state / "llm-prompt.txt"
        output_file = self.state / "llm-output.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        command = [provider_cfg["command"]]
        if provider == "codex":
            command += ["exec", "-C", str(self.workspace), "--dangerously-bypass-approvals-and-sandbox", "--output-last-message", str(output_file)]
            if provider_cfg.get("model"):
                command += ["--model", provider_cfg["model"]]
            command.append(prompt)
        else:
            command += ["-p", "--dangerously-skip-permissions"]
            if provider_cfg.get("model"):
                command += ["--model", provider_cfg["model"]]
            command.append(prompt)
        try:
            run_cmd(command, cwd=self.workspace, timeout=int(llm.get("timeout_seconds", 900)), output=output_file)
        except Exception as exc:
            self.log_line(f"llm repair failed: {exc}")
            self.git("reset", "--hard", pre)
            return False
        after_branch = self.current_branch()
        if after_branch != branch_name or self.git("merge-base", "--is-ancestor", pre, "HEAD").returncode != 0:
            self.log_line("llm repair rejected: history rewrite or branch change")
            self.git("reset", "--hard", pre)
            return False
        self.start_gateway()
        if self.is_healthy():
            self.git("add", "-A")
            if self.git("status", "--porcelain").stdout.strip():
                self.git("commit", "-m", f"Gateway Guardian LLM repair {self.profile['id']}")
            self.record_last_good()
            return True
        self.git("reset", "--hard", pre)
        return False

    def repair_flow(self, *, cooldown: bool = True) -> bool:
        self.log_line("profile unhealthy; starting repair")
        self.mark_unhealthy()
        attempts = int(self.cfg.get("max_repair_attempts", 3))
        for _ in range(attempts):
            self.doctor()
            self.start_gateway()
            if self.is_healthy():
                self.record_last_good()
                self.mark_healthy("Profile health was restored by the target doctor repair flow.")
                return True
        if self.rollback():
            self.record_last_good()
            self.mark_healthy("Profile health was restored by git rollback to the last-known-good commit.")
            return True
        if self.llm_repair():
            self.mark_healthy("Profile health was restored by configured local LLM repair.")
            return True
        self.mark_repair_failed()
        if cooldown:
            time.sleep(int(self.cfg.get("cooldown_seconds", 300)))
        return False

    def iteration(self, *, wait_when_healthy: bool = True) -> bool:
        if self.is_healthy():
            self.record_last_good()
            self.daily_backup()
            self.mark_healthy("Profile health check passed.")
            if wait_when_healthy:
                interval = self.profile.get("check_interval_seconds") or self.cfg["default_check_interval_seconds"]
                for _ in range(int(interval)):
                    if self.stop:
                        break
                    time.sleep(1)
            return True
        else:
            return self.repair_flow(cooldown=wait_when_healthy)

    def loop(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        lock = self.state / "lock"
        try:
            lock.mkdir()
        except FileExistsError:
            die(f"profile already locked: {self.profile['id']}")
        (self.state / "worker.pid").write_text(str(os.getpid()), encoding="utf-8")
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
        try:
            if os.environ.get("GATEWAY_GUARDIAN_ONCE") == "1":
                if not self.iteration(wait_when_healthy=False):
                    self.log_line("profile remains unhealthy after one repair iteration")
                    raise SystemExit(1)
                return
            while not self.stop:
                self.iteration()
        finally:
            self.stop_gateway()
            (self.state / "worker.pid").unlink(missing_ok=True)
            try:
                lock.rmdir()
            except OSError:
                pass


def cmd_worker(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    ensure_state_dirs(cfg)
    Worker(cfg, find_profile(cfg, args.profile_id)).loop()


class Supervisor:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.reload = True
        self.stop = False
        self.children: dict[str, subprocess.Popen[Any]] = {}

    def state_dir(self) -> Path:
        return expand_path(load_config(self.config_path)["state_dir"])

    def write_pid(self, cfg: dict[str, Any]) -> None:
        state = expand_path(cfg["state_dir"])
        state.mkdir(parents=True, exist_ok=True)
        (state / "supervisor.pid").write_text(str(os.getpid()), encoding="utf-8")

    def start_child(self, cfg: dict[str, Any], p: dict[str, Any]) -> subprocess.Popen[Any]:
        log_dir = expand_path(cfg["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        log = (log_dir / f"{p['id']}.worker.log").open("a", encoding="utf-8")
        return subprocess.Popen([sys.executable, str(SCRIPT), "_worker", "--config", str(self.config_path), "--profile-id", p["id"]], stdout=log, stderr=log, start_new_session=True)

    def reconcile(self, cfg: dict[str, Any]) -> None:
        wanted = {p["id"]: p for p in cfg.get("profiles", []) if p.get("enabled")}
        for pid in list(self.children):
            if pid not in wanted:
                self.children[pid].terminate()
                self.children.pop(pid)
        for pid, proc in list(self.children.items()):
            if proc.poll() is not None:
                self.children.pop(pid)
        for pid, profile in wanted.items():
            if pid not in self.children:
                self.children[pid] = self.start_child(cfg, profile)

    def loop(self) -> None:
        signal.signal(signal.SIGHUP, lambda *_: setattr(self, "reload", True))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
        cfg = load_config(self.config_path)
        ensure_state_dirs(cfg)
        self.write_pid(cfg)
        try:
            while not self.stop:
                if self.reload:
                    cfg = load_config(self.config_path)
                    ensure_state_dirs(cfg)
                    self.write_pid(cfg)
                    self.reload = False
                self.reconcile(cfg)
                time.sleep(2)
        finally:
            for proc in self.children.values():
                proc.terminate()
            for proc in self.children.values():
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            (expand_path(cfg["state_dir"]) / "supervisor.pid").unlink(missing_ok=True)


def cmd_run(args: argparse.Namespace) -> None:
    Supervisor(args.config).loop()


def service_state_path(cfg: dict[str, Any]) -> Path:
    return expand_path(cfg["state_dir"]) / "service.json"


def supervisor_pid(cfg: dict[str, Any]) -> int | None:
    path = expand_path(cfg["state_dir"]) / "supervisor.pid"
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return pid if pid_alive(pid) else None


def service_args(config: Path) -> list[str]:
    args = [sys.executable, str(SCRIPT), "run"]
    if config != default_config_path():
        args += ["--config", str(config)]
    return args


def service_environment() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}


def detect_backend() -> str:
    if platform.system() == "Linux" and shutil.which("systemctl"):
        result = subprocess.run(["systemctl", "--user", "is-system-running"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode in {0, 1}:
            return "systemd"
    if platform.system() == "Darwin":
        return "launchd"
    return "nohup"


def service_start(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    ensure_state_dirs(cfg)
    if supervisor_pid(cfg):
        print("Gateway Guardian is already running")
        return
    backend = detect_backend()
    if backend == "systemd":
        user_dir = Path.home() / ".config" / "systemd" / "user"
        user_dir.mkdir(parents=True, exist_ok=True)
        service = user_dir / "gateway-guardian.service"
        service.write_text("[Unit]\nDescription=Gateway Guardian\n\n[Service]\nEnvironment=PATH=" + service_environment()["PATH"] + "\nExecStart=" + " ".join(service_args(args.config)) + "\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "gateway-guardian.service"], check=False)
    elif backend == "launchd":
        launch_dir = Path.home() / "Library" / "LaunchAgents"
        launch_dir.mkdir(parents=True, exist_ok=True)
        plist = launch_dir / "com.gateway-guardian.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": "com.gateway-guardian",
            "ProgramArguments": service_args(args.config),
            "RunAtLoad": True,
            "KeepAlive": True,
            "EnvironmentVariables": service_environment(),
            "StandardOutPath": str(expand_path(cfg["log_dir"]) / "supervisor.log"),
            "StandardErrorPath": str(expand_path(cfg["log_dir"]) / "supervisor.log"),
        }))
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=False)
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.gateway-guardian"], check=False)
    else:
        log = (expand_path(cfg["log_dir"]) / "supervisor.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        out = log.open("a", encoding="utf-8")
        proc = subprocess.Popen(service_args(args.config), stdout=out, stderr=out, start_new_session=True)
        out.close()
        service_state_path(cfg).write_text(json.dumps({"backend": "nohup", "pid": proc.pid}) + "\n", encoding="utf-8")
        return
    service_state_path(cfg).write_text(json.dumps({"backend": backend}) + "\n", encoding="utf-8")


def service_stop(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    backend = "nohup"
    sp = service_state_path(cfg)
    if sp.exists():
        try:
            backend = json.loads(sp.read_text(encoding="utf-8")).get("backend", backend)
        except Exception:
            pass
    if backend == "systemd":
        subprocess.run(["systemctl", "--user", "stop", "gateway-guardian.service"], check=False)
    elif backend == "launchd":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/com.gateway-guardian"], check=False)
    pid = supervisor_pid(cfg)
    if pid:
        os.kill(pid, signal.SIGTERM)
    sp.unlink(missing_ok=True)


def service_reload(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    pid = supervisor_pid(cfg)
    if not pid:
        die("Gateway Guardian supervisor is not running")
    os.kill(pid, signal.SIGHUP)


def service_status(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    pid = supervisor_pid(cfg)
    print(f"supervisor: {'running pid ' + str(pid) if pid else 'stopped'}")
    state = expand_path(cfg["state_dir"]) / "profiles"
    for p in cfg.get("profiles", []):
        worker_pid_file = state / p["id"] / "worker.pid"
        wp = None
        if worker_pid_file.exists():
            try:
                candidate = int(worker_pid_file.read_text(encoding="utf-8").strip())
                wp = candidate if pid_alive(candidate) else None
            except Exception:
                pass
        print(f"{p['id']}: {'enabled' if p['enabled'] else 'disabled'}, worker {'running pid ' + str(wp) if wp else 'stopped'}")


def service_restart(args: argparse.Namespace) -> None:
    service_stop(args)
    time.sleep(1)
    service_start(args)


def normalize_profile_arg(argv: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--profile-arg" and i + 1 < len(argv):
            out.append("--profile-arg=" + argv[i + 1])
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=default_config_path(), help="config TOML path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway-guardian", description="Configure and run the Gateway Guardian supervisor.")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="create or update config and optionally start service")
    add_config_arg(setup)
    setup.add_argument("--target", choices=sorted(TARGETS))
    setup.add_argument("--profile")
    setup.add_argument("--workspace")
    setup.add_argument("--cli")
    setup.add_argument("--profile-arg", action="append")
    setup.add_argument("--start", action="store_true")
    setup.set_defaults(func=cmd_setup)
    run = sub.add_parser("run", help="run the foreground supervisor")
    add_config_arg(run)
    run.set_defaults(func=cmd_run)
    for name, func in {"start": service_start, "stop": service_stop, "restart": service_restart, "reload": service_reload, "status": service_status}.items():
        p = sub.add_parser(name, help=f"{name} the background supervisor")
        add_config_arg(p)
        p.set_defaults(func=func)
    config = sub.add_parser("config", help="manage global config")
    csub = config.add_subparsers(dest="config_command", required=True)
    show = csub.add_parser("show", help="print config")
    add_config_arg(show)
    show.set_defaults(func=cmd_config_show)
    cset = csub.add_parser("set", help="set global KEY=VALUE entries")
    add_config_arg(cset)
    cset.add_argument("assignments", nargs="+")
    cset.set_defaults(func=cmd_config_set)
    profile = sub.add_parser("profile", help="manage profiles")
    psub = profile.add_subparsers(dest="profile_command", required=True)
    padd = psub.add_parser("add", help="add a profile")
    add_config_arg(padd)
    padd.add_argument("--target", choices=sorted(TARGETS), required=True)
    padd.add_argument("--profile", required=True)
    padd.add_argument("--workspace")
    padd.add_argument("--cli")
    padd.add_argument("--profile-arg", action="append")
    padd.set_defaults(func=cmd_profile_add)
    plist = psub.add_parser("list", help="list profiles")
    add_config_arg(plist)
    plist.set_defaults(func=cmd_profile_list)
    pset = psub.add_parser("set", help="set profile KEY=VALUE entries")
    add_config_arg(pset)
    pset.add_argument("profile_id")
    pset.add_argument("assignments", nargs="+")
    pset.set_defaults(func=cmd_profile_set)
    prem = psub.add_parser("remove", help="remove a profile")
    add_config_arg(prem)
    prem.add_argument("profile_id")
    prem.set_defaults(func=cmd_profile_remove)
    for worker_name in ("_worker", "__worker"):
        worker = sub.add_parser(worker_name, help=argparse.SUPPRESS)
        add_config_arg(worker)
        worker.add_argument("--profile-id", required=True)
        worker.set_defaults(func=cmd_worker)
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = normalize_profile_arg(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

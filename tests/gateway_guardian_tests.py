import http.server
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import textwrap
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "scripts" / "gateway_guardian.py"


def gateway_command():
    first = GATEWAY.read_bytes()[:128] if GATEWAY.exists() else b""
    if first.startswith(b"#!") and b"python" in first.splitlines()[0].lower():
        return [sys.executable, str(GATEWAY)]
    if GATEWAY.suffix == ".py":
        return [sys.executable, str(GATEWAY)]
    return [str(GATEWAY)]


def write_executable(path, body):
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def start_webhook_server():
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, requests, f"http://127.0.0.1:{server.server_port}/webhook"


class GatewayGuardianTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.config_home = self.root / "xdg-config"
        self.state_home = self.root / "xdg-state"
        self.bin = self.root / "bin"
        self.home.mkdir()
        self.config_home.mkdir()
        self.state_home.mkdir()
        self.bin.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_STATE_HOME": str(self.state_home),
                "PATH": f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}",
                "PYTHONUNBUFFERED": "1",
            }
        )
        self.config = self.config_home / "gateway-guardian" / "config.toml"
        self.cmd = gateway_command()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, input_text=None, check=True, env=None, timeout=10):
        proc = subprocess.run(
            [*self.cmd, *args],
            input=input_text,
            text=True,
            capture_output=True,
            env=env or self.env,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            self.fail(
                f"gateway-guardian {' '.join(args)} failed with {proc.returncode}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return proc

    def load_config(self, path=None):
        with (path or self.config).open("rb") as fh:
            return tomllib.load(fh)

    def write_fake_target(self, name):
        script = self.bin / name
        write_executable(
            script,
            """\
            #!/usr/bin/env python3
            import os, signal, sys, time
            cwd = os.getcwd()
            with open(os.path.join(cwd, f"{os.path.basename(sys.argv[0])}.argv"), "a", encoding="utf-8") as fh:
                fh.write(" ".join(sys.argv[1:]) + "\\n")
            if "status" in sys.argv:
                sys.exit(0 if os.path.exists(os.path.join(cwd, "healthy")) else 1)
            if "doctor" in sys.argv:
                sys.exit(0 if os.path.exists(os.path.join(cwd, "doctor_ok")) else 1)
            if "gateway" in sys.argv:
                if not os.path.exists(os.path.join(cwd, "healthy")):
                    sys.exit(1)
                stop = False
                def handler(_sig, _frame):
                    global stop
                    stop = True
                signal.signal(signal.SIGTERM, handler)
                deadline = time.time() + 2
                while not stop and time.time() < deadline:
                    time.sleep(0.05)
                sys.exit(0)
            sys.exit(0)
            """,
        )
        return script

    def write_doctor_repair_target(self, name):
        script = self.bin / name
        write_executable(
            script,
            """\
            #!/usr/bin/env python3
            import os, signal, sys, time
            cwd = os.getcwd()
            with open(os.path.join(cwd, f"{os.path.basename(sys.argv[0])}.argv"), "a", encoding="utf-8") as fh:
                fh.write(" ".join(sys.argv[1:]) + "\\n")
            if "status" in sys.argv:
                sys.exit(0 if os.path.exists(os.path.join(cwd, "healthy")) else 1)
            if "doctor" in sys.argv:
                with open(os.path.join(cwd, "healthy"), "w", encoding="utf-8") as fh:
                    fh.write("1")
                sys.exit(0)
            if "gateway" in sys.argv:
                if not os.path.exists(os.path.join(cwd, "healthy")):
                    sys.exit(1)
                stop = False
                def handler(_sig, _frame):
                    global stop
                    stop = True
                signal.signal(signal.SIGTERM, handler)
                deadline = time.time() + 2
                while not stop and time.time() < deadline:
                    time.sleep(0.05)
                sys.exit(0)
            sys.exit(0)
            """,
        )
        return script

    def write_hermes_gateway_target(self, name):
        script = self.bin / name
        write_executable(
            script,
            """\
            #!/usr/bin/env python3
            import os, sys
            cwd = os.getcwd()
            with open(os.path.join(cwd, f"{os.path.basename(sys.argv[0])}.argv"), "a", encoding="utf-8") as fh:
                fh.write(" ".join(sys.argv[1:]) + "\\n")
            if "gateway" in sys.argv and "status" in sys.argv:
                if os.path.exists(os.path.join(cwd, "healthy")):
                    print("✓ Gateway service is loaded")
                    print('"PID" = 1234;')
                    sys.exit(0)
                if os.path.exists(os.path.join(cwd, "stale_service")):
                    print("⚠ Service definition is stale relative to the current Hermes install")
                    print("  Run: hermes gateway start")
                    print("✓ Gateway service is loaded")
                    sys.exit(0)
                if os.path.exists(os.path.join(cwd, "secrets_failed")):
                    print("agent-secrets: agent-secrets exited 1: export failed: ANTHROPIC_API_KEY")
                    print("✓ Gateway service is loaded")
                    sys.exit(0)
                print("Gateway service is not running")
                sys.exit(1)
            if "doctor" in sys.argv:
                sys.exit(1)
            if "gateway" in sys.argv and "start" in sys.argv:
                for marker in ("stale_service", "secrets_failed"):
                    try:
                        os.unlink(os.path.join(cwd, marker))
                    except FileNotFoundError:
                        pass
                with open(os.path.join(cwd, "healthy"), "w", encoding="utf-8") as fh:
                    fh.write("1")
                sys.exit(0)
            if "status" in sys.argv:
                sys.exit(0 if os.path.exists(os.path.join(cwd, "healthy")) else 1)
            sys.exit(0)
            """,
        )
        return script

    def worker_command_name(self):
        for name in ("__worker", "_worker"):
            proc = subprocess.run(
                [*self.cmd, name, "--help"],
                text=True,
                capture_output=True,
                env=self.env,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                return name
        self.fail("gateway-guardian does not expose a hidden worker command")

    def write_fake_git(self):
        script = self.bin / "git"
        write_executable(
            script,
            """\
            #!/usr/bin/env python3
            import json, os, sys
            state_path = os.path.join(os.getcwd(), ".gitfake.json")
            if os.path.exists(state_path):
                with open(state_path, encoding="utf-8") as fh:
                    state = json.load(fh)
            else:
                state = {"branch": "main", "history": ["initial"], "head": "initial", "commits": 0}
            state.setdefault("known_commits", list(state.get("history", [])))
            for commit in [*state.get("history", []), state.get("head")]:
                if commit and commit not in state["known_commits"]:
                    state["known_commits"].append(commit)
            def save():
                with open(state_path, "w", encoding="utf-8") as fh:
                    json.dump(state, fh)
            args = sys.argv[1:]
            with open(os.path.join(os.getcwd(), ".gitfake.calls"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(args) + "\\n")
            if not args:
                sys.exit(0)
            def resolve(commit):
                return state["head"] if commit == "HEAD" else commit
            def remember(commit):
                if commit not in state["known_commits"]:
                    state["known_commits"].append(commit)
            def known(commit):
                return commit in state["known_commits"]
            def ancestry_through(commit):
                if commit in state["history"]:
                    return state["history"][: state["history"].index(commit) + 1]
                if commit == state["head"]:
                    return list(state["history"])
                if known(commit):
                    return state["known_commits"][: state["known_commits"].index(commit) + 1]
                return []
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                print(state.get("branch", "main"))
            elif args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                print("true")
            elif args[:2] == ["rev-parse", "--show-toplevel"]:
                print(os.getcwd())
            elif args[0] == "rev-parse":
                print(state["head"])
            elif args[:2] == ["symbolic-ref", "--short"]:
                print(state.get("branch", "main"))
            elif args[0] == "cat-file":
                commit = resolve(args[-1].split("^{", 1)[0])
                sys.exit(0 if known(commit) else 1)
            elif args[0] == "status":
                if os.path.exists(os.path.join(os.getcwd(), ".dirty")):
                    print(" M changed.txt")
            elif args[0] == "diff":
                sys.exit(1 if os.path.exists(os.path.join(os.getcwd(), ".dirty")) else 0)
            elif args[0] == "add":
                pass
            elif args[0] == "commit":
                if os.path.exists(os.path.join(os.getcwd(), ".fail_commit")):
                    sys.exit(1)
                state["commits"] = int(state.get("commits", 0)) + 1
                state["head"] = f"commit{state['commits']}"
                state["history"].append(state["head"])
                remember(state["head"])
                save()
            elif args[:2] == ["reset", "--hard"]:
                commit = resolve(args[2])
                if not known(commit):
                    sys.exit(1)
                if commit in state["history"]:
                    replacement_history = state["history"][: state["history"].index(commit) + 1]
                else:
                    replacement_history = ancestry_through(commit)
                state["head"] = commit
                state["history"] = replacement_history
                save()
            elif args[:2] == ["merge-base", "--is-ancestor"]:
                ancestor = resolve(args[2])
                descendant = resolve(args[3])
                sys.exit(0 if ancestor in ancestry_through(descendant) else 1)
            elif args[:2] == ["branch", "--show-current"]:
                print(state.get("branch", "main"))
            elif args[0] in {"branch", "tag", "checkout", "switch"}:
                pass
            else:
                pass
            save()
            """,
        )
        return script

    def init_workspace(self, name, head="good", healthy=True):
        workspace = self.root / name
        workspace.mkdir()
        (workspace / ".gitfake.json").write_text(
            json.dumps(
                {
                    "branch": "main",
                    "history": ["old", head],
                    "head": head,
                    "commits": 0,
                    "known_commits": ["old", head],
                }
            ),
            encoding="utf-8",
        )
        if healthy:
            (workspace / "healthy").write_text("1", encoding="utf-8")
        return workspace

    def write_config(self, profiles, llm_enabled=False, provider="codex", codex="codex", claude="claude", webhook_url=""):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        profile_blocks = []
        for profile in profiles:
            args = ", ".join(json.dumps(arg) for arg in profile.get("args", []))
            profile_blocks.append(
                f"""
                [[profiles]]
                id = "{profile['id']}"
                enabled = {str(profile.get('enabled', True)).lower()}
                target = "{profile['target']}"
                profile = "{profile.get('profile', profile['id'])}"
                workspace = "{profile['workspace']}"
                command = "{profile['command']}"
                args = [{args}]
                rollback_enabled = {str(profile.get('rollback_enabled', True)).lower()}
                llm_enabled = {str(profile.get('llm_enabled', False)).lower()}
                check_interval_seconds = {profile.get('check_interval_seconds', 1)}
                """
            )
        self.config.write_text(
            textwrap.dedent(
                f"""
                version = 1
                default_check_interval_seconds = 1
                max_repair_attempts = 1
                cooldown_seconds = 1
                state_dir = "{self.state_home / 'gateway-guardian'}"
                log_dir = "{self.state_home / 'gateway-guardian' / 'logs'}"

                [notifications.discord]
                webhook_url = {json.dumps(webhook_url)}

                [llm]
                enabled = {str(llm_enabled).lower()}
                provider = "{provider}"
                timeout_seconds = 5

                [llm.codex]
                command = "{codex}"
                model = ""
                prompt = "repair {{target}} {{profile}} in {{workspace}}"

                [llm.claude]
                command = "{claude}"
                model = ""
                prompt = "repair {{target}} {{profile}} in {{workspace}}"
                """
            ).lstrip()
            + "\n".join(textwrap.dedent(block) for block in profile_blocks),
            encoding="utf-8",
        )
        self.config.chmod(0o600)

    def run_worker_once(self, profile_id, check=True):
        env = self.env.copy()
        env["GATEWAY_GUARDIAN_ONCE"] = "1"
        return self.run_cli(
            self.worker_command_name(),
            "--config",
            str(self.config),
            "--profile-id",
            profile_id,
            check=check,
            env=env,
            timeout=15,
        )


class GatewayGuardianConfigTests(GatewayGuardianTestCase):
    def test_setup_writes_default_toml_config_with_secure_mode_and_default_interval(self):
        workspace = self.init_workspace("hermes-prod")
        hermes = self.write_fake_target("hermes")

        self.run_cli(
            "setup",
            "--target",
            "hermes",
            "--profile",
            "prod",
            "--workspace",
            str(workspace),
            "--cli",
            str(hermes),
        )

        self.assertTrue(self.config.exists())
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        config = self.load_config()
        self.assertEqual(config["default_check_interval_seconds"], 300)
        self.assertEqual(config["profiles"][0]["target"], "hermes")

    def test_config_set_updates_global_keys_and_rejects_unknown_or_invalid_values(self):
        workspace = self.init_workspace("hermes-prod")
        hermes = self.write_fake_target("hermes")
        self.run_cli(
            "setup",
            "--target",
            "hermes",
            "--profile",
            "prod",
            "--workspace",
            str(workspace),
            "--cli",
            str(hermes),
        )

        self.run_cli("config", "set", "default_check_interval_seconds=1", "llm.enabled=true")
        config = self.load_config()
        self.assertEqual(config["default_check_interval_seconds"], 1)
        self.assertTrue(config["llm"]["enabled"])

        unknown = self.run_cli("config", "set", "not_a_key=1", check=False)
        self.assertNotEqual(unknown.returncode, 0)
        invalid_number = self.run_cli("config", "set", "default_check_interval_seconds=0", check=False)
        self.assertNotEqual(invalid_number.returncode, 0)
        invalid_bool = self.run_cli("config", "set", "llm.enabled=maybe", check=False)
        self.assertNotEqual(invalid_bool.returncode, 0)

    def test_profile_add_set_and_list_cover_hermes_openclaw_duplicate_and_isolation(self):
        hermes_workspace = self.init_workspace("hermes-prod")
        openclaw_workspace = self.init_workspace("openclaw-stage")
        hermes = self.write_fake_target("hermes")
        openclaw = self.write_fake_target("openclaw")

        self.run_cli(
            "profile",
            "add",
            "--target",
            "hermes",
            "--profile",
            "prod",
            "--workspace",
            str(hermes_workspace),
            "--cli",
            str(hermes),
        )
        self.run_cli(
            "profile",
            "add",
            "--target",
            "openclaw",
            "--profile",
            "stage",
            "--workspace",
            str(openclaw_workspace),
            "--cli",
            str(openclaw),
        )
        duplicate = self.run_cli(
            "profile",
            "add",
            "--target",
            "hermes",
            "--profile",
            "prod",
            "--workspace",
            str(hermes_workspace),
            "--cli",
            str(hermes),
            check=False,
        )
        self.assertNotEqual(duplicate.returncode, 0)

        self.run_cli("profile", "set", "openclaw-stage", "enabled=false", "llm_enabled=true")
        config = self.load_config()
        profiles = {profile["id"]: profile for profile in config["profiles"]}
        self.assertTrue(profiles["hermes-prod"]["enabled"])
        self.assertFalse(profiles["openclaw-stage"]["enabled"])
        self.assertTrue(profiles["openclaw-stage"]["llm_enabled"])

        listed = self.run_cli("profile", "list")
        self.assertIn("hermes-prod", listed.stdout)
        self.assertIn("openclaw-stage", listed.stdout)
        self.assertIn("hermes", listed.stdout)
        self.assertIn("openclaw", listed.stdout)


class GatewayGuardianWorkerTests(GatewayGuardianTestCase):
    def test_worker_once_creates_isolated_state_dirs_logs_and_records_last_good_commits(self):
        self.write_fake_git()
        hermes = self.write_hermes_gateway_target("hermes")
        openclaw = self.write_fake_target("openclaw")
        hermes_workspace = self.init_workspace("hermes-prod", head="hermes-good")
        openclaw_workspace = self.init_workspace("openclaw-stage", head="openclaw-good")
        self.write_config(
            [
                {
                    "id": "hermes-prod",
                    "target": "hermes",
                    "profile": "prod",
                    "workspace": str(hermes_workspace),
                    "command": str(hermes),
                    "args": ["--profile", "prod"],
                },
                {
                    "id": "openclaw-stage",
                    "target": "openclaw",
                    "profile": "stage",
                    "workspace": str(openclaw_workspace),
                    "command": str(openclaw),
                    "args": ["--profile", "stage"],
                },
            ]
        )

        self.run_worker_once("hermes-prod")
        self.run_worker_once("openclaw-stage")

        hermes_state = self.state_home / "gateway-guardian" / "profiles" / "hermes-prod"
        openclaw_state = self.state_home / "gateway-guardian" / "profiles" / "openclaw-stage"
        self.assertTrue((hermes_state / "last-good-commit").exists())
        self.assertTrue((openclaw_state / "last-good-commit").exists())
        self.assertEqual((hermes_state / "last-good-commit").read_text(encoding="utf-8").strip(), "hermes-good")
        self.assertEqual((openclaw_state / "last-good-commit").read_text(encoding="utf-8").strip(), "openclaw-good")
        self.assertTrue((hermes_state / "repair.log").exists())
        self.assertTrue((openclaw_state / "repair.log").exists())
        self.assertNotEqual(hermes_state, openclaw_state)
        hermes_calls = (hermes_workspace / "hermes.argv").read_text(encoding="utf-8")
        openclaw_calls = (openclaw_workspace / "openclaw.argv").read_text(encoding="utf-8")
        self.assertIn("--profile prod gateway status --deep", hermes_calls)
        self.assertIn("--profile stage status", openclaw_calls)
        self.assertNotIn("--profile stage gateway status --deep", openclaw_calls)

    def test_hermes_stale_service_status_triggers_gateway_start_repair(self):
        self.write_fake_git()
        hermes = self.write_hermes_gateway_target("hermes")
        workspace = self.init_workspace("hermes-prod", head="good", healthy=False)
        (workspace / "stale_service").write_text("1", encoding="utf-8")
        self.write_config(
            [
                {
                    "id": "hermes-prod",
                    "target": "hermes",
                    "profile": "prod",
                    "workspace": str(workspace),
                    "command": str(hermes),
                    "args": ["--profile", "prod"],
                    "rollback_enabled": False,
                }
            ]
        )

        self.run_worker_once("hermes-prod")

        calls = (workspace / "hermes.argv").read_text(encoding="utf-8")
        self.assertIn("--profile prod gateway status --deep", calls)
        self.assertIn("--profile prod doctor --fix", calls)
        self.assertIn("--profile prod gateway start", calls)
        self.assertTrue((workspace / "healthy").exists())
        state = self.state_home / "gateway-guardian" / "profiles" / "hermes-prod"
        self.assertEqual((state / "notification-status").read_text(encoding="utf-8").strip(), "healthy")

    def test_hermes_agent_secrets_failure_is_unhealthy_even_with_zero_exit(self):
        self.write_fake_git()
        hermes = self.write_hermes_gateway_target("hermes")
        workspace = self.init_workspace("hermes-prod", head="good", healthy=False)
        (workspace / "secrets_failed").write_text("1", encoding="utf-8")
        self.write_config(
            [
                {
                    "id": "hermes-prod",
                    "target": "hermes",
                    "profile": "prod",
                    "workspace": str(workspace),
                    "command": str(hermes),
                    "args": ["--profile", "prod"],
                    "rollback_enabled": False,
                }
            ]
        )

        self.run_worker_once("hermes-prod")

        state = self.state_home / "gateway-guardian" / "profiles" / "hermes-prod"
        log = (state / "repair.log").read_text(encoding="utf-8")
        calls = (workspace / "hermes.argv").read_text(encoding="utf-8")
        self.assertIn("agent-secrets exited 1", log)
        self.assertIn("hermes gateway health failed: agent-secrets exited ", log)
        self.assertIn("--profile prod gateway start", calls)

    def test_daily_backup_marker_is_not_updated_after_failed_commit(self):
        self.write_fake_git()
        hermes = self.write_fake_target("hermes")
        workspace = self.init_workspace("hermes-prod", head="good")
        (workspace / ".dirty").write_text("1", encoding="utf-8")
        (workspace / ".fail_commit").write_text("1", encoding="utf-8")
        self.write_config(
            [
                {
                    "id": "hermes-prod",
                    "target": "hermes",
                    "workspace": str(workspace),
                    "command": str(hermes),
                }
            ]
        )

        self.run_worker_once("hermes-prod", check=False)

        state = self.state_home / "gateway-guardian" / "profiles" / "hermes-prod"
        self.assertFalse((state / "last-backup-date").exists())

    def test_rollback_uses_recorded_last_good_commit(self):
        self.write_fake_git()
        hermes = self.write_fake_target("hermes")
        workspace = self.init_workspace("hermes-prod", head="bad", healthy=False)
        (workspace / ".gitfake.json").write_text(
            json.dumps({"branch": "main", "history": ["old", "recorded-good", "bad"], "head": "bad", "commits": 0}),
            encoding="utf-8",
        )
        self.write_config(
            [
                {
                    "id": "hermes-prod",
                    "target": "hermes",
                    "workspace": str(workspace),
                    "command": str(hermes),
                }
            ]
        )
        state = self.state_home / "gateway-guardian" / "profiles" / "hermes-prod"
        state.mkdir(parents=True)
        (state / "last-good-commit").write_text("recorded-good\n", encoding="utf-8")

        self.run_worker_once("hermes-prod", check=False)

        calls = (workspace / ".gitfake.calls").read_text(encoding="utf-8").splitlines()
        reset_calls = [json.loads(line) for line in calls if json.loads(line)[:2] == ["reset", "--hard"]]
        self.assertIn(["reset", "--hard", "recorded-good"], reset_calls)
        self.assertNotIn(["reset", "--hard", "old"], reset_calls)


class GatewayGuardianNotificationTests(GatewayGuardianTestCase):
    def test_discord_webhook_reports_unhealthy_and_recovery_once_per_incident(self):
        self.write_fake_git()
        server, requests, webhook_url = start_webhook_server()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        hermes = self.write_doctor_repair_target("hermes")
        workspace = self.init_workspace("hermes-prod", head="good", healthy=False)
        self.write_config(
            [
                {
                    "id": "hermes-prod",
                    "target": "hermes",
                    "profile": "prod",
                    "workspace": str(workspace),
                    "command": str(hermes),
                }
            ],
            webhook_url=webhook_url,
        )

        self.run_worker_once("hermes-prod")

        payloads = [json.loads(request["body"].decode("utf-8")) for request in requests]
        titles = [payload["embeds"][0]["title"] for payload in payloads]
        self.assertEqual(
            titles,
            [
                "Gateway Guardian detected an unhealthy profile",
                "Gateway Guardian recovered a profile",
            ],
        )
        self.assertEqual(payloads[0]["embeds"][0]["fields"][0]["value"], "hermes-prod")
        self.assertIn("doctor repair", payloads[1]["embeds"][0]["description"])
        state = self.state_home / "gateway-guardian" / "profiles" / "hermes-prod"
        self.assertEqual((state / "notification-status").read_text(encoding="utf-8").strip(), "healthy")

        self.run_worker_once("hermes-prod")
        self.assertEqual(len(requests), 2)

    def test_discord_webhook_reports_unrepaired_failure_without_repeating_alerts(self):
        self.write_fake_git()
        server, requests, webhook_url = start_webhook_server()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        hermes = self.write_fake_target("hermes")
        workspace = self.init_workspace("hermes-prod", head="bad", healthy=False)
        self.write_config(
            [
                {
                    "id": "hermes-prod",
                    "target": "hermes",
                    "profile": "prod",
                    "workspace": str(workspace),
                    "command": str(hermes),
                    "rollback_enabled": False,
                }
            ],
            webhook_url=webhook_url,
        )

        self.run_worker_once("hermes-prod", check=False)

        payloads = [json.loads(request["body"].decode("utf-8")) for request in requests]
        titles = [payload["embeds"][0]["title"] for payload in payloads]
        self.assertEqual(
            titles,
            [
                "Gateway Guardian detected an unhealthy profile",
                "Gateway Guardian could not repair a profile",
            ],
        )
        state = self.state_home / "gateway-guardian" / "profiles" / "hermes-prod"
        self.assertEqual((state / "notification-status").read_text(encoding="utf-8").strip(), "failed")

        self.run_worker_once("hermes-prod", check=False)
        self.assertEqual(len(requests), 2)


class GatewayGuardianLlmTests(GatewayGuardianTestCase):
    def write_fake_llm(self, name, rewrite=False):
        script = self.bin / name
        rewrite_code = (
            """
            state_path = os.path.join(os.getcwd(), ".gitfake.json")
            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            state["head"] = "rewritten"
            state["history"] = ["rewritten"]
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            """
            if rewrite
            else ""
        )
        write_executable(
            script,
            f"""\
            #!/usr/bin/env python3
            import json, os, sys
            with open(os.path.join(os.getcwd(), "{name}.capture.json"), "w", encoding="utf-8") as fh:
                json.dump({{"argv": sys.argv, "cwd": os.getcwd()}}, fh)
            with open(os.path.join(os.getcwd(), "healthy"), "w", encoding="utf-8") as fh:
                fh.write("1")
            {textwrap.indent(textwrap.dedent(rewrite_code), " " * 12).strip()}
            sys.exit(0)
            """,
        )
        return script

    def test_llm_runs_only_when_global_and_profile_flags_enabled_and_builds_codex_and_claude_commands(self):
        self.write_fake_git()
        target = self.write_fake_target("hermes")
        codex = self.write_fake_llm("codex")
        claude = self.write_fake_llm("claude")
        codex_workspace = self.init_workspace("codex-workspace", head="bad", healthy=False)
        claude_workspace = self.init_workspace("claude-workspace", head="bad", healthy=False)

        self.write_config(
            [
                {
                    "id": "codex-profile",
                    "target": "hermes",
                    "workspace": str(codex_workspace),
                    "command": str(target),
                    "llm_enabled": False,
                }
            ],
            llm_enabled=True,
            provider="codex",
            codex=str(codex),
            claude=str(claude),
        )
        state = self.state_home / "gateway-guardian" / "profiles" / "codex-profile"
        state.mkdir(parents=True)
        (state / "last-good-commit").write_text("bad\n", encoding="utf-8")
        self.run_worker_once("codex-profile", check=False)
        self.assertFalse((codex_workspace / "codex.capture.json").exists())

        self.write_config(
            [
                {
                    "id": "codex-profile",
                    "target": "hermes",
                    "workspace": str(codex_workspace),
                    "command": str(target),
                    "llm_enabled": True,
                }
            ],
            llm_enabled=True,
            provider="codex",
            codex=str(codex),
            claude=str(claude),
        )
        self.run_worker_once("codex-profile", check=False)
        codex_capture = json.loads((codex_workspace / "codex.capture.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(codex_capture["cwd"]).resolve(), codex_workspace.resolve())
        self.assertIn("exec", codex_capture["argv"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex_capture["argv"])
        self.assertIn("-C", codex_capture["argv"])
        codex_cd = codex_capture["argv"][codex_capture["argv"].index("-C") + 1]
        self.assertEqual(Path(codex_cd).resolve(), codex_workspace.resolve())

        self.write_config(
            [
                {
                    "id": "claude-profile",
                    "target": "hermes",
                    "workspace": str(claude_workspace),
                    "command": str(target),
                    "llm_enabled": True,
                }
            ],
            llm_enabled=True,
            provider="claude",
            codex=str(codex),
            claude=str(claude),
        )
        state = self.state_home / "gateway-guardian" / "profiles" / "claude-profile"
        state.mkdir(parents=True, exist_ok=True)
        (state / "last-good-commit").write_text("bad\n", encoding="utf-8")
        self.run_worker_once("claude-profile", check=False)
        claude_capture = json.loads((claude_workspace / "claude.capture.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(claude_capture["cwd"]).resolve(), claude_workspace.resolve())
        self.assertIn("-p", claude_capture["argv"])
        self.assertIn("--dangerously-skip-permissions", claude_capture["argv"])

    def test_llm_history_rewrite_is_rejected_and_reset_to_pre_llm_commit(self):
        self.write_fake_git()
        target = self.write_fake_target("hermes")
        codex = self.write_fake_llm("codex", rewrite=True)
        workspace = self.init_workspace("rewrite-workspace", head="bad", healthy=False)
        self.write_config(
            [
                {
                    "id": "rewrite-profile",
                    "target": "hermes",
                    "workspace": str(workspace),
                    "command": str(target),
                    "llm_enabled": True,
                }
            ],
            llm_enabled=True,
            provider="codex",
            codex=str(codex),
        )
        state = self.state_home / "gateway-guardian" / "profiles" / "rewrite-profile"
        state.mkdir(parents=True, exist_ok=True)
        (state / "last-good-commit").write_text("bad\n", encoding="utf-8")

        proc = self.run_worker_once("rewrite-profile", check=False)

        checkpoint = json.loads((state / "llm-checkpoint.json").read_text(encoding="utf-8"))
        git_state = json.loads((workspace / ".gitfake.json").read_text(encoding="utf-8"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(git_state["head"], checkpoint["commit"])
        self.assertIn(checkpoint["commit"], git_state["history"])
        self.assertNotEqual(git_state["history"], ["rewritten"])
        self.assertNotIn("rewritten", git_state["history"])


if __name__ == "__main__":
    unittest.main()

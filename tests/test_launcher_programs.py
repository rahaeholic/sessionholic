from pathlib import Path
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher_programs as lp  # noqa: E402

LAUNCHER = ["/bin/zsh", "-lic", "zz"]
FIRST, SECOND = "a" * 32, "b" * 32


def started(text):
    return int(time.mktime(time.strptime(text, lp.LSTART)))


PS = """  501   501     1 Mon Oct 12 15:16:50 2026     /usr/sbin/sshd
  501 57597   501 Mon Oct 12 15:16:52 2026     /bin/zsh -lic zz
  501 57608 57597 Mon Oct 12 15:16:59 2026     /bin/zsh -c _zz_run
  501 57623 57608 Mon Oct 12 15:17:00 2026     /Users/example/.local/bin/claude agents --dangerously-skip-permissions --cwd /Users/example/My Project --mcp-config /x.json
  501 57635 57623 Mon Oct 12 15:17:00 2026     /Users/example/.local/bin/claude daemon run --origin transient
  501 67233   501 Mon Oct 12 17:14:22 2026     /bin/zsh -lic zz
  501 67255 67233 Mon Oct 12 17:14:29 2026     node /opt/homebrew/bin/codex --cd /Users/example/other
  501 67260 67255 Mon Oct 12 17:14:29 2026     /opt/homebrew/lib/node_modules/@openai/codex/vendor/codex --cd /Users/example/other
  501 70000   501 Mon Oct 12 17:30:00 2026     /bin/zsh -lic zz
  501 70001   501 Mon Oct 12 17:30:02 2026     /bin/zsh -lic zz
  502 80000     1 Mon Oct 12 18:00:00 2026     /bin/zsh -lic zz
  502 80001 80000 Mon Oct 12 18:00:01 2026     claude agents --cwd /Users/other/secret
  501 90000   501 Mon Oct 12 19:00:00 2026     /bin/zsh /Users/example/bin/zz-launcher
  501 90001 90000 Mon Oct 12 19:00:03 2026     claude
not a process line
"""


class TableTest(unittest.TestCase):
    def test_ps_rows_are_parsed_in_the_c_locale_and_bad_lines_are_skipped(self):
        commands = []
        rows = lp.table(lambda command: commands.append(command) or PS, uid=501)
        self.assertEqual(commands, [[lp.PS, "-axww", "-o", "uid=,pid=,ppid=,lstart=,command="]])
        self.assertEqual(len(rows), 12, "another user's processes are never read")
        self.assertEqual(rows[1], (57597, 501, started("Mon Oct 12 15:16:52 2026"), "/bin/zsh -lic zz"))


class ExactCodexTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sh-link-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.native = {"agent": "codex", "home": ".codex-work", "cwd": "/work/project",
                       "id": "019a0000-0000-7000-8000-000000000001"}

    def rollout(self, identifier=None, source="vscode", key=".codex-work"):
        identifier = identifier or self.native["id"]
        path = self.home / key / "sessions/2026/10/21" / ("rollout-now-" + identifier + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": identifier, "cwd": self.native["cwd"], "source": source}}) + "\n")
        return path

    def read(self, files):
        output = "p20\n" + "".join("f%d\na%s\nn%s\n" % (i, access, path)
                                     for i, (access, path) in enumerate(files))
        with patch.object(lp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)):
            return lp.writable_codex_source({20}, home=self.home)

    def test_writable_original_is_unique_even_when_subagent_and_read_only_records_are_open(self):
        root = self.rollout()
        child = self.rollout("019a0000-0000-7000-8000-000000000002", {"subagent": {"thread_spawn": {}}})
        other = self.rollout("019a0000-0000-7000-8000-000000000003")
        self.assertEqual(self.read([("u", child), ("r", other), ("u", root), ("w", root)]), self.native)
        self.assertIsNone(self.read([("u", root), ("u", other)]), "two original writers prove no single conversation")

    def test_invalid_paths_metadata_and_failed_reads_never_confirm_a_link(self):
        original = self.rollout()
        original.unlink()
        target = self.rollout(key="other")
        original.symlink_to(target)
        self.assertIsNone(self.read([("u", original), ("u", target)]))
        original.unlink()
        for source in ("exec", {"subagent": {}}, None):
            self.assertIsNone(self.read([("u", self.rollout(source=source))]))
        wrong = self.rollout()
        wrong.write_text(json.dumps({"type": "session_meta", "payload": {
            **self.native, "id": "019a0000-0000-7000-8000-000000000009", "source": "cli"}}))
        self.assertIsNone(self.read([("u", wrong)]))
        wrong.write_text("x" * 65537)
        self.assertIsNone(self.read([("u", wrong)]))

    def test_hold_pane_owns_the_cli_and_embedded_server_without_following_tools_or_daemon(self):
        rows = [(10, 1, 1, "zsh"), (20, 10, 2, "node /bin/codex"),
                (21, 20, 2, "/vendor/codex"), (22, 21, 2, "/vendor/codex app-server"),
                (23, 21, 2, "/vendor/codex app-server daemon"), (30, 21, 3, "bash -c tool"),
                (31, 30, 3, "/vendor/codex"), (40, 1, 2, "/vendor/codex")]
        done = subprocess.CompletedProcess([], 0, "10\n")
        with patch("collector.which", return_value="/fixture/tmux"), patch.object(lp.subprocess, "run", return_value=done) as run, \
                patch.object(lp, "writable_codex_source", return_value=self.native) as read:
            self.assertEqual(lp.held_codex("sh-0123456789abcdef", rows),
                             {"program": "codex", "pid": 20, "cwd": "/work/project", "nativeSource": self.native})
            read.assert_called_once_with({20, 21, 22})
            self.assertIn("list-panes", run.call_args.args[0])
            run.return_value = subprocess.CompletedProcess([], 0, "10\n40\n")
            self.assertIsNone(lp.held_codex("sh-0123456789abcdef", rows))
            run.side_effect = subprocess.TimeoutExpired("tmux", 3)
            self.assertIsNone(lp.held_codex("sh-0123456789abcdef", rows))
            self.assertIsNone(lp.held_codex("../other", rows))

    def test_exact_hold_evidence_overrides_only_the_program_time_description(self):
        terminal = {"id": FIRST, "createdAt": 1, "holdName": "sh-0123456789abcdef"}
        evidence = {"program": "codex", "pid": 20, "cwd": "/work/project", "nativeSource": self.native}
        with patch("launch.new_session", return_value={"argv": LAUNCHER}), \
                patch.object(lp, "table", return_value=[]), patch.object(lp, "held_codex", return_value=evidence):
            self.assertEqual(lp.launcher_programs({"terminals": [terminal]})["terminals"][FIRST], evidence)


class ProgramTest(unittest.TestCase):
    def test_agent_clis_are_named_and_claude_helpers_are_not(self):
        cases = {
            "/Users/example/.local/bin/claude agents --cwd /Users/example/project": {"program": "claude-agents", "cwd": "/Users/example/project"},
            "/Users/example/.local/bin/claude agents --cwd=relative": {"program": "claude-agents", "cwd": None},
            "claude": {"program": "claude", "cwd": None},
            "/x/claude daemon run": None, "claude bg-pty-host --bg-pty-host x": None, "claude bg-spare": None,
            "node /opt/homebrew/bin/codex resume 019a": {"program": "codex", "cwd": None},
            "/opt/homebrew/bin/codex app-server --listen unix://": None,
            "/bin/zsh -lic zz": None, "": None,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(lp.program_of(command), expected)

    def test_each_terminal_names_its_own_launcher_and_the_first_cli_below_it(self):
        rows = lp.table(lambda command: PS, uid=501)
        terminals = [{"id": FIRST, "createdAt": started("Mon Oct 12 15:16:51 2026")},
                     {"id": SECOND, "createdAt": started("Mon Oct 12 17:14:21 2026")},
                     {"id": "c" * 32, "createdAt": started("Mon Oct 12 17:30:00 2026")},
                     {"id": "d" * 32, "createdAt": started("Mon Oct 12 09:00:00 2026")},
                     {"id": "f" * 32, "createdAt": started("Mon Oct 12 18:00:00 2026")},
                     {"id": "../x", "createdAt": 1}, {"id": "e" * 32, "createdAt": "soon"}]
        result = lp.describe(terminals, LAUNCHER, rows)
        self.assertEqual(result[FIRST], {"program": "claude-agents", "cwd": "/Users/example/My Project", "pid": 57623})
        self.assertEqual(result[SECOND], {"program": "codex", "cwd": None, "pid": 67255})
        # Two launchers in the same window, or none: no guess.
        self.assertEqual(result["c" * 32], {"program": None})
        self.assertEqual(result["d" * 32], {"program": None})
        self.assertEqual(result["f" * 32], {"program": None}, "another user's launcher never names this terminal")
        self.assertEqual(set(result), {FIRST, SECOND, "c" * 32, "d" * 32, "f" * 32})
        # Only behind an interpreter: another command ending in the launcher path never matches.
        other = lp.describe([{"id": "a" * 32, "createdAt": started("Mon Oct 12 19:00:00 2026")}], ["bin/zz-launcher"],
                            [(1, 0, started("Mon Oct 12 19:00:00 2026"), "vim /Users/example/bin/zz-launcher"),
                             (2, 1, started("Mon Oct 12 19:00:01 2026"), "claude")])
        self.assertEqual(other["a" * 32], {"program": None})
        self.assertEqual(lp.program_of("claude agents --cwd /Users/example/p -- extra"), {"program": "claude-agents", "cwd": "/Users/example/p"})
        # A script launcher appears behind its interpreter.
        script = lp.describe([{"id": "a" * 32, "createdAt": started("Mon Oct 12 19:00:00 2026")}], ["/Users/example/bin/zz-launcher"], rows)
        self.assertEqual(script["a" * 32], {"program": "claude", "cwd": None, "pid": 90001})

    def test_terminals_opened_close_together_never_share_a_launcher(self):
        opened = started("Mon Oct 12 15:16:51 2026")
        first, second = {"id": FIRST, "createdAt": opened}, {"id": SECOND, "createdAt": opened + 10}
        launcher = lambda pid, after: (pid, 1, opened + after, "/bin/zsh -lic zz")
        program = lambda pid, parent, after: (pid, parent, opened + after, "claude")
        # Each launcher started after its own terminal: both are named, though the windows overlap.
        rows = [launcher(10, 1), program(11, 10, 2), launcher(20, 11), program(21, 20, 12)]
        self.assertEqual(lp.describe([first, second], LAUNCHER, rows),
                         {FIRST: {"program": "claude", "cwd": None, "pid": 11}, SECOND: {"program": "claude", "cwd": None, "pid": 21}})
        # A launcher slow to start (SSH) still belongs to the terminal opened first.
        late = [launcher(10, 8), program(11, 10, 9), launcher(20, 16), program(21, 20, 17)]
        self.assertEqual(lp.describe([first, second], LAUNCHER, late),
                         {FIRST: {"program": "claude", "cwd": None, "pid": 11}, SECOND: {"program": "claude", "cwd": None, "pid": 21}})
        # Two launchers that started in the other order (SSH delays) pair up two ways: neither is named.
        swapped = [launcher(10, 10), program(11, 10, 11), launcher(20, 6), program(21, 20, 7)]
        self.assertEqual(lp.describe([first, {**second, "createdAt": opened + 5}], LAUNCHER, swapped),
                         {FIRST: {"program": None}, SECOND: {"program": None}})
        # The first one's launcher is gone: the one left could be either's, so it names neither.
        self.assertEqual(lp.describe([first, second], LAUNCHER, rows[2:]),
                         {FIRST: {"program": None}, SECOND: {"program": None}})
        # A launcher only one terminal could have opened still names it.
        self.assertEqual(lp.describe([first, {**second, "createdAt": opened + 30}], LAUNCHER,
                                     [launcher(20, 31), program(21, 20, 32)]),
                         {FIRST: {"program": None}, SECOND: {"program": "claude", "cwd": None, "pid": 21}})
        # Two terminals opened in the same second: their launchers name neither.
        self.assertEqual(lp.describe([first, {**second, "createdAt": opened}], LAUNCHER, rows[2:]),
                         {FIRST: {"program": None}, SECOND: {"program": None}})

    def test_a_launcher_at_its_menu_and_one_running_ssh_are_told_apart(self):
        opened = started("Mon Oct 12 15:16:51 2026")
        terminal = {"id": FIRST, "createdAt": opened}
        launcher = (10, 1, opened + 1, "/bin/zsh -lic zz")
        # Nothing chosen yet: only the launcher's own interpreters below it.
        menu = [launcher, (11, 10, opened + 1, "/usr/bin/python3 /Users/example/.config/zz/ag.py")]
        self.assertEqual(lp.describe([terminal], LAUNCHER, menu), {FIRST: {"program": None, "launcher": True}})
        # A PC session the launcher opened: SSH is work of its own, never a menu.
        remote = menu + [(12, 11, opened + 9, "ssh -t -o BatchMode=yes beaulink powershell")]
        self.assertEqual(lp.describe([terminal], LAUNCHER, remote), {FIRST: {"program": "ssh", "cwd": None, "pid": 12}})
        self.assertEqual(lp.program_of("/usr/bin/ssh -t host"), {"program": "ssh", "cwd": None})
        # No launcher named: no claim either way.
        self.assertEqual(lp.describe([terminal], LAUNCHER, []), {FIRST: {"program": None}})

    def test_a_host_without_a_launcher_reports_nothing(self):
        with patch("launch.new_session", side_effect=ValueError("no launcher")):
            self.assertEqual(lp.launcher_programs({"terminals": []}), {"terminals": {}})
        with patch("launch.new_session", return_value={"argv": LAUNCHER}), self.assertRaises(ValueError):
            lp.launcher_programs({"terminals": "x"})


class WorkflowTest(unittest.TestCase):
    def test_host_owned_exact_evidence_is_recorded_and_unconfirmed_holds_are_read_after_a_minute(self):
        import server
        from unittest.mock import Mock
        native = {"agent": "codex", "home": ".codex-work", "cwd": "/work/project",
                  "id": "019a0000-0000-7000-8000-000000000001", "host": "untrusted-helper-host"}
        row = {"id": FIRST, "mode": "new-session", "alive": True, "host": "laptop", "createdAt": 100,
               "holdName": "sh-0123456789abcdef", "program": "codex", "programAt": int(time.time()) - 65,
               "linkConfidence": "estimated"}
        board = Mock(hosts=[{"name": "laptop"}], state={"laptop": {"ok": True}})
        terminals = Mock()
        terminals.list.return_value = [row]
        evidence = {"program": "codex", "cwd": "/work/project", "nativeSource": native}
        with tempfile.TemporaryDirectory() as directory:
            flow = server.Workflow(board, terminals, directory)
            with patch.object(flow.transfers, "rpc", return_value={"terminals": {FIRST: evidence}}):
                rows = [{"id": FIRST, "createdAt": 100, "holdName": row["holdName"]}]
                flow._describe_host(board.hosts[0], rows)
            terminals.link_native.assert_called_once_with(FIRST, {**native, "host": "laptop"}, confidence="exact")
            with patch.object(flow, "_describe_host") as describe:
                flow.describe_launchers()
                for _ in range(50):
                    if describe.called:
                        break
                    time.sleep(0.02)
                describe.assert_called_once_with(board.hosts[0], rows, {FIRST})

    def test_reads_run_in_the_background_once_a_minute_per_host_and_save_the_program(self):
        import server

        class Terminals:
            def __init__(self):
                self.saved, self.rows = [], [
                    {"id": FIRST, "mode": "new-session", "alive": True, "host": "laptop", "createdAt": 100},
                    {"id": SECOND, "mode": "attach", "alive": True, "host": "laptop", "createdAt": 100},
                    {"id": "c" * 32, "mode": "new-session", "alive": True, "host": "pc", "createdAt": 100},
                ]

            def list(self):
                return self.rows

            def set_program(self, identifier, info):
                self.saved.append((identifier, info))
                return True

        class Board:
            hosts = [{"name": "laptop", "label": "노트북"}, {"name": "pc", "label": "PC", "platform": "windows"}]
            state = {"laptop": {"ok": True}, "pc": {"ok": True}}

        with tempfile.TemporaryDirectory() as directory:
            terminals = Terminals()
            flow = server.Workflow(Board(), terminals, directory)
            done = threading.Event()
            calls = []

            def rpc(host, op, args, timeout):
                calls.append((host["name"], op, args))
                done.set()
                return {"terminals": {FIRST: {"program": "claude-agents", "cwd": "/p"}, "f" * 32: {"program": "codex"}}}
            with patch.object(flow.transfers, "rpc", side_effect=rpc):
                flow.describe_launchers()
                self.assertTrue(done.wait(3))
                for _ in range(50):
                    if terminals.saved:
                        break
                    time.sleep(0.02)
                flow.describe_launchers()
        self.assertEqual(calls, [("laptop", "launcher_programs", {"terminals": [{"id": FIRST, "createdAt": 100}]})])
        self.assertEqual(terminals.saved, [(FIRST, {"program": "claude-agents", "cwd": "/p"})])


if __name__ == "__main__":
    unittest.main()

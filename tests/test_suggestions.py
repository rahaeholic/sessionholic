import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import settings  # noqa: E402
import suggestions  # noqa: E402

SOURCE = {"host": "air15", "agent": "codex", "home": ".codex", "id": "019a0000-0000-7000-8000-000000000001"}


class SuggestionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="sh-suggest-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "suggestions.json"
        self.prompts = []

    def make(self, reply='["네, 진행해 주세요.", "먼저 보여 주세요.", "테스트도 돌려 주세요."]', enabled=True):
        def runner(prompt):
            self.prompts.append(prompt)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return suggestions.Suggestions(self.path, enabled=enabled, runner=runner)

    def ask(self, board, answer="고쳤습니다. 배포할까요?"):
        result = board.request({"source": SOURCE, "question": "고쳐 줘", "answer": answer})
        for thread in threading.enumerate():
            if thread.name == "suggested-replies":
                thread.join(5)
        return result

    def test_off_answers_off_and_never_runs_the_model(self):
        board = self.make(enabled=False)
        self.assertEqual(self.ask(board), {"status": "off", "replies": []})
        self.assertEqual(self.prompts, [])

    def test_one_model_call_per_answer_kept_across_restarts(self):
        board = self.make()
        self.assertEqual(self.ask(board)["status"], "pending")
        ready = self.ask(board)
        self.assertEqual(ready, {"status": "ready", "replies": ["네, 진행해 주세요.", "먼저 보여 주세요.", "테스트도 돌려 주세요."]})
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("고쳤습니다. 배포할까요?", self.prompts[0])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        again = self.make()
        self.assertEqual(self.ask(again), ready)
        self.assertEqual(len(self.prompts), 1, "a restarted board keeps the replies")
        # Another answer is asked once more.
        self.assertEqual(self.ask(again, "다른 답변")["status"], "pending")
        self.assertEqual(len(self.prompts), 2)

    def test_a_failure_is_kept_and_never_retried(self):
        board = self.make(reply=RuntimeError("claude exited 1"))
        self.ask(board)
        self.assertEqual(self.ask(board), {"status": "failed", "replies": []})
        self.assertEqual(len(self.prompts), 1)

    def test_at_most_two_model_runs_at_once_and_half_an_emoji_does_not_break_a_request(self):
        release, running, peak, lock = threading.Event(), [0], [0], threading.Lock()

        def runner(prompt):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            release.wait(5)
            with lock:
                running[0] -= 1
            return '["네"]'
        board = suggestions.Suggestions(self.path, enabled=True, runner=runner)
        for index in range(4):
            board.request({"source": SOURCE, "question": "\ud83d질문", "answer": "답변 %d" % index})
        time.sleep(0.3)
        self.assertEqual(peak[0], suggestions.AT_ONCE)
        release.set()
        for thread in threading.enumerate():
            if thread.name == "suggested-replies":
                thread.join(5)
        self.assertEqual(board.request({"source": SOURCE, "question": "\ud83d질문", "answer": "답변 0"})["status"], "ready")

    def test_bad_requests_are_refused(self):
        board = self.make()
        for data in (None, {"source": {**SOURCE, "agent": "bash"}, "answer": "a"},
                     {"source": {**SOURCE, "host": "a b"}, "answer": "a"}, {"source": SOURCE, "answer": "  "},
                     {"source": SOURCE, "answer": 3}):
            with self.assertRaises(ValueError):
                board.request(data)
        self.assertEqual(self.prompts, [])

    def test_only_the_end_of_long_texts_is_sent(self):
        board = self.make()
        self.ask(board, "가" * 9000 + "마지막 질문인가요?")
        self.assertIn("마지막 질문인가요?", self.prompts[0])
        self.assertLess(len(self.prompts[0]), suggestions.MAX_ANSWER + suggestions.MAX_QUESTION + 1000)

    def test_replies_are_parsed_trimmed_and_capped(self):
        self.assertEqual(suggestions.parse('네\n["  하나 ", "둘", "둘", 3, "", "셋", "넷"] 끝'), ["하나", "둘", "셋"])
        self.assertEqual(suggestions.parse("배열이 없음"), [])
        self.assertEqual(suggestions.parse('[깨진 json'), [])
        # Sent exactly as shown: a long reply stays whole, one too long to be a reply is left out (never cut).
        long_reply = "배포 전에 변경 파일 목록과 테스트 결과, 그리고 운영 영향 범위를 먼저 정리해서 보여 줘"
        self.assertEqual(suggestions.parse(json.dumps([long_reply, "짧게"])), [long_reply, "짧게"])
        self.assertEqual(suggestions.parse(json.dumps(["가" * 100, "나" * (suggestions.MAX_REPLY + 1)])), ["가" * 100])

    def test_the_cli_runs_without_settings_hooks_tools_or_a_saved_session(self):
        home = Path(self.temp.name) / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}}))
        (home / ".local/bin").mkdir(parents=True)
        binary = home / ".local/bin/claude"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        done = subprocess.CompletedProcess([], 0, '["a"]', "")
        with patch("subprocess.run", return_value=done) as run:
            self.assertEqual(suggestions.run_claude("질문", home=home), '["a"]')
        args, kwargs = run.call_args
        self.assertEqual(args[0][:3], [str(binary), "-p", "질문"])
        for flag in (["--model", "haiku"], ["--tools", ""], ["--setting-sources", ""]):
            index = args[0].index(flag[0])
            self.assertEqual(args[0][index:index + 2], flag)
        self.assertIn("--no-session-persistence", args[0])
        self.assertEqual(kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "tok")
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
        self.assertEqual(kwargs["cwd"], str(home / ".local/state/session-board-suggestions"))
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "Not logged in")):
            with self.assertRaises(RuntimeError):
                suggestions.run_claude("질문", home=home)


class SettingTest(unittest.TestCase):
    def test_off_unless_the_host_settings_turn_it_on(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="sh-suggest-settings-") as folder:
            path = Path(folder) / "config.json"
            base = {"version": 2, "profiles": [], "projects": [],
                    "environments": {"default": {"label": "기본 환경", "variables": {}}}}
            for value, expected in ((None, False), (True, True), (False, False)):
                data = dict(base, **({} if value is None else {"suggestedReplies": value}))
                path.write_text(json.dumps(data))
                os.chmod(path, 0o600)
                self.assertIs(settings.load(home=folder, path=path)["suggestedReplies"], expected)
            path.write_text(json.dumps(dict(base, suggestedReplies="yes")))
            with self.assertRaises(ValueError):
                settings.load(home=folder, path=path)


if __name__ == "__main__":
    unittest.main()

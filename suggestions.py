"""Suggested next replies to a conversation's last answer: the board's one model call (opt-in).

With `"suggestedReplies": true` in the host settings the board asks Claude Haiku, through this
host's own `claude` CLI, for three short replies the user might send next. The CLI runs with no
settings, hooks, tools or saved session (so the call never shows up as a conversation), once per
answer: replies, and a failed attempt, are kept by the answer's digest and never asked again.
The page sends the texts it already shows; nothing is asked while an agent works, for a bot's
conversation or for a one-shot run (the page decides that before asking).
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

COUNT = 3
MAX_REPLY = 40
MAX_QUESTION = 2000
MAX_ANSWER = 6000
CACHE_LIMIT = 400
TIMEOUT = 45
AGENTS = ("codex", "claude")
PROMPT = """아래는 사용자와 AI 코딩 에이전트의 대화 마지막 부분입니다.
사용자가 이 답변에 이어서 보낼 법한 짧은 답장 {count}개를 제안하세요.
- 한국어, 각 20자 안팎의 한 문장, 서로 다른 방향(진행·확인·다른 요청 등)
- 답변이 질문으로 끝나면 그 질문에 대한 답을 우선
- 거절만 하는 답이나 인사는 넣지 않음
- 출력은 JSON 문자열 배열 하나만. 다른 글자 없이.

[사용자의 마지막 질문]
{question}

[에이전트의 답변]
{answer}
"""


def _token(home):
    """This host's long-lived Claude Code token from its settings (passed on, never logged)."""
    try:
        data = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    value = (data.get("env") or {}).get("CLAUDE_CODE_OAUTH_TOKEN") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def run_claude(prompt, home=None):
    """Haiku's reply text for `prompt` from the local CLI, with no settings, hooks or tools."""
    home = Path(home) if home is not None else Path.home()
    search = os.pathsep.join([str(home / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])
    binary = shutil.which("claude", path=search)
    if binary is None:
        raise RuntimeError("claude CLI not found")
    env = {"HOME": str(home), "PATH": search, "LANG": "en_US.UTF-8"}
    token = _token(home)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    workdir = home / ".local/state/session-board-suggestions"
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    done = subprocess.run([binary, "-p", prompt, "--model", "haiku", "--no-session-persistence", "--tools", "",
                           "--setting-sources", "", "--output-format", "text"],
                          cwd=str(workdir), env=env, capture_output=True, text=True, timeout=TIMEOUT)
    if done.returncode:
        raise RuntimeError("claude exited %d" % done.returncode)
    return done.stdout


def parse(text):
    """Up to COUNT distinct one-line replies from the model's JSON array, else []."""
    found = re.search(r"\[.*\]", text or "", re.S)
    try:
        items = json.loads(found[0]) if found else []
    except ValueError:
        return []
    replies = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, str):
            continue
        reply = " ".join(item.split())[:MAX_REPLY].strip()
        if reply and reply not in replies:
            replies.append(reply)
    return replies[:COUNT]


def _identity(source):
    if (not isinstance(source, dict) or source.get("agent") not in AGENTS
            or not isinstance(source.get("host"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", source["host"])
            or not isinstance(source.get("id"), str) or not 0 < len(source["id"]) <= 128):
        raise ValueError("대화 출처 형식이 올바르지 않습니다.")
    home = source.get("home")
    if home is not None and (not isinstance(home, str) or len(home) > 80):
        raise ValueError("대화 출처 형식이 올바르지 않습니다.")
    return [source["host"], source["agent"], home or "", source["id"]]


class Suggestions:
    def __init__(self, path, enabled=False, runner=run_claude):
        self.path = Path(path)
        self.enabled = enabled
        self.runner = runner
        self.lock = threading.Lock()
        self.pending = set()
        self.cache = self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {}
        return {key: value for key, value in data.items()
                if isinstance(value, dict) and value.get("status") in ("ready", "failed")
                and isinstance(value.get("replies"), list)} if isinstance(data, dict) else {}

    def _save(self):
        if len(self.cache) > CACHE_LIMIT:
            for key, _value in sorted(self.cache.items(), key=lambda item: item[1].get("at", 0))[:len(self.cache) - CACHE_LIMIT]:
                del self.cache[key]
        temporary = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(self.cache, stream, ensure_ascii=False)
        os.replace(str(temporary), str(self.path))

    def request(self, data):
        """{"status": "off" | "pending" | "ready" | "failed", "replies": [...]} for the texts given."""
        if not self.enabled:
            return {"status": "off", "replies": []}
        if not isinstance(data, dict):
            raise ValueError("추천 답변 요청 형식이 올바르지 않습니다.")
        identity = _identity(data.get("source"))
        question, answer = data.get("question") or "", data.get("answer")
        if not isinstance(question, str) or not isinstance(answer, str) or not answer.strip():
            raise ValueError("추천 답변에 쓸 답변이 없습니다.")
        question, answer = question[-MAX_QUESTION:], answer[-MAX_ANSWER:]
        digest = hashlib.sha256(json.dumps([identity, question, answer], ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            kept = self.cache.get(digest)
            if kept is not None:
                return {"status": kept["status"], "replies": list(kept["replies"])}
            if digest in self.pending:
                return {"status": "pending", "replies": []}
            self.pending.add(digest)
        try:
            threading.Thread(target=self._fill, args=(digest, question, answer), daemon=True,
                             name="suggested-replies").start()
        except RuntimeError:
            with self.lock:
                self.pending.discard(digest)
            raise
        return {"status": "pending", "replies": []}

    def _fill(self, digest, question, answer):
        try:
            replies = parse(self.runner(PROMPT.format(count=COUNT, question=question or "(없음)", answer=answer)))
        except Exception:
            replies = []
        with self.lock:
            self.cache[digest] = {"status": "ready" if replies else "failed", "replies": replies, "at": int(time.time())}
            self.pending.discard(digest)
            try:
                self._save()
            except OSError:
                pass

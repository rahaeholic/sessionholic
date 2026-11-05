"""Opt-in bridge to the existing Woni API on a registered machine."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def validate(data):
    if not isinstance(data, dict) or set(data) != {"text", "requestId"}:
        raise ValueError("메시지와 요청 ID를 확인해 주세요.")
    if not isinstance(data["text"], str) or not 1 <= len(data["text"].strip()) <= 12000:
        raise ValueError("메시지를 확인해 주세요.")
    if not isinstance(data["requestId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", data["requestId"]):
        raise ValueError("요청 ID를 확인해 주세요.")
    return data


def local_call(data=None, home=None):
    config = json.loads(((home or Path.home()) / ".local/share/ryan-one/config.json").read_text())
    token = config["token"] if data is not None else config.get("readToken") or config["token"]
    headers = {"Authorization": "Bearer " + token, "X-Woni-Task-Roles": "1"}
    if data is not None:
        body = json.dumps(validate(data), ensure_ascii=False).encode()
        headers.update({"Content-Type": "application/json", "X-Woni": "1"})
        request = Request("http://127.0.0.1:18772/v1/chat", data=body, headers=headers)
    else:
        request = Request("http://127.0.0.1:18772/v1/state", headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except HTTPError as exc:
        try:
            message = json.load(exc).get("error")
        except (ValueError, OSError):
            message = None
        raise ValueError(message or "원이 요청을 처리하지 못했어요.") from None
    except (URLError, OSError, ValueError):
        raise RuntimeError("원이에 연결하지 못했어요. Air15 연결을 확인해 주세요.") from None
    if data is None:
        keys = ("id", "kind", "text", "answer", "status", "progress", "createdAt", "finishedAt")
        chats = [{key: chat[key] for key in keys if key in chat} for chat in result.get("chats", []) if isinstance(chat, dict)]
        return {"enabled": True, "chats": chats, "activity": result.get("activity", {})}
    return {key: result[key] for key in ("id", "status") if key in result}


class WoniBridge:
    def __init__(self, hosts, config, state_dir):
        self.hosts = {host["name"]: host for host in hosts}
        self.config, self.state_dir = Path(config), state_dir

    def host(self):
        if not self.config.exists():
            return None
        info = self.config.lstat()
        if not self.config.is_file() or self.config.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("원이 연결 설정의 소유권과 권한을 확인해 주세요.")
        data = json.loads(self.config.read_text())
        if not isinstance(data, dict) or set(data) != {"host"} or data["host"] not in self.hosts:
            raise ValueError("원이 연결 기기 설정을 확인해 주세요.")
        host = self.hosts[data["host"]]
        if host.get("platform") == "windows":
            raise ValueError("원이 연결 기기를 확인해 주세요.")
        return host

    def call(self, data=None):
        host = self.host()
        if host is None:
            if data is not None:
                raise ValueError("원이가 연결되지 않았어요.")
            return {"enabled": False}
        if data is not None:
            validate(data)
        if host.get("local"):
            return local_call(data)
        from server import collector_command
        program = ("REMOTE_REQUEST = " + ascii(data) + "\n").encode() + Path(__file__).read_bytes()
        try:
            result = subprocess.run(collector_command(host, [], self.state_dir), input=program,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=25, check=False)
            if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
                raise RuntimeError()
            payload = json.loads(result.stdout)
            if "error" in payload:
                raise ValueError(payload["error"])
            return payload
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError):
            raise RuntimeError("원이에 연결하지 못했어요. 전송했다면 기존 대화를 확인해 주세요.") from None


if __name__ == "__main__" and "REMOTE_REQUEST" in globals():
    try:
        print(json.dumps(local_call(REMOTE_REQUEST), ensure_ascii=True))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True))
    except Exception:
        print(json.dumps({"error": "원이에 연결하지 못했어요. Air15 연결을 확인해 주세요."}, ensure_ascii=True))

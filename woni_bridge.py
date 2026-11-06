"""Opt-in bridge to the existing Woni API on a registered machine."""
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


MAX_FILE = 100 * 1024 * 1024
ATTACHMENT_ID = re.compile(r"\d{8}-[0-9a-f]{12}\Z")


def validate(data):
    if not isinstance(data, dict) or not {"text", "requestId"} <= set(data) or set(data) - {"text", "requestId", "attachments", "replyTo"}:
        raise ValueError("메시지와 요청 ID를 확인해 주세요.")
    if not isinstance(data["text"], str) or not 1 <= len(data["text"].strip()) <= 12000:
        raise ValueError("메시지를 확인해 주세요.")
    if not isinstance(data["requestId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", data["requestId"]):
        raise ValueError("요청 ID를 확인해 주세요.")
    files = data.get("attachments", [])
    if (not isinstance(files, list) or len(files) > 10 or
            any(not isinstance(item, str) or not ATTACHMENT_ID.fullmatch(item) for item in files) or len(set(files)) != len(files)):
        raise ValueError("첨부 파일을 다시 확인해 주세요.")
    reply = data.get("replyTo")
    if reply is not None and (not isinstance(reply, dict) or set(reply) != {"id", "part"} or
            not isinstance(reply["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", reply["id"]) or
            reply["part"] not in ("question", "answer")):
        raise ValueError("답글 대상을 다시 확인해 주세요.")
    return data


def validate_read(data):
    through = data.get("through") if isinstance(data, dict) and set(data) == {"through"} else None
    if isinstance(through, bool) or not isinstance(through, (int, float)) or not math.isfinite(through) or through <= 0:
        raise ValueError("읽은 시각을 확인해 주세요.")
    return data


def read_through(value):
    return value if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None


def local_call(data=None, home=None, operation="state", query=None):
    # A write is either a chat message (operation "state") or the shared read mark ("read").
    if (operation not in ("state", "activity", "chat", "worker-result", "read") or
            data is not None and operation not in ("state", "read") or data is None and operation == "read"):
        raise ValueError("원이 요청을 다시 확인해 주세요.")
    query = query or {}
    if operation == "chat":
        if set(query) != {"id"} or not isinstance(query["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", query["id"]):
            raise ValueError("대화를 다시 선택해 주세요.")
    elif operation == "worker-result":
        if set(query) != {"batch", "n"} or not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", str(query["batch"])) or str(query["n"]) not in tuple("12345678"):
            raise ValueError("작업 결과를 다시 선택해 주세요.")
    elif query:
        raise ValueError("원이 요청을 다시 확인해 주세요.")
    config = json.loads(((home or Path.home()) / ".local/share/ryan-one/config.json").read_text())
    token = config["token"] if data is not None else config.get("readToken") or config["token"]
    headers = {"Authorization": "Bearer " + token, "X-Woni-Task-Roles": "1"}
    if data is not None:
        body = json.dumps(validate_read(data) if operation == "read" else validate(data), ensure_ascii=False).encode()
        headers.update({"Content-Type": "application/json", "X-Woni": "1"})
        request = Request("http://127.0.0.1:18772/v1/" + ("read" if operation == "read" else "chat"), data=body, headers=headers)
    else:
        url = "http://127.0.0.1:18772/v1/" + operation
        request = Request(url + ("?" + urlencode(query) if query else ""), headers=headers)
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
    if data is None and operation in ("state", "chat"):
        keys = ("id", "kind", "text", "answer", "status", "progress", "createdAt", "finishedAt", "stream", "streamTitle", "replyTo")
        chats = []
        for chat in (result.get("chats", []) if operation == "state" else [result]):
            if not isinstance(chat, dict):
                continue
            item = {key: chat[key] for key in keys if key in chat}
            item["attachments"] = [{key: file[key] for key in ("id", "name", "size", "mime") if key in file}
                                   for file in (chat.get("attachments") or []) if isinstance(file, dict)]
            chats.append(item)
        if operation == "chat":
            return chats[0] if chats else {}
        output = {"enabled": True, "chats": chats, "activity": result.get("activity", {})}
        if read_through(result.get("readThrough")) is not None:
            output["readThrough"] = result["readThrough"]  # 원이앱·하던일이 함께 쓰는 '이 시각까지 끝난 답은 읽음'
        return output
    if data is None and operation == "activity":
        def clean_row(row):
            keys = ("id", "text", "stream", "streamTitle", "status", "createdAt", "finishedAt", "progress", "tools", "workers", "workersFailed")
            item = {key: row[key] for key in keys if key in row}
            item["steps"] = [{"tool": step.get("tool", "")} for step in (row.get("steps") or []) if isinstance(step, dict)]
            item["batches"] = [{"id": batch.get("id"), "tasks": [{key: task[key] for key in ("n", "title", "status", "seconds") if key in task}
                                  for task in (batch.get("tasks") or []) if isinstance(task, dict)]}
                               for batch in (row.get("batches") or []) if isinstance(batch, dict)]
            return item
        output = {key: [clean_row(row) for row in (result.get(key) or []) if isinstance(row, dict)] for key in ("running", "recent")}
        output["loose"] = [clean_row({"text": "대화와 연결되지 않은 작업", "status": "running", "batches": [batch]})
                           for batch in (result.get("loose") or []) if isinstance(batch, dict)]
        return output
    if data is None and operation == "worker-result":
        return {key: result[key] for key in ("batch", "n", "text", "truncated") if key in result}
    if operation == "read":
        if read_through(result.get("readThrough")) is None:
            raise ValueError("읽음 표시를 확인하지 못했어요.")
        return {"readThrough": result["readThrough"]}
    return {key: result[key] for key in ("id", "status") if key in result}


# Fixed program, with metadata and bytes on stdin. No credentials or file names enter SSH argv.
UPLOAD_PROGRAM = r'''import http.client,json,sys
from pathlib import Path
from urllib.parse import quote_plus
try:
    meta=json.loads(sys.stdin.buffer.readline(16384))
    config=json.loads((Path.home()/'.local/share/ryan-one/config.json').read_text())
    connection=http.client.HTTPConnection('127.0.0.1',18772,timeout=120)
    connection.putrequest('POST','/v1/attachment')
    for key,value in {'Authorization':'Bearer '+config['token'],'X-Woni':'1','X-File-Name':quote_plus(meta['name']),
                      'Content-Type':meta['mime'],'Content-Length':str(meta['size'])}.items(): connection.putheader(key,value)
    connection.endheaders()
    remaining=meta['size']
    while remaining:
        chunk=sys.stdin.buffer.read(min(1048576,remaining))
        if not chunk: raise ValueError('incomplete input')
        connection.send(chunk); remaining-=len(chunk)
    response=connection.getresponse(); raw=response.read(65537)
    if len(raw)>65536: raise ValueError('oversized response')
    result=json.loads(raw)
    if response.status != 200: print(json.dumps({'error':'파일을 원이에 올리지 못했어요. 다시 첨부해 주세요.'}))
    else: print(json.dumps({'attachment':result},ensure_ascii=True))
except Exception:
    print(json.dumps({'error':'파일을 원이에 올리지 못했어요. 연결을 확인해 주세요.'}))
'''


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

    def call(self, data=None, operation="state", query=None):
        host = self.host()
        if host is None:
            if data is not None or operation != "state":
                raise ValueError("원이가 연결되지 않았어요.")
            return {"enabled": False}
        if data is not None:
            validate_read(data) if operation == "read" else validate(data)
        if host.get("local"):
            return local_call(data, operation=operation, query=query)
        from server import collector_command
        program = ("REMOTE_REQUEST = " + ascii(data) + "\nREMOTE_OPERATION = " + ascii(operation) +
                   "\nREMOTE_QUERY = " + ascii(query) + "\n").encode() + Path(__file__).read_bytes()
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

    def upload(self, stream, length, name, mime):
        host = self.host()
        if host is None:
            raise ValueError("원이가 연결되지 않았어요.")
        if not isinstance(length, int) or not 0 < length <= MAX_FILE:
            raise ValueError("파일은 100MB 이하로 첨부해 주세요.")
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(ord(c) < 32 for c in name):
            raise ValueError("파일 이름을 확인해 주세요.")
        if not isinstance(mime, str) or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime):
            mime = "application/octet-stream"
        if host.get("local"):
            command = [sys.executable, "-I", "-c", UPLOAD_PROGRAM]
        else:
            from server import ssh_command
            remote = shlex.quote(host["python"]) + " -I -c " + shlex.quote(UPLOAD_PROGRAM)
            command = ssh_command(host, remote, self.state_dir)
        try:
            with tempfile.TemporaryFile() as body:
                body.write((json.dumps({"size": length, "name": name, "mime": mime}, ensure_ascii=True) + "\n").encode())
                remaining = length
                while remaining:
                    chunk = stream.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ValueError("파일을 끝까지 받지 못했어요. 다시 첨부해 주세요.")
                    body.write(chunk)
                    remaining -= len(chunk)
                body.seek(0)
                result = subprocess.run(command, stdin=body, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180, check=False)
            if result.returncode or len(result.stdout) > 65536:
                raise RuntimeError()
            value = json.loads(result.stdout)
            if value.get("error"):
                raise ValueError(value["error"])
            attachment = value.get("attachment")
            if not isinstance(attachment, dict) or not isinstance(attachment.get("id"), str) or not ATTACHMENT_ID.fullmatch(attachment["id"]):
                raise RuntimeError()
            return {"attachment": {key: attachment[key] for key in ("id", "name", "size", "mime") if key in attachment}}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError):
            raise RuntimeError("파일을 원이에 올리지 못했어요. 연결을 확인해 주세요.") from None


if __name__ == "__main__" and "REMOTE_REQUEST" in globals():
    try:
        print(json.dumps(local_call(REMOTE_REQUEST, operation=REMOTE_OPERATION, query=REMOTE_QUERY), ensure_ascii=True))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True))
    except Exception:
        print(json.dumps({"error": "원이에 연결하지 못했어요. Air15 연결을 확인해 주세요."}, ensure_ascii=True))

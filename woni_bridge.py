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
    if not isinstance(data, dict) or not {"text", "requestId"} <= set(data) or set(data) - {"text", "requestId", "attachments", "replyTo", "retryOf"}:
        raise ValueError("메시지와 요청 ID를 확인해 주세요.")
    # 실패·중단된 질문 다시 보내기(원이가 그 질문에 잇고, 재시작 때 멈춘 것은 같은 세션에서 이어서 한다)
    if "retryOf" in data and (not isinstance(data["retryOf"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", data["retryOf"])):
        raise ValueError("다시 보낼 질문을 확인해 주세요.")
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


def validate_task_status(data):
    # Exactly what the Woni server's task-status takes; the screen never sends more.
    if not isinstance(data, dict) or set(data) != {"actionId", "status", "expectedEvent", "requestId"}:
        raise ValueError("할 일 상태 요청을 확인해 주세요.")
    if not isinstance(data["actionId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", data["actionId"]):
        raise ValueError("할 일을 다시 선택해 주세요.")
    if data["status"] not in ("done", "open", "reported"):
        raise ValueError("변경할 상태를 확인해 주세요.")
    event = data["expectedEvent"]
    if not isinstance(event, str) or len(event) > 300 or any(ord(c) < 32 or ord(c) == 127 for c in event):
        raise ValueError("할 일 상태 기준을 확인해 주세요.")
    if not isinstance(data["requestId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", data["requestId"]):
        raise ValueError("요청 ID를 확인해 주세요.")
    return data


NOTE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
EVIDENCE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
CHAT_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
STREAM_ID = re.compile(r"[a-z0-9-]{1,40}")
OFFSET = re.compile(r"[0-9]{1,7}")
READ_QUERY_KEYS = {"notes": {"q", "offset"}, "note": {"id", "offset"},
                   "transcript": {"id", "offset", "format", "q", "evidenceId", "context"},
                   "chats": {"before", "stream", "through", "limit"}, "chat-search": {"q", "offset", "limit"}}
# The Woni server's own bounds: a chat page takes 1-200, a search page 1-50.
READ_LIMITS = {"chats": 200, "chat-search": 50}
CONNECT_ERROR = "원이에 연결하지 못했어요. Air15 연결을 확인해 주세요."


def read_query(operation, query):
    """Query of the note, transcript and chat-history reads: only known keys, each a bounded string."""
    if not isinstance(query, dict) or set(query) - READ_QUERY_KEYS[operation]:
        raise ValueError("원이 요청을 다시 확인해 주세요.")
    query = {key: str(value) if isinstance(value, int) and not isinstance(value, bool) else value for key, value in query.items()}
    if any(not isinstance(value, str) for value in query.values()):
        raise ValueError("원이 요청을 다시 확인해 주세요.")
    if "offset" in query and (not OFFSET.fullmatch(query["offset"]) or int(query["offset"]) > 1000000):
        raise ValueError("페이지 위치를 확인해 주세요.")
    if "q" in query and (len(query["q"]) > 300 or any(ord(c) < 32 or ord(c) == 127 for c in query["q"])):
        raise ValueError("검색어를 확인해 주세요.")
    if "limit" in query and (not re.fullmatch(r"[0-9]{1,3}", query["limit"]) or not 1 <= int(query["limit"]) <= READ_LIMITS[operation]):
        raise ValueError("불러올 개수를 확인해 주세요.")
    if operation == "chats":
        # Earlier chats before one, one stream from its start, or up to a found chat: chat IDs and a stream slug only.
        if any(key in query and not CHAT_ID.fullmatch(query[key]) for key in ("before", "through")):
            raise ValueError("대화를 다시 선택해 주세요.")
        if "stream" in query and not STREAM_ID.fullmatch(query["stream"]):
            raise ValueError("작업 흐름을 다시 선택해 주세요.")
        return query
    if operation == "chat-search":
        if not 1 <= len(query.get("q", "").strip()) <= 100:
            raise ValueError("찾을 말을 1~100자로 적어 주세요.")
        return query
    if operation == "notes":
        return query
    if not NOTE_ID.fullmatch(query.get("id", "")):
        raise ValueError("기록을 다시 선택해 주세요.")
    if operation == "note":
        return query
    if "evidenceId" in query:
        # One passage with its neighbours: nothing that selects a page or a search.
        if set(query) - {"id", "evidenceId", "context"} or not EVIDENCE_ID.fullmatch(query["evidenceId"]) or query.get("context", "1") not in ("0", "1", "2"):
            raise ValueError("원문 구간을 다시 선택해 주세요.")
    elif "context" in query or query.get("format", "passages") != "passages" or "q" in query and "format" not in query:
        # A full page takes only id and offset; a search or the passage list needs format=passages.
        raise ValueError("원이 요청을 다시 확인해 주세요.")
    return query


def pick(row, keys):
    return {key: row[key] for key in keys if key in row}


def clean_tasks(result):
    text = ("id", "kind", "title", "project", "due", "assignee", "attention", "taskStatus", "previousStatus", "stateEvent", "quote",
            "requestQuote", "reportQuote", "recordTitle", "recordedAt", "counterpart", "sourceId", "guessBasis", "ownerGuess")
    def clean(row):
        item = {key: row[key] for key in text if isinstance(row.get(key), str)}
        if isinstance(row.get("archived"), bool):
            item["archived"] = row["archived"]
        if isinstance(row.get("autoBasis"), dict):
            item["autoBasis"] = {key: row["autoBasis"][key] for key in ("quote", "source", "at", "reason") if key in row["autoBasis"]}
        return item
    def count(key):
        value = result.get(key)
        return value if type(value) is int and value >= 0 else 0
    output = {key: [clean(row) for row in result.get(key) or [] if isinstance(row, dict)] for key in ("actions", "completedActions", "archivedActions")}
    output["counts"] = {name: count(key) for name, key in (
        ("mine", "actionCount"), ("delegated", "delegatedCount"), ("unassigned", "unassignedCount"), ("followup", "followupCount"),
        ("due", "dueCount"), ("autoDone", "autoDoneCount"), ("completed", "completedActionCount"), ("archived", "archivedCount"))}
    synced = result.get("syncedAt")
    output["syncedAt"] = synced if type(synced) in (int, float) and math.isfinite(synced) else None
    output["error"] = result.get("driveError") if isinstance(result.get("driveError"), str) else ""
    return output


def clean_notes(result):
    output = {"items": [pick(row, ("id", "title", "recordedAt", "preview")) for row in result.get("items") or [] if isinstance(row, dict)],
              "total": result.get("total") if type(result.get("total")) is int else 0,
              "nextOffset": result.get("nextOffset") if type(result.get("nextOffset")) is int else None,
              "query": result.get("query") if isinstance(result.get("query"), str) else "",
              "error": result.get("error") if isinstance(result.get("error"), str) else ""}
    return output


def clean_note(result):
    output = pick(result, ("id", "title", "recordedAt", "text", "nextOffset", "revision"))
    if isinstance(result.get("url"), str) and result["url"].startswith("https://drive.google.com/"):
        output["url"] = result["url"]
    return output


def clean_transcript(result, query):
    segment = ("id", "kind", "label", "text", "start", "end", "speaker", "transcriptDoubt")
    if "evidenceId" in query:
        output = pick(result, segment + ("sourceId", "revision"))
        if isinstance(result.get("context"), list):
            output["context"] = [pick(row, segment) for row in result["context"] if isinstance(row, dict)]
        return output
    if query.get("format") == "passages":
        output = pick(result, ("sourceId", "total", "nextOffset", "query", "label", "revision"))
        output["passages"] = [pick(row, segment + ("position",)) for row in result.get("passages") or [] if isinstance(row, dict)]
        return output
    return pick(result, ("sourceId", "text", "truncated", "offset", "nextOffset", "revision", "label"))


CHAT_KEYS = ("id", "kind", "text", "answer", "status", "progress", "createdAt", "finishedAt", "stream", "streamTitle", "replyTo",
             "retriedBy", "errorKind", "toolsRan")  # 실패·중단 안내와 다시 보내기


def clean_chat(chat):
    item = {key: chat[key] for key in CHAT_KEYS if key in chat}
    if type(chat.get("seq")) is int and chat["seq"] >= 0:
        item["seq"] = chat["seq"]  # the chat's place in Woni's whole list: pages fetched apart are merged by it
    item["attachments"] = [{key: file[key] for key in ("id", "name", "size", "mime") if key in file}
                           for file in (chat.get("attachments") or []) if isinstance(file, dict)]
    return item


def count_of(value):
    return value if type(value) is int and value >= 0 else 0


def clean_chats(result):
    output = {"chats": [clean_chat(chat) for chat in result.get("chats") or [] if isinstance(chat, dict)],
              "hasMore": result.get("hasMore") is True, "total": count_of(result.get("total"))}
    if result.get("tooFar") is True:
        output["tooFar"] = True  # the found chat is too far back to fetch up to: the screen opens its stream instead
    if isinstance(result.get("streamTitle"), str):
        output["streamTitle"] = result["streamTitle"]
    return output


def clean_chat_search(result):
    hit = ("id", "kind", "status", "createdAt", "finishedAt", "stream", "streamTitle", "question", "snippet")
    def clean(row):
        item = pick(row, hit)
        if type(row.get("seq")) is int:
            item["seq"] = row["seq"]
        return item
    return {"q": result.get("q") if isinstance(result.get("q"), str) else "",
            "items": [clean(row) for row in result.get("items") or [] if isinstance(row, dict)],
            "total": count_of(result.get("total")),
            "nextOffset": result.get("nextOffset") if type(result.get("nextOffset")) is int else None,
            "streams": [pick(row, ("id", "title", "count", "lastAt")) for row in result.get("streams") or [] if isinstance(row, dict)]}


def clean_streams(result):
    keys = ("id", "title", "count", "running", "unread", "lastAt", "lastId", "lastText", "lastAnswer", "lastStatus")
    return {"items": [pick(row, keys) for row in result.get("items") or [] if isinstance(row, dict)],
            "total": count_of(result.get("total"))}


HISTORY_CLEANERS = {"chats": clean_chats, "chat-search": clean_chat_search, "streams": clean_streams}
WRITE_VALIDATORS = {"state": validate, "read": validate_read, "task-status": validate_task_status}
WRITE_PATHS = {"state": "chat", "read": "read", "task-status": "task-status"}


def local_call(data=None, home=None, operation="state", query=None):
    # A write is a chat message (operation "state"), the shared read mark ("read") or a task status ("task-status").
    if (operation not in ("state", "activity", "chat", "worker-result", "read", "tasks", "notes", "note", "transcript", "task-status",
                          "chats", "chat-search", "streams") or
            data is not None and operation not in WRITE_VALIDATORS or data is None and operation in ("read", "task-status")):
        raise ValueError("원이 요청을 다시 확인해 주세요.")
    query = query or {}
    if operation == "chat":
        if set(query) != {"id"} or not isinstance(query["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", query["id"]):
            raise ValueError("대화를 다시 선택해 주세요.")
    elif operation == "worker-result":
        if set(query) != {"batch", "n"} or not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", str(query["batch"])) or str(query["n"]) not in tuple("12345678"):
            raise ValueError("작업 결과를 다시 선택해 주세요.")
    elif operation in READ_QUERY_KEYS:
        query = read_query(operation, query)
    elif query:
        raise ValueError("원이 요청을 다시 확인해 주세요.")
    config = json.loads(((home or Path.home()) / ".local/share/ryan-one/config.json").read_text())
    token = config["token"] if data is not None else config.get("readToken") or config["token"]
    headers = {"Authorization": "Bearer " + token, "X-Woni-Task-Roles": "1"}
    if data is not None:
        body = json.dumps(WRITE_VALIDATORS[operation](data), ensure_ascii=False).encode()
        headers.update({"Content-Type": "application/json", "X-Woni": "1"})
        request = Request("http://127.0.0.1:18772/v1/" + WRITE_PATHS[operation], data=body, headers=headers)
    else:
        url = "http://127.0.0.1:18772/v1/" + ("state" if operation == "tasks" else operation)
        request = Request(url + ("?" + urlencode(query) if query else ""), headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except HTTPError as exc:
        try:
            message = json.load(exc).get("error")
        except (ValueError, OSError):
            message = None
        if operation == "task-status" and exc.code >= 500:
            raise RuntimeError(message or CONNECT_ERROR) from None  # the change may have been saved
        raise ValueError(message or "원이 요청을 처리하지 못했어요.") from None
    except (URLError, OSError, ValueError):
        raise RuntimeError(CONNECT_ERROR) from None
    if operation in ("tasks", "notes", "note", "transcript", "task-status", "chats", "chat-search", "streams") and not isinstance(result, dict):
        raise RuntimeError(CONNECT_ERROR)
    if operation in HISTORY_CLEANERS:
        return HISTORY_CLEANERS[operation](result)
    if operation == "tasks":
        return clean_tasks(result)
    if operation == "notes":
        return clean_notes(result)
    if operation == "note":
        return clean_note(result)
    if operation == "transcript":
        return clean_transcript(result, query)
    if operation == "task-status":
        output = pick(result, ("actionId", "taskStatus", "stateEvent", "result"))
        if set(output) != {"actionId", "taskStatus", "stateEvent", "result"} or output["result"] not in ("saved", "unconfirmed"):
            raise RuntimeError(CONNECT_ERROR)  # an answer this screen cannot read is not a verdict on the save
        return output
    if data is None and operation in ("state", "chat"):
        chats = [clean_chat(chat) for chat in (result.get("chats", []) if operation == "state" else [result]) if isinstance(chat, dict)]
        if operation == "chat":
            return chats[0] if chats else {}
        output = {"enabled": True, "chats": chats, "activity": result.get("activity", {})}
        # The state holds Woni's latest 60 chats; these say whether earlier ones can be fetched with /v1/chats.
        if isinstance(result.get("chatsHasMore"), bool):
            output["chatsHasMore"] = result["chatsHasMore"]
        if type(result.get("chatTotal")) is int and result["chatTotal"] >= 0:
            output["chatTotal"] = result["chatTotal"]
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
            if operation not in WRITE_VALIDATORS:
                raise ValueError("원이 요청을 다시 확인해 주세요.")
            WRITE_VALIDATORS[operation](data)
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
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError):
            raise RuntimeError("원이에 연결하지 못했어요. 전송했다면 기존 대화를 확인해 주세요.") from None
        if isinstance(payload, dict) and payload.get("failed"):  # not "error": the task and note replies carry their own error text
            # Only a status change whose outcome is unknown stays an unknown outcome across SSH.
            raise (RuntimeError if operation == "task-status" and payload.get("unknown") else ValueError)(payload.get("error") or CONNECT_ERROR)
        return payload

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
        print(json.dumps({"failed": True, "error": str(exc)}, ensure_ascii=True))
    except Exception:
        print(json.dumps({"failed": True, "error": CONNECT_ERROR, "unknown": True}, ensure_ascii=True))

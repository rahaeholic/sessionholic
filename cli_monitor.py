"""Cached read-only machine maintenance receipts, independent of session collectors."""
import copy
from datetime import datetime
import re
import threading
import time


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def normalize(raw):
    if not isinstance(raw, dict) or raw.get("schema") != 1 or type(raw.get("configured")) is not bool:
        raise ValueError("invalid maintenance receipt")
    tools = {}
    for tool in ("codex", "claude"):
        row = raw.get("tools", {}).get(tool, {})
        if not isinstance(row, dict):
            raise ValueError("invalid tool receipt")
        version = row.get("version")
        tools[tool] = {"version": version if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", version) else None,
                       "status": row.get("status") if row.get("status") in ("checking", "updated", "current", "failed") else "unknown",
                       "checkedAt": timestamp(row.get("checkedAt")), "lastSuccessAt": timestamp(row.get("lastSuccessAt")),
                       "channel": row.get("channel") if row.get("channel") in ("stable", "latest") else None}
    schedule = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
    return {"configured": raw["configured"], "daily": schedule.get("daily") is True,
            "startedAt": timestamp(raw.get("startedAt")), "finishedAt": timestamp(raw.get("finishedAt")), "tools": tools}


class CliMonitor:
    def __init__(self, hosts, runner, clock=time.time, threaded=True):
        self.hosts, self.runner, self.clock, self.threaded = hosts, runner, clock, threaded
        self.lock = threading.Lock()
        self.rows = {h["name"]: {"data": None, "fetchedAt": None, "attemptedAt": None,
                                  "refreshing": False, "failed": False} for h in hosts}

    def snapshot(self, refresh=False, force=False):
        now, due = self.clock(), []
        with self.lock:
            for host in self.hosts:
                row = self.rows[host["name"]]
                interval = 10 if force else 300 if row["failed"] else 60
                if (refresh or force) and not row["refreshing"] and (row["attemptedAt"] is None or now - row["attemptedAt"] >= interval):
                    row.update(refreshing=True, attemptedAt=now)
                    due.append(host)
        for host in due:
            if self.threaded:
                threading.Thread(target=self._read, args=(host,), daemon=True, name="cli-status-" + host["name"]).start()
            else:
                self._read(host)
        with self.lock:
            rows = []
            for host in self.hosts:
                row = copy.deepcopy(self.rows[host["name"]])
                data = row["data"]
                checked = data.get("finishedAt") if data else None
                status = ("stale" if data else "offline") if row["failed"] else \
                    "pending" if data is None else "unconfigured" if not data["configured"] else \
                    "stale" if checked is None or now - checked > 172800 else "ready"
                rows.append({"name": host["name"], "label": host.get("label") or host["name"],
                             "monitorOnly": host.get("monitorOnly") is True, "status": status, **row})
            return {"hosts": rows, "refreshing": any(row["refreshing"] for row in rows)}

    def _read(self, host):
        try:
            data = normalize(self.runner(host))
        except Exception:
            with self.lock:
                self.rows[host["name"]].update(failed=True, refreshing=False)
            return
        with self.lock:
            self.rows[host["name"]].update(data=data, fetchedAt=self.clock(), failed=False, refreshing=False)

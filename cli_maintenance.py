"""Per-user CLI maintenance. `run` updates; `snapshot` only reads private state.

No board HTTP request invokes `run`. A machine's own scheduler invokes this file.
Never stops sessions. npm releases are staged separately before an atomic launcher
switch, so files used by existing processes remain available.
"""
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request


VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?\Z")
ROOT = Path.home() / ".local/share/sessionholic/cli-maintenance"


def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    if path.stat().st_size > 65536:
        raise ValueError("oversized maintenance file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("invalid maintenance file")
    return value


def config(root):
    value = read_json(root / "config.json")
    for tool in ("codex", "claude"):
        row = value.get(tool)
        if not isinstance(row, dict) or not Path(row.get("path", "")).is_absolute():
            raise ValueError("absolute launcher path required")
    row = value["codex"]
    if row.get("installer") not in ("npm", "standalone"):
        raise ValueError("unsupported Codex installer")
    if row["installer"] == "npm":
        for field in ("npm", "node"):
            if not Path(row.get(field, "")).is_absolute():
                raise ValueError("absolute npm/node path required")
    return value


def command(args, timeout=30, env=None):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, timeout=timeout, env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        # Do not retain installer output, environment, user paths or credentials.
        raise RuntimeError("command_failed")
    return result.stdout.decode("utf-8", errors="replace")[:65536]


def version(row, tool):
    args = [row["path"], "--safe-mode", "--version"] if tool == "claude" else \
        ([row["node"], row["path"], "--version"] if row.get("node") else [row["path"], "--version"])
    output = command(args, timeout=15)
    match = re.search(r"\b(\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?)\b", output)
    if not match or not VERSION.fullmatch(match[1]):
        raise ValueError("version_unavailable")
    return match[1]


def native_policy(row):
    output = command([row["path"], "--safe-mode", "doctor"], timeout=30)
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    channel = re.search(r"Update channel:\s*(latest|stable)\b", output, re.I)
    enabled = re.search(r"Auto-updates:\s*enabled\b", output, re.I)
    native = re.search(r"(?:Currently running|Installation method|Install method):[^\n]*native", output, re.I)
    # A native versions directory is also a stable, read-only installation check.
    resolved = str(Path(row["path"]).resolve()).replace("\\", "/").lower()
    native = bool(native or "/claude/versions/" in resolved or "/.local/bin/claude.exe" in resolved)
    return {"channel": channel[1].lower() if channel else None,
            "autoUpdates": bool(enabled), "native": native}


def latest_codex():
    with urllib.request.urlopen("https://registry.npmjs.org/@openai%2fcodex/latest", timeout=30) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise ValueError("registry_response_invalid")
    value = json.loads(raw).get("version")
    if not isinstance(value, str) or not VERSION.fullmatch(value) or "-" in value:
        raise ValueError("registry_version_invalid")
    return value


def update_npm(row, root, current, latest):
    if not VERSION.fullmatch(current) or not VERSION.fullmatch(latest) or "-" in latest:
        raise ValueError("invalid stable release version")
    current_core = tuple(map(int, current.split("-", 1)[0].split(".")))
    latest_core = tuple(map(int, latest.split(".")))
    if current_core > latest_core or (current_core == latest_core and "-" not in current):
        return current
    launcher = Path(row["path"])
    if not launcher.is_symlink():
        raise ValueError("launcher_not_symlink")
    previous = os.readlink(str(launcher))
    prefix = root / "codex-releases" / latest
    candidate = prefix / "bin/codex"
    env = dict(os.environ)
    env["PATH"] = str(Path(row["node"]).parent) + os.pathsep + env.get("PATH", "")
    # npm is pinned to one validated version. No in-place global package overwrite.
    command([row["node"], row["npm"], "install", "--global", "--prefix", prefix,
             "@openai/codex@" + latest, "--registry=https://registry.npmjs.org",
             "--ignore-scripts", "--no-audit", "--no-fund"], timeout=600, env=env)
    staged = {"path": str(candidate), "node": row["node"]}
    if version(staged, "codex") != latest:
        raise ValueError("candidate_version_mismatch")
    command([row["node"], candidate, "app-server", "--help"], timeout=15, env=env)
    if not launcher.is_symlink() or os.readlink(str(launcher)) != previous:
        raise ValueError("launcher_changed_during_update")
    temporary = launcher.with_name(".sessionholic-codex-" + os.urandom(8).hex())
    try:
        temporary.symlink_to(candidate)
        os.replace(str(temporary), str(launcher))
    finally:
        if temporary.is_symlink():
            temporary.unlink()
    # All prior versions, including the original global package, are kept intact.
    return latest


@contextlib.contextmanager
def lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "run.lock").open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def maintain(root=ROOT):
    with lock(root) as acquired:
        if not acquired:
            return 0
        settings = config(root)
        try:
            previous = read_json(root / "status.json")
        except (OSError, ValueError):
            previous = {}
        state = {"schema": 1, "startedAt": stamp(), "finishedAt": None, "tools": {}}
        for tool in ("codex", "claude"):
            row = settings[tool]
            old = previous.get("tools", {}).get(tool, {})
            result = {"version": old.get("version"), "lastSuccessAt": old.get("lastSuccessAt"),
                      "status": "checking", "checkedAt": stamp(), "channel": None}
            state["tools"][tool] = result
            atomic_json(root / "status.json", state)
            try:
                before = version(row, tool)
                result["version"] = before
                if tool == "codex":
                    if row["installer"] == "npm":
                        update_npm(row, root, before, latest_codex())
                    else:
                        command([row["path"], "update"], timeout=600)
                else:
                    policy = native_policy(row)
                    result.update(channel=policy["channel"], autoUpdates=policy["autoUpdates"])
                    if not policy["native"] or not policy["autoUpdates"] or policy["channel"] is None:
                        raise ValueError("native_policy_unconfirmed")
                    # Native updater chooses the existing channel, including stable.
                    command([row["path"], "--safe-mode", "update"], timeout=600)
                result["version"] = version(row, tool)
                result.update(status="updated" if before != result["version"] else "current",
                              lastSuccessAt=stamp())
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                result["status"] = "failed"
            result["checkedAt"] = stamp()
            atomic_json(root / "status.json", state)
        state["finishedAt"] = stamp()
        atomic_json(root / "status.json", state)
        return int(any(row["status"] == "failed" for row in state["tools"].values()))


def snapshot(root=ROOT):
    """No subprocess, network, installs or state writes; safe to serve over SSH."""
    try:
        settings = config(root)
    except (OSError, ValueError):
        return {"schema": 1, "configured": False, "tools": {}}
    try:
        state = read_json(root / "status.json")
    except (OSError, ValueError):
        state = {"schema": 1, "tools": {}}
    return {**state, "configured": True, "schedule": settings.get("schedule", {})}


def main():
    action = sys.argv[1] if len(sys.argv) == 2 else "snapshot"
    if action == "snapshot":
        print(json.dumps(snapshot(), ensure_ascii=True))
        return 0
    if action == "run":
        try:
            return maintain()
        except (OSError, ValueError):
            return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

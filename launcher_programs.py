"""What each "새 세션" terminal ended up running on this host, read from the process table.

A 새 세션 terminal runs this host's launcher (the host settings' `launcher`). Its launcher
process starts when the terminal opens, so the board's creation time names it; the first
agent CLI below it is the program: Claude's agent view (`claude agents`, with its --cwd),
Claude, or Codex. Only `ps` is read; nothing is signalled. A terminal whose launcher cannot
be named exactly (none, or two within the window) reports no program rather than a guess.
"""
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import time
import uuid

# The launcher's process starts a little after the board created the terminal (SSH setup).
BEFORE, AFTER = 3, 15
MAX_TERMINALS = 32
PAIRING_LIMIT = 6  # terminals weighed together at most; more name none
PS = "/bin/ps"
LSTART = "%a %b %d %H:%M:%S %Y"
# Claude's own helpers under the agent view, never a program a person runs.
INTERPRETERS = {"zsh", "bash", "sh", "env", "python3", "python"}
CLAUDE_HELPERS = {"daemon", "bg-pty-host", "bg-spare", "--chrome-native-host", "mcp", "doctor"}


def table(runner=None, uid=None):
    """[(pid, ppid, started_epoch, argv_text)] of this user's processes from `ps`, in the C
    locale and this process's time zone (so `lstart` and `mktime` agree)."""
    uid = os.getuid() if uid is None else uid
    command = [PS, "-axww", "-o", "uid=,pid=,ppid=,lstart=,command="]
    if runner is None:
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"}
        if os.environ.get("TZ"):
            env["TZ"] = os.environ["TZ"]
        done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, env=env)
        if done.returncode != 0:
            raise RuntimeError("프로세스 목록을 읽지 못했습니다.")
        text = done.stdout.decode("utf-8", "replace")
    else:
        text = runner(command)
    rows = []
    for line in text.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\w{3} \w{3}\s+\d+ \d\d:\d\d:\d\d \d{4})\s+(.*)$", line)
        if not match or int(match[1]) != uid:
            continue
        try:
            started = time.mktime(time.strptime(re.sub(r"\s+", " ", match[4]), LSTART))
        except ValueError:
            continue
        rows.append((int(match[2]), int(match[3]), int(started), match[5].strip()))
    return rows


def program_of(command):
    """{"program", "cwd"} for an agent CLI's (or an SSH connection's) command line, else None."""
    words = command.split()
    if not words:
        return None
    name = Path(words[0]).name
    rest = words[1:]
    if name == "node" and rest and Path(rest[0]).name in ("codex", "codex.js"):
        name, rest = "codex", rest[1:]
    if name == "claude":
        if rest[:1] == ["agents"]:
            # `ps` joins arguments with spaces, so a folder runs until the next option.
            found = re.search(r"\s--cwd(?:=|\s+)(.+?)(?=\s+-|$)", command)
            cwd = found[1].strip() if found else None
            return {"program": "claude-agents", "cwd": cwd if cwd and cwd.startswith("/") else None}
        if rest[:1] and rest[0] in CLAUDE_HELPERS:
            return None
        return {"program": "claude", "cwd": None}
    if name == "codex":
        if rest[:2] == ["app-server", "daemon"] or rest[:1] == ["app-server"]:
            return None
        return {"program": "codex", "cwd": None}
    # A session the launcher opened on another host (a PC's): work of its own, never a menu.
    if name == "ssh":
        return {"program": "ssh", "cwd": None}
    return None


def describe(terminals, launcher, rows):
    """{terminal id: {"program", "cwd", "pid"}, {"program": None, "launcher": True} (its launcher,
    nothing chosen below it) or {"program": None} (no launcher named)} for the given terminals."""
    expected = " ".join(launcher)
    children = {}
    for pid, ppid, _, _ in rows:
        children.setdefault(ppid, []).append(pid)
    by_pid = {pid: (ppid, started, command) for pid, ppid, started, command in rows}
    # A script launcher shows its interpreter first (`/bin/zsh /path/launcher`).
    launchers = [(pid, started) for pid, _, started, command in rows
                 if command == expected or (command.endswith(" " + expected)
                                            and Path(command.split()[0]).name in INTERPRETERS)]
    named = [(terminal["id"], terminal["createdAt"]) for terminal in terminals[:MAX_TERMINALS]
             if isinstance(terminal.get("id"), str) and re.fullmatch(r"[0-9a-f]{32}", terminal["id"])
             and type(terminal.get("createdAt")) is int]
    window = {identifier: {pid for pid, started in launchers if created - BEFORE <= started <= created + AFTER}
              for identifier, created in named}
    owned = {}
    # Terminals whose windows share a launcher are weighed together. As many launchers as
    # terminals that pair up in only one way: each takes its own (a launcher slow to start or
    # two started in the other order may pair two ways, and then names neither). Otherwise
    # (one ended, one is another program's) only a launcher that a single terminal could have
    # opened, and the only one in its window, names it.
    left = [identifier for identifier, _ in named]
    while left:
        group, queue = set(), [left[0]]
        while queue:
            identifier = queue.pop()
            if identifier in group:
                continue
            group.add(identifier)
            queue.extend(other for other in left if other not in group and window[other] & window[identifier])
        left = [identifier for identifier in left if identifier not in group]
        members = sorted((created, identifier) for identifier, created in named if identifier in group)
        pids = sorted({pid for identifier in group for pid in window[identifier]})
        if len(pids) == len(members) <= PAIRING_LIMIT:
            pairings = [order for order in itertools.permutations(pids)
                        if all(pid in window[identifier] for pid, (_, identifier) in zip(order, members))]
            if len(pairings) == 1:
                owned.update({identifier: [pid] for pid, (_, identifier) in zip(pairings[0], members)})
                continue
        for _, identifier in members:
            if len(window[identifier]) == 1:
                pid = next(iter(window[identifier]))
                if all(pid not in window[other] for other in group if other != identifier):
                    owned[identifier] = [pid]
    result = {}
    for identifier, _ in named:
        matches = owned.get(identifier, [])
        if len(matches) != 1:
            result[identifier] = {"program": None}
            continue
        found, queue, seen = None, [matches[0]], set()
        while queue and found is None:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            if pid != matches[0]:
                found = program_of(by_pid[pid][2])
                if found is not None:
                    found = {**found, "pid": pid}
                    break
            queue.extend(sorted(children.get(pid, [])))
        # Its own launcher, with nothing chosen below it yet: the menu still waits.
        result[identifier] = found or {"program": None, "launcher": True}
    return result


def launcher_programs(args):
    from launch import new_session
    try:
        launcher = new_session()["argv"]
    except ValueError:
        return {"terminals": {}}
    terminals = args.get("terminals") if isinstance(args, dict) else None
    if not isinstance(terminals, list):
        raise ValueError("터미널 목록 형식이 올바르지 않습니다.")
    if os.name == "nt":
        return {"terminals": {}}
    rows = table()
    result = describe(terminals, launcher, rows)
    for terminal in terminals[:MAX_TERMINALS]:
        identifier = terminal.get("id")
        if identifier not in result:
            continue
        exact = held_codex(terminal.get("holdName"), rows)
        if exact:
            result[identifier] = exact
    return {"terminals": result}


def held_codex(hold, rows):
    """Read the exact hold pane's CLI and its writable original rollout, never a time guess."""
    if not isinstance(hold, str) or not re.fullmatch(r"sh-[0-9a-f]{16}", hold):
        return None
    from transfer_worker import HOLD_SOCKET
    from collector import which
    tmux = which("tmux")
    if not tmux:
        return None
    try:
        done = subprocess.run([tmux, "-L", HOLD_SOCKET, "-f", "/dev/null", "list-panes", "-t", "=" + hold,
                               "-F", "#{pane_pid}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=3, text=True)
        panes = done.stdout.splitlines()
        if done.returncode or len(panes) != 1 or not panes[0].isdigit():
            return None
        by_pid = {pid: command for pid, _, _, command in rows}
        children = {}
        for pid, parent, _, _ in rows:
            children.setdefault(parent, []).append(pid)
        queue, seen, agents = [int(panes[0])], set(), []
        while queue and len(seen) < 128:
            pid = queue.pop(0)
            if pid in seen or pid not in by_pid:
                continue
            seen.add(pid)
            program = program_of(by_pid[pid])
            if program:
                agents.append((pid, program["program"]))
            else:
                queue.extend(children.get(pid, []))
        if queue or len(agents) != 1 or agents[0][1] != "codex":
            return None
        # The Node wrapper, native TUI and its embedded app-server can own the writer.
        # Do not descend through command tools, MCP servers or independently launched agents.
        first = agents[0][0]
        queue, pids = [first], set()
        while queue and len(pids) < 8:
            pid = queue.pop(0)
            pids.add(pid)
            for child in children.get(pid, []):
                command = by_pid[child]
                words = command.split()
                if (program_of(command) or {}).get("program") == "codex" or (
                        Path(words[0]).name == "codex" and words[1:2] == ["app-server"]
                        and words[2:3] != ["daemon"]):
                    if child not in pids:
                        queue.append(child)
        if queue:
            return None
        source = writable_codex_source(pids)
        if source:
            return {"program": "codex", "pid": first, "cwd": source["cwd"], "nativeSource": source}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def writable_codex_source(pids, home=None):
    """Only metadata from one writable, user-owned interactive rollout is evidence."""
    from launch import HOME_KEY
    home = Path.home() if home is None else Path(home)
    done = subprocess.run(["/usr/sbin/lsof", "-a", "-p", ",".join(str(pid) for pid in sorted(pids)), "-Ffan"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, text=True)
    if done.returncode or len(done.stdout) > 2 * 1024 * 1024:
        return None
    paths, access = set(), None
    for line in done.stdout.splitlines():
        if line.startswith("f"):
            access = None
        elif line.startswith("a"):
            access = line[1:]
        elif line.startswith("n") and access in ("w", "u") and line.endswith(".jsonl"):
            paths.add(line[1:])
    if len(paths) > 32:
        return None
    sources = {}
    for name in paths:
        path = Path(name)
        try:
            parts = path.relative_to(home).parts
            position = parts.index("sessions")
            key = "/".join(parts[:position])
            if not HOME_KEY.fullmatch(key) or not path.name.startswith("rollout-"):
                continue
            if any(p.is_symlink() for p in (path, *path.parents) if p != home and home in p.parents):
                continue
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    continue
                line = stream.readline(65537)
            if len(line) > 65536:
                continue
            record = json.loads(line)
            meta = record.get("payload") if record.get("type") == "session_meta" else None
            if not isinstance(meta, dict) or meta.get("source") not in ("cli", "vscode"):
                continue
            identifier = str(uuid.UUID(meta.get("id")))
            cwd = meta.get("cwd")
            if (identifier != meta.get("id") or not path.name.endswith("-" + identifier + ".jsonl")
                    or not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd):
                continue
            source = {"agent": "codex", "id": identifier, "home": key, "cwd": cwd}
            sources[json.dumps(source, sort_keys=True)] = source
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
            continue
    return next(iter(sources.values())) if len(sources) == 1 else None

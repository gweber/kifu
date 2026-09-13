"""Where sessions live, and an archive that outlives each tool's own cleanup.

Claude Code deletes session files after `cleanupPeriodDays` (30 days unless configured). `kifu pull` copies every
configured source into <data_dir>/archive/<host>/<kind> with rsync and never deletes, so the archive keeps what
the machines forget. File-based tools (claude, codex, gemini, cline) are always read from the archive.

SQLite-based tools (hermes, opencode) keep their own history and do not prune it: on this machine they are read
in place, read-only; from another machine `kifu pull` fetches a consistent `.backup` copy when it changed.
"""
import os
import shlex
import subprocess

from . import config

RSYNC_INCLUDES = {"claude": ["*.jsonl"], "codex": ["*.jsonl"], "gemini": ["*.json"], "cline": ["*.json"]}
SQLITE_KINDS = {"hermes", "opencode"}


def archive_root(host, kind="claude"):
    # Claude Code's archive keeps the layout kifu used before other tools existed.
    return os.path.join(config.get().archive_dir, host, "projects" if kind == "claude" else kind)


def archived(src):
    """Whether scans read this source from the archive (or in place)."""
    return not (src.kind in SQLITE_KINDS and not src.ssh)


def source_root(src):
    if src.kind in SQLITE_KINDS:
        if not src.ssh:
            return os.path.expanduser(src.path)
        return os.path.join(archive_root(src.host, src.kind), os.path.basename(src.path))
    return archive_root(src.host, src.kind)


SSH_OPTIONS = "ssh -o BatchMode=yes -o ConnectTimeout=10"


def _ssh(src, command):
    return SSH_OPTIONS.split() + [src.ssh, command]


def _pull_files(src):
    dest = archive_root(src.host, src.kind)
    os.makedirs(dest, exist_ok=True)
    path = src.path.rstrip("/") + "/"
    remote = f"{src.ssh}:{path}" if src.ssh else os.path.expanduser(path)
    includes = [f"--include={pattern}" for pattern in RSYNC_INCLUDES.get(src.kind, ["*.jsonl"])]
    cmd = ["rsync", "-a", "--partial", "--include=*/", *includes, "--exclude=*", remote, dest + "/"]
    if src.ssh:
        cmd[1:1] = ["-e", SSH_OPTIONS]
    return subprocess.run(cmd, capture_output=True, text=True), dest


def remote_path(path):
    """A path for a remote shell: ~/ expands to the remote home, everything else stays quoted."""
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def _pull_sqlite(src):
    """A consistent copy of a remote SQLite history, fetched only when the database changed."""
    dest = source_root(src)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    stamp = dest + ".mtime"
    quoted = remote_path(src.path)
    probe = subprocess.run(_ssh(src, f"stat -c %Y {quoted} 2>/dev/null || stat -f %m {quoted}"),
                           capture_output=True, text=True, timeout=60)
    if probe.returncode:
        return probe, dest
    remote_mtime = probe.stdout.strip()
    if os.path.exists(dest) and os.path.exists(stamp) and open(stamp).read().strip() == remote_mtime:
        return probe, dest
    tmp = f"/tmp/kifu-{src.kind}-{os.getpid()}.db"
    backup = subprocess.run(_ssh(src, f"sqlite3 -readonly {quoted} {shlex.quote('.backup ' + tmp)}"),
                            capture_output=True, text=True, timeout=600)
    if backup.returncode:
        return backup, dest
    copy = subprocess.run(["rsync", "-a", "-e", SSH_OPTIONS, f"{src.ssh}:{tmp}", dest], capture_output=True, text=True)
    subprocess.run(_ssh(src, f"rm -f {shlex.quote(tmp)}"), capture_output=True, text=True, timeout=60)
    if copy.returncode == 0:
        with open(stamp, "w") as fh:
            fh.write(remote_mtime)
    return copy, dest


def pull(log=print, only_local=False):
    """Copy each source into the archive. No deletes: the archive only grows. Returns the sources that failed."""
    failed = []
    for src in config.get().source_list():
        if (only_local and src.ssh) or not archived(src):
            continue                    # remote sources wait for the next full pull; local SQLite is read in place
        label = src.host if src.kind == "claude" else f"{src.host}/{src.kind}"
        result, dest = _pull_sqlite(src) if src.kind in SQLITE_KINDS else _pull_files(src)
        if result.returncode:
            log(f"{label}: pull failed: {(result.stderr or result.stdout).strip()[-300:]}")
            failed.append(label)
        else:
            log(f"{label}: pulled into {dest}")
    return failed

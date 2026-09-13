"""Where sessions live, and an archive that outlives Claude Code's cleanup.

Claude Code deletes session files after `cleanupPeriodDays` (30 days unless configured).
`kifu pull` copies every configured source into <data_dir>/archive/<host>/projects with
rsync and never deletes, so the archive keeps what the machines forget. Scans read the archive.
"""
import os
import subprocess

from . import config


def archive_root(host):
    return os.path.join(config.get().archive_dir, host, "projects")


def pull(log=print, only_local=False):
    """rsync each source into the archive. No --delete: the archive only grows. Returns the hosts that failed."""
    failed = []
    for src in config.get().source_list():
        if only_local and src.ssh:
            continue
        dest = archive_root(src.host)
        os.makedirs(dest, exist_ok=True)
        path = src.path.rstrip("/") + "/"
        remote = f"{src.ssh}:{path}" if src.ssh else os.path.expanduser(path)
        cmd = ["rsync", "-a", "--partial", "--include=*/", "--include=*.jsonl", "--exclude=*", remote, dest + "/"]
        if src.ssh:
            cmd[1:1] = ["-e", "ssh -o BatchMode=yes -o ConnectTimeout=10"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            log(f"{src.host}: pull failed: {(r.stderr or r.stdout).strip()[-300:]}")
            failed.append(src.host)
        else:
            log(f"{src.host}: pulled into {dest}")
    return failed

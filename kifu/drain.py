"""`kifu drain`: analyze sessions queued by the SessionEnd hook, shortly after they end.

Waits until no session has ended for DEBOUNCE seconds, so a burst of short sessions becomes one run, then pulls
this machine's sessions, scans, embeds, analyzes and links. Only new work costs model time; git checks stay
with `kifu verify` / `kifu run`. A lock keeps drains from overlapping; entries queued during a run stay for the next.
"""
import fcntl
import os
import time

from . import analyze, config, db, embed, extract, hooks, link, sources

DEBOUNCE = 120


def pending():
    try:
        with open(hooks.queue_path()) as fh:
            return sum(1 for line in fh if line.strip())
    except FileNotFoundError:
        return 0


def _quiet_for():
    try:
        return time.time() - os.path.getmtime(hooks.queue_path())
    except FileNotFoundError:
        return float("inf")


def _consume(n):
    """Drop the first n entries; keep whatever arrived while the run was going."""
    path = hooks.queue_path()
    with open(path) as fh:
        rest = [line for line in fh if line.strip()][n:]
    with open(path, "w") as fh:
        fh.writelines(rest)


def drain(debounce=DEBOUNCE, log=print):
    cfg = config.get()
    os.makedirs(cfg.data_dir, exist_ok=True)
    lock = open(os.path.join(cfg.data_dir, "drain.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another drain is running")
        return 0
    runs = 0
    while (n := pending()):
        wait = debounce - _quiet_for()
        if wait > 0:
            time.sleep(min(wait, debounce))
            continue
        log(f"{time.strftime('%Y-%m-%d %H:%M')} draining {n} ended session(s)")
        sources.pull(log=log, only_local=True)
        con = db.connect()
        extract.scan_archive(con, log=log)
        embed.build_moves(con)
        embed.embed_moves(con, log=log)
        analyze.analyze(lambda: db.connect(), log=log)
        link.build_lines(con, log=log)
        _consume(n)
        runs += 1
    return runs

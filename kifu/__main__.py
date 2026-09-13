"""kifu: replay the record of your Claude Code sessions and find the ideas left on the board."""
import argparse
import dataclasses
import json
import os
import sys
import time

from . import analyze, config, db, embed, extract, link, report, sources


def cmd_pull(args, con):
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if sources.pull(log=lambda m: print(f"{stamp} {m}", flush=True)):
        sys.exit(1)


def cmd_scan(args, con):
    t0 = time.time()
    if args.root:
        extract.scan(con, args.root, force=args.force, host=args.host)
    else:
        extract.scan_archive(con, force=args.force)
    row = con.execute("""SELECT COUNT(*) n, SUM(n_turns) turns, SUM(automated) auto,
                         MIN(started) first, MAX(ended) last FROM sessions""").fetchone()
    if not row["n"]:
        print("db: no sessions yet — run `kifu pull` first, or `kifu scan <folder>`")
        return
    print(f"db: {row['n']} sessions ({row['auto']} automated), {row['turns']} turns, "
          f"{row['first'][:10]} .. {row['last'][:10]}  [{time.time() - t0:.0f}s]")


def cmd_embed(args, con):
    print(f"{embed.build_moves(con)} moves")
    embed.embed_moves(con)


def cmd_analyze(args, con):
    if analyze.analyze(lambda: db.connect(args.db), backend=args.backend, workers=args.workers, only=args.session):
        sys.exit(1)


def cmd_link(args, con):
    link.build_lines(con, backend=args.backend, workers=args.workers)


def cmd_run(args, con):
    """Everything, incrementally: only new or changed sessions cost model time."""
    sources.pull()
    extract.scan_archive(con)
    cmd_embed(args, con)
    failed = analyze.analyze(lambda: db.connect(args.db), backend=args.backend, workers=args.workers)
    link.build_lines(con, backend=args.backend, workers=args.workers)
    if failed:
        sys.exit(f"{failed} sessions failed to analyze; the next run retries them")


def cmd_sessions(args, con):
    q = "SELECT * FROM sessions WHERE automated=0"
    params = []
    if args.project:
        q += " AND project LIKE ?"
        params.append(f"%{args.project}%")
    for s in con.execute(q + " ORDER BY started", params):
        title = s["title"] or s["ai_title"] or ""
        print(f"{s['started'][:16].replace('T', ' ')}  {s['id'][:8]}  {(s['host'] or '')[:10]:10}  "
              f"{s['project'][:28]:28}  {s['n_turns']:4}  {title[:60]}")


def cmd_show(args, con):
    s = con.execute("SELECT * FROM sessions WHERE id >= ? AND id < ?", db.prefix_range(args.session)).fetchone()
    if not s:
        sys.exit(f"no session {args.session}")
    print(json.dumps({k: s[k] for k in s.keys()}, indent=1))
    for t in con.execute("SELECT * FROM turns WHERE session_id=? ORDER BY idx", (s["id"],)):
        mark = " (copy)" if t["dup_of"] else ""
        print(f"\n#{t['idx']} {t['ts'][:16]}{mark}\n> {t['prompt'][:args.width]}\n  {t['reply'][:args.width]!r}")
    for e in con.execute("SELECT kind, COUNT(*) n FROM evidence WHERE session_id=? GROUP BY kind", (s["id"],)):
        print(f"evidence {e['kind']}: {e['n']}")


def cmd_threads(args, con):
    q = "SELECT t.*, s.project FROM threads t JOIN sessions s ON s.id=t.session_id WHERE 1=1"
    params = []
    if args.session:
        q += " AND t.session_id >= ? AND t.session_id < ?"
        params += db.prefix_range(args.session)
    if args.status:
        q += f" AND t.status IN ({','.join('?' * len(args.status.split(',')))})"
        params += args.status.split(",")
    for t in con.execute(q + " ORDER BY t.first_ts", params):
        print(f"\n{t['first_ts'][:10]} {t['session_id'][:8]} {t['project']}  [{t['status']}/{t['kind']}] {t['title']}")
        print(f"  {t['summary']}\n  “{t['quote']}”")
        for item in json.loads(t["loose_ends"] or "[]"):
            print(f"  - {item}")
        if t["next_step"]:
            print(f"  next: {t['next_step']}")


def cmd_ideas(args, con):
    data = report.collect(con, habits_data={})
    items = [report.compact(l, data) for l in data["lines"] if l["score"] > 0 and not l["mark"]]
    if args.q:
        words = args.q.lower().split()
        items = [i for i in items if all(w in json.dumps(i).lower() for w in words)]
    for i in items[:args.limit]:
        print(f"\n{i['title']}  [{i['status']}, {i['project']}, {i['sessions']} session(s), quiet {i['quiet_days']}d]")
        if i["verdict"]:
            print(f"  {i['verdict']}")
        for loose in i["loose_ends"]:
            print(f"  - {loose}")
        if i["resume"]:
            print(f"  {i['resume']}")
    if not items:
        print("no open ideas")


def cmd_report(args, con):
    print(report.render(con, args.out))


def cmd_serve(args, con):
    import uvicorn
    cfg = config.get()
    uvicorn.run("kifu.api:app", host=args.host or cfg.server_host, port=args.port or cfg.server_port)


def cmd_demo(args, con):
    from . import demo
    path = demo.build(args.dir)
    print(f"\nTry it:\n  KIFU_CONFIG={path} kifu serve\n  KIFU_CONFIG={path} kifu ideas")


def cmd_config(args, con):
    cfg = config.get()
    out = dataclasses.asdict(cfg)
    out["db_path"], out["archive_dir"] = cfg.db_path, cfg.archive_dir
    out["sources"] = [dataclasses.asdict(s) for s in cfg.source_list()]
    print(f"# {cfg.path or 'no config file: defaults (' + str(config.default_path()) + ')'}")
    print(json.dumps(out, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="kifu", description=__doc__)
    p.add_argument("--config", help="config file (default: $KIFU_CONFIG or ~/.config/kifu/config.toml)")
    p.add_argument("--db", help="database file (default: <data_dir>/kifu.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pull", help="copy every source's session files into the archive (never deletes)")
    s = sub.add_parser("scan", help="read the archive, or one folder, into the database")
    s.add_argument("root", nargs="?", help="scan this folder instead of the archive")
    s.add_argument("--host", help="host label for a folder scan")
    s.add_argument("--force", action="store_true", help="re-read files that did not change")
    sub.add_parser("embed", help="group turns into moves and embed them")
    for name, help_ in (("analyze", "list each session's threads and loose ends with a model"),
                        ("link", "follow the same idea across sessions"),
                        ("run", "pull, scan, embed, analyze and link: only new work costs model time")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--backend", choices=sorted(analyze.BACKENDS))
        s.add_argument("--workers", type=int)
        if name == "analyze":
            s.add_argument("--session", help="only sessions whose id starts with this")

    s = sub.add_parser("ideas", help="open ideas, most aji first")
    s.add_argument("q", nargs="?", help="words that must all appear")
    s.add_argument("--limit", type=int, default=15)
    s = sub.add_parser("sessions", help="list sessions")
    s.add_argument("--project")
    s = sub.add_parser("show", help="one session's turns")
    s.add_argument("session", help="id or id prefix")
    s.add_argument("--width", type=int, default=300)
    s = sub.add_parser("threads", help="analyzed threads")
    s.add_argument("--session")
    s.add_argument("--status", help="comma separated, e.g. proposed,started,parked")

    s = sub.add_parser("serve", help="the web app and API")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s = sub.add_parser("report", help="a static, self-contained HTML export")
    s.add_argument("--out", default="kifu.html")
    s = sub.add_parser("demo", help="build a store from synthetic sessions, no model calls")
    s.add_argument("dir")
    sub.add_parser("config", help="print the effective settings")

    args = p.parse_args(argv)
    if args.config:
        os.environ["KIFU_CONFIG"] = args.config
    if args.cmd in ("demo", "config"):
        con = None
    else:
        args.db = args.db or config.get().db_path
        con = db.connect(args.db)
    {"pull": cmd_pull, "scan": cmd_scan, "embed": cmd_embed, "analyze": cmd_analyze, "link": cmd_link,
     "run": cmd_run, "ideas": cmd_ideas, "sessions": cmd_sessions, "show": cmd_show, "threads": cmd_threads,
     "serve": cmd_serve, "report": cmd_report, "demo": cmd_demo, "config": cmd_config}[args.cmd](args, con)


if __name__ == "__main__":
    main()

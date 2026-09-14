"""`kifu reclassify`: find private conversation among ideas analyzed before the `personal` kind existed.

Chat assistants hear about more than work. Sessions analyzed now mark private or social conversation as kind
"personal" (never listed as open ideas); this looks over threads analyzed earlier, in batches of titles and
summaries rather than whole sessions, and moves the private ones there. Each thread is asked about once.
"""

from . import analyze, config, link

BATCH = 40

SCHEMA = {"type": "object", "properties": {"personal": {"type": "array", "items": {"type": "integer"}}},
          "required": ["personal"]}

SYSTEM = """You get short entries found in {user}'s conversations with AI assistants. Most are work: projects,
features, fixes, research, operations. Some are private or social: plans with family or friends, greetings,
small talk, feelings, the assistant's persona or relationship with {user}.
Return the numbers of the private or social entries only. When an entry mixes both, it is work.
JSON only."""


def reclassify(con, tools=None, backend=None, log=print):
    cfg = config.get()
    backend = backend or cfg.backend
    tools = tools or [t for t, w in cfg.tool_weights.items() if w < 1]
    marks = ",".join("?" * len(tools))
    rows = con.execute(f"""SELECT t.id, t.title, t.summary, t.quote FROM threads t JOIN sessions s ON s.id=t.session_id
                           WHERE s.tool IN ({marks}) AND t.kind != 'personal' AND COALESCE(t.reviewed, 0) = 0
                           ORDER BY t.id""", tools).fetchall()
    log(f"reviewing {len(rows)} threads from {', '.join(tools)}")
    system = analyze.system_prompt(SYSTEM)
    moved = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        user = "\n".join(f"{n}. {r['title']} — {(r['summary'] or '')[:220]} (said: {(r['quote'] or '')[:120]})"
                         for n, r in enumerate(batch, 1))
        out = analyze.BACKENDS[backend](system, user, schema=SCHEMA)
        personal = {batch[n - 1]["id"] for n in out.get("personal", []) if 1 <= n <= len(batch)}
        con.executemany("UPDATE threads SET kind='personal' WHERE id=?", [(tid,) for tid in personal])
        con.executemany("UPDATE threads SET reviewed=1 WHERE id=?", [(r["id"],) for r in batch])
        con.commit()
        moved += len(personal)
        log(f"  {min(i + BATCH, len(rows))}/{len(rows)}: {len(personal)} personal")
    if rows:
        link.build_lines(con, backend=backend, log=log)
    return moved

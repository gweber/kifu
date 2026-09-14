# Changelog

## 0.3.1 — 2026-09-14

- **Chat assistants rank lower:** `tool_weights` (Hermes at 0.5 by default). An idea that also lives in a coding
  agent keeps full weight. The web app and the Hermes tab filter by tool.
- **Private conversation is not an idea:** a new kind `personal`, set by the analyzer for new sessions and by
  `kifu reclassify` for older ones (batches of titles and summaries, each thread asked once). Personal ideas stay
  findable at the bottom and are hidden by default, because the classification can be wrong.
- **Only real projects in the filter:** sessions in `~`, `/tmp`, project roots or folders matched by
  `ignore_projects` (benchmarks, test runs) are ideas without a project; the timeline gathers them in one lane.
- **Fixes:** model refusals are recorded instead of retried on every run; verify explains ideas from sessions that
  recorded no working directory instead of failing on them.

## 0.3.0 — 2026-09-13

- **Claude Code integration** (`kifu install claude`, or the plugin in `claude-code-plugin/`): a SessionStart
  hook shows the project's open ideas; a SessionEnd hook queues the session for analysis two quiet minutes
  later; `kifu mcp` serves `kifu_ideas`, `kifu_idea`, `kifu_brief` and `kifu_mark`.
- **`kifu brief` / `kifu resume`:** pick an idea up in a new session that starts from a compact brief.
- **Messages sent while the assistant was working** are now read. They were skipped before — on the machines
  kifu was built on, 956 of them in 176 sessions, many of them new ideas.
- **Secrets are redacted** when scanning; `kifu redact` cleans an older database.
- **Corrections:** rename, merge and detach ideas in the web app; corrections survive rebuilds. Scores
  calibrate to your done/dismissed marks once there are enough of them.
- **Other coding agents:** sources of `kind` codex, gemini, cline, hermes and opencode. Each tool's history
  becomes the same sessions and turns, so ideas are followed across tools; resume commands open the tool a
  session came from. SQLite histories are read in place locally and copied with `.backup` from other machines.

## 0.2.0 — 2026-09-13

- **`kifu verify`** (also part of `kifu run`): checks open ideas against git on the machine each session ran
  on. Commits that touched an idea's files after it went quiet are shown on the idea, a model reads their
  messages against the loose ends, settled loose ends are struck through, and an idea whose loose ends all
  look settled drops down the ranking and out of the digest. The Hermes tab and `kifu_idea` show it too.
- **Marks follow their idea** when re-analysis moves its anchor: a mark remembers the idea's sessions and
  meaning, and a rebuild re-attaches it to the matching idea — or leaves it unattached rather than guessing.
- **Fix:** two ideas found in the same move shared an anchor, so marking one marked both. Anchors now carry
  a short hash of the idea's first title; existing marks are re-attached on the next `kifu link`.
- **Fix:** checks and judgements are written after all workers finish, instead of waiting on a lock the
  main loop held.

## 0.1.1 — 2026-09-13

- **Security:** the API refuses DNS rebinding (only loopback `Host` names, 421 otherwise) and
  cross-site writes (a foreign `Origin` on a request that changes something, 403). Before, a web page
  could read ideas and start jobs through a rebound domain. `[server] allowed_hosts` admits a proxy's
  host name.

## 0.1.0 — 2026-09-13

First release.

- **Pipeline:** pull sessions from any number of machines into an archive that outlives Claude Code's
  cleanup; scan prompts, reply excerpts and evidence of work (writes, commits, pushes, PRs, deploys,
  question dialogs, forks); analyze each session's ideas with `claude -p` or any OpenAI-compatible
  model; follow the same idea across sessions and score what is still open.
- **Web app:** open ideas with done/dismiss marks, a timeline per project, sessions with resume
  commands, and habits — rhythm, prompt modes, focus stretches, time to answer, juggling, questions
  left behind.
- **API** with OpenAPI docs, including a compact form for agents and a digest of ideas that went quiet.
- **Hermes plugin:** an Ideas dashboard tab, three agent tools, `/kifu`, and a weekly digest job.
- **`kifu demo`:** a synthetic store to try everything without real sessions or model calls.

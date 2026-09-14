# Changelog

## 0.4.0 — 2026-09-14

- **`kifu memory-check`** (and `/api/memory-check`): memory and CLAUDE.md files that name paths which are gone, memory
  files the MEMORY.md index does not load, and memory folders of projects that no longer exist, with where the
  project's files are written now. Paths of other machines are skipped (sources, `~/.ssh/config` hosts, `other_hosts`).
- **Journeys between tools:** an idea that went from one tool to another (a Hermes chat that became Claude Code
  work, Cline to Codex) shows its path on the card (`journey`), and the Habits tab counts each handoff with how many
  of those ideas shipped and how many are still open (`stats.handoffs`).
- **`kifu blame file[:line[-line]]`** (MCP `kifu_why`, `/api/blame`): git blame for intent. For each line, the session
  and your own prompt that wrote it (and the last real request when that prompt was "go on"), the idea it belonged to,
  and the commit. The edits in the sessions that wrote the file are replayed in order; when no edit text matches,
  the commit subject and then timing decide, and each answer says which. A whole file folds into sessions by lines.
- **Déjà vu** (`UserPromptSubmit` hook, `/api/dejavu`, `[dejavu]` settings): the first three substantive prompts of
  a session are compared with every earlier idea, and the closest go to Claude as context, which mentions one only
  when it is clearly what you are returning to. Similarity alone misjudges too often to show it to you directly
  (measured on 300 returns and 300 new ideas; see `dejavu.py`). Needs the running service; about 0.3 s per prompt.
  `kifu install claude` adds the hook.
- **Effort:** tokens per turn and session from Claude Code transcripts (each API response counted once; subagents
  count for their session; needs `kifu scan --force` once for older sessions). Each idea shows its active time and
  tokens, and the Habits tab shows where the time went by outcome, and the costliest ideas never finished.
- **`kifu rules`** (`/api/rules`): corrections you keep repeating ("no shims", "stop pausing"), grouped by one model
  call, worded as rules, checked against CLAUDE.md files and memory, with the file each would go in. A group needs 3
  prompts from 2 sessions, counted by kifu from the prompts, not taken from the model.
- **Decisions and promises** (`kifu notes`, `kifu decisions`, `kifu promises`, MCP `kifu_decisions`, `/api/decisions`,
  `/api/promises`): choices with their reasons, and what the assistant said it would do later, read from the turns
  whose replies use that wording. Open promises (made in an idea's last session) and decisions show on the idea and
  in the brief. `kifu run` includes the pass; the drain after a session end reads only the last two days.
- **Telegram digest with buttons** (`hermes kifu setup --buttons`): the Hermes plugin sends the digest from the gateway,
  one message per idea with Done, Dismiss and Brief buttons, answered only in the digest's chat. Replaces the
  text-only cron job. New endpoint `/api/lines/{anchor}/brief`.
- **Digest timing from your habits** (`/api/habits/slot`, `digest_schedule = "auto"`): the hour of the week you most
  often start coding sessions (chat tools left out), with the reason and the next time.
- Model prompts say to refer to the user by name or as they/them.

## 0.3.2 — 2026-09-14

- **`moved_paths`:** a project moved since its sessions ran keeps them — working directories and written file
  paths are mapped from the old prefix to the new one when scanning, so resume commands, project names and git
  checks follow the move. Session files are not changed.

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

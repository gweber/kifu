# Security

kifu reads your Claude Code sessions: every prompt you typed, the end of every reply, file paths,
commit messages. The questions that matter are where that goes, who can read it, and what the
content of a session can make kifu do.

## Reporting

Open an issue, or for anything that should not be public, a private security advisory on GitHub.

## What stays local

- **Session files are only read.** `kifu pull` copies them into an archive with rsync and never deletes;
  nothing writes back to `~/.claude`.
- **The database** (`~/.local/share/kifu/kifu.db`) holds prompts, reply excerpts, analyzed ideas and
  your marks. Treat it like the session files themselves.
- **The web app and API have no authentication.** `kifu serve` binds `127.0.0.1` by default. Do not
  bind it to a public interface, and do not put it behind a proxy without authentication in front.
  Anyone who reaches it can read your ideas and quotes, mark them, and start jobs — including `run`,
  which spends model time.
- **Binding to loopback is not enough on its own**, so kifu also refuses what a web page could do from
  your browser. *DNS rebinding:* a page can point its own domain at 127.0.0.1 and read a local API as
  if it were its own; such a request names that domain in `Host`, and kifu answers only `127.0.0.1`,
  `localhost` and `::1` (421 otherwise). *Cross-site writes:* a request that changes something is
  refused when the browser reports another `Origin` (403). Behind your own authenticating proxy, add
  its host name to `[server] allowed_hosts`.

## Secrets in sessions

Sessions are full of credentials: keys pasted into prompts, tokens in config files the assistant read back,
passwords in connection strings. kifu **redacts them when it scans**, before anything is stored — so the
database, the web app, the API, the embeddings and the analysis backend never see them. The session files and
the archive are not changed.

It recognises the common token formats (Anthropic, OpenAI, GitHub, GitLab, Slack, AWS, Google, Hugging Face,
Stripe, Telegram bot tokens, JWTs, private key blocks), credentials in URLs, bearer headers, and values
assigned to names like `API_KEY`, `password` or `client_secret` when the value looks like a secret rather than
code, a URL, a quantity or a word. Add your own formats with `redact_patterns`. A database filled by an older
version is cleaned with `kifu redact`.

Redaction is pattern matching: it catches what looks like a secret, not everything that is one. Treat the
database as sensitive anyway.

## What leaves the machine

- **Analysis** sends a digest of each session (your prompts and the end of each reply) to the backend
  you configure. With `backend = "claude"` that is Anthropic, through your own `claude` login — the
  same place the sessions came from. With `backend = "openai"` it is whatever endpoint you set; point it
  at a local server to keep everything on the machine.
- **Embeddings** go to the endpoint in `[embeddings]`: short idea summaries and move texts.
- **Nothing else.** No telemetry, no update checks.

## Commands kifu runs

- `kifu pull` runs `rsync` against each source, over `ssh` for remote ones.
- `kifu verify` runs `git rev-parse`, `git log` and an existence test for files, in the working directory of
  each session, on the machine the session came from (over `ssh` for remote sources). Directories and file
  paths come from session files, so they are passed as separate arguments and shell-quoted for `ssh`, never
  interpolated into a command string. `verify` only reads; it never changes a repository.
- Commit subjects found by `verify` go to the analysis backend, together with the idea's loose ends.

## Untrusted content

Session content is untrusted input: it includes web pages, tool output and files that passed through
a conversation. kifu treats it as data.

- The analyzer runs `claude -p` with **no tools** (`--tools ""`), no MCP servers, no settings and no
  skills, and `--no-session-persistence`. Text in a session cannot make it run commands, read files
  or reach the network. The worst a crafted session can do is distort its own summary.
- The web app escapes everything it renders; the Hermes plugin renders through React.
- Resume commands are shown for you to copy, never executed.

## The Hermes plugin

The plugin adds routes under `/api/plugins/kifu/` to the Hermes dashboard and forwards them to kifu on
loopback. **The dashboard's authentication is the perimeter**: wherever the dashboard is reachable, the
Ideas tab is too, including its Pull and Run buttons. The agent tools can read ideas and mark them;
they cannot start jobs.

# Changelog

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

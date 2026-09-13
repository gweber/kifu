"""Settings: defaults, overridden by a TOML file, overridden by environment variables.

The file is ~/.config/kifu/config.toml (or $KIFU_CONFIG). Every key is optional:

    user = "Ada"                         # how prompts to the analyzer name the person typing
    timezone = "Europe/Berlin"           # for rhythm and habits; default: the system's
    data_dir = "~/.local/share/kifu"     # database and session archive
    project_roots = ["~/code", "~/src"]  # stripped from a session's working directory to name its project
    confirmations = ["weiter", "auf geht's"]   # extra "go on" phrases in your language
    writing_note = "often writes in German and dictates"   # a hint for the analyzer
    automated_markers = ["-ci-runner-"]  # session paths containing these count as automated, not typed
    redact = true                        # remove secrets from session text when scanning (default)
    redact_patterns = ["corp-[0-9a-f]{32}"]   # extra regular expressions for your own token formats

    [[sources]]                          # where Claude Code keeps sessions; default: this machine only
    host = "laptop"
    path = "~/.claude/projects"
    [[sources]]
    host = "studio"
    ssh = "studio.local"                 # any ssh destination; rsync pulls over it
    path = "~/.claude/projects"

    [embeddings]                         # any OpenAI-compatible /v1/embeddings endpoint
    url = "http://localhost:11434/v1/embeddings"
    model = "bge-m3"

    [analysis]
    backend = "claude"                   # claude (headless `claude -p`) | openai (any compatible server)
    model = "sonnet"
    effort = "medium"
    workers = 6
    openai_url = "http://localhost:8000/v1/chat/completions"
    openai_model = "qwen3"
    openai_api_key_env = ""              # name of the variable that holds the key, if the server needs one

    [server]
    host = "127.0.0.1"
    port = 8765
    allowed_hosts = ["kifu.example.org"]   # extra Host names, only behind a proxy that authenticates
"""
from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _path(value: str) -> str:
    return os.path.expanduser(value)


@dataclass
class Source:
    host: str
    path: str
    ssh: str | None = None


@dataclass
class Config:
    user: str = "the user"
    timezone: str = ""
    data_dir: str = _path("~/.local/share/kifu")
    project_roots: list[str] = field(default_factory=lambda: ["~/code", "~/src", "~/projects"])
    confirmations: list[str] = field(default_factory=list)
    writing_note: str = ""
    automated_markers: list[str] = field(default_factory=list)
    redact: bool = True
    redact_patterns: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    embed_url: str = "http://localhost:11434/v1/embeddings"
    embed_model: str = "bge-m3"
    backend: str = "claude"
    model: str = "sonnet"
    effort: str = "medium"
    workers: int = 6
    openai_url: str = "http://localhost:8000/v1/chat/completions"
    openai_model: str = "qwen3"
    openai_api_key_env: str = ""
    server_host: str = "127.0.0.1"
    server_port: int = 8765
    allowed_hosts: list[str] = field(default_factory=list)
    demo: bool = False
    path: str | None = None

    @property
    def db_path(self) -> str:
        return os.environ.get("KIFU_DB") or os.path.join(self.data_dir, "kifu.db")

    @property
    def archive_dir(self) -> str:
        return os.path.join(self.data_dir, "archive")

    @property
    def tz(self):
        if self.timezone:
            return ZoneInfo(self.timezone)
        return None          # system local time

    def source_list(self) -> list[Source]:
        return self.sources or [Source(host=socket.gethostname(), path="~/.claude/projects")]


def default_path() -> Path:
    return Path(os.environ.get("KIFU_CONFIG") or _path("~/.config/kifu/config.toml"))


def load(path: str | os.PathLike | None = None) -> Config:
    p = Path(path) if path else default_path()
    raw: dict = {}
    if p.exists():
        with open(p, "rb") as fh:
            raw = tomllib.load(fh)
    emb = raw.get("embeddings", {})
    ana = raw.get("analysis", {})
    srv = raw.get("server", {})
    cfg = Config(
        user=raw.get("user", Config.user),
        timezone=raw.get("timezone", ""),
        data_dir=_path(raw.get("data_dir", Config.data_dir)),
        project_roots=list(raw["project_roots"]) if "project_roots" in raw else Config().project_roots,
        confirmations=list(raw.get("confirmations", [])),
        writing_note=raw.get("writing_note", ""),
        automated_markers=list(raw.get("automated_markers", [])),
        redact=bool(raw.get("redact", True)),
        redact_patterns=list(raw.get("redact_patterns", [])),
        sources=[Source(host=s["host"], path=s.get("path", "~/.claude/projects"), ssh=s.get("ssh"))
                 for s in raw.get("sources", [])],
        embed_url=emb.get("url", Config.embed_url),
        embed_model=emb.get("model", Config.embed_model),
        backend=ana.get("backend", Config.backend),
        model=ana.get("model", Config.model),
        effort=ana.get("effort", Config.effort),
        workers=int(ana.get("workers", Config.workers)),
        openai_url=ana.get("openai_url", Config.openai_url),
        openai_model=ana.get("openai_model", Config.openai_model),
        openai_api_key_env=ana.get("openai_api_key_env", ""),
        server_host=srv.get("host", Config.server_host),
        server_port=int(srv.get("port", Config.server_port)),
        allowed_hosts=list(srv.get("allowed_hosts", [])),
        demo=bool(raw.get("demo", False)),
        path=str(p) if p.exists() else None,
    )
    env = os.environ
    cfg.data_dir = _path(env.get("KIFU_DATA_DIR", cfg.data_dir))
    cfg.embed_url = env.get("KIFU_EMBED_URL", cfg.embed_url)
    cfg.embed_model = env.get("KIFU_EMBED_MODEL", cfg.embed_model)
    cfg.backend = env.get("KIFU_BACKEND", cfg.backend)
    cfg.model = env.get("KIFU_CLAUDE_MODEL", cfg.model)
    cfg.effort = env.get("KIFU_CLAUDE_EFFORT", cfg.effort)
    cfg.openai_url = env.get("KIFU_LLM_URL", cfg.openai_url)
    cfg.openai_model = env.get("KIFU_LLM_MODEL", cfg.openai_model)
    return cfg


_current: Config | None = None


def get() -> Config:
    """The process-wide config, loaded once."""
    global _current
    if _current is None:
        _current = load()
    return _current


def set_current(cfg: Config) -> None:
    """For tests and the demo: use this config instead of the file."""
    global _current
    _current = cfg

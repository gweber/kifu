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
    ignore_projects = [".bench*", "tmp-*"]   # top-level folders that are not projects (added to ?, ~, /tmp*, _*)
    tool_weights = { hermes = 0.5 }      # chat assistants' ideas rank lower; a line keeps its highest tool's weight
    moved_paths = { "/home/ada/src/old" = "/home/ada/code/new" }   # projects moved since: old prefix = new prefix
    other_hosts = ["nas"]                 # machine nicknames besides sources and ~/.ssh/config (memory-check)
    redact_patterns = ["corp-[0-9a-f]{32}"]   # extra regular expressions for your own token formats

    [[sources]]                          # where Claude Code keeps sessions; default: this machine only
    host = "laptop"
    path = "~/.claude/projects"
    [[sources]]
    host = "studio"
    ssh = "studio.local"                 # any ssh destination; rsync pulls over it
    path = "~/.claude/projects"
    [[sources]]
    host = "studio"
    ssh = "studio.local"
    kind = "codex"                       # codex, gemini, cline, hermes, opencode; path defaults per kind

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

    [dejavu]                             # Claude hears about earlier ideas a new session's prompts resemble
    prompts = 3                          # substantive prompts checked per session; 0 turns it off
    threshold = 0.5                      # cosine similarity for a candidate (Claude judges whether it is the same)
    top = 3

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

# Where each tool keeps its history by default (see agents.py).
DEFAULT_PATHS = {
    "claude": "~/.claude/projects",
    "codex": "~/.codex/sessions",
    "gemini": "~/.gemini/tmp",
    "cline": "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev",
    "hermes": "~/.hermes/state.db",
    "opencode": "~/.local/share/opencode/opencode.db",
}


def _path(value: str) -> str:
    return os.path.expanduser(value)


@dataclass
class Source:
    host: str
    path: str
    ssh: str | None = None
    kind: str = "claude"        # claude, codex, gemini, cline, hermes, opencode


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
    ignore_projects: list[str] = field(default_factory=list)
    tool_weights: dict = field(default_factory=lambda: {"hermes": 0.5})
    moved_paths: dict = field(default_factory=dict)
    other_hosts: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    embed_url: str = "http://localhost:11434/v1/embeddings"
    embed_model: str = "bge-m3"
    embed_api_key: str = ""              # literal key for the embeddings endpoint (e.g. a LiteLLM virtual key)
    backend: str = "claude"
    model: str = "sonnet"
    effort: str = "medium"
    workers: int = 6
    openai_url: str = "http://localhost:8000/v1/chat/completions"
    openai_model: str = "qwen3"
    openai_api_key_env: str = ""
    dejavu_prompts: int = 3
    dejavu_threshold: float = 0.5
    dejavu_top: int = 3
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
    dv = raw.get("dejavu", {})
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
        ignore_projects=list(raw.get("ignore_projects", [])),
        tool_weights={"hermes": 0.5, **raw.get("tool_weights", {})},
        moved_paths={_path(k): _path(v) for k, v in raw.get("moved_paths", {}).items()},
        other_hosts=list(raw.get("other_hosts", [])),
        sources=[Source(host=s["host"], kind=s.get("kind", "claude"), ssh=s.get("ssh"),
                        path=s.get("path") or DEFAULT_PATHS.get(s.get("kind", "claude"), "~/.claude/projects"))
                 for s in raw.get("sources", [])],
        embed_url=emb.get("url", Config.embed_url),
        embed_model=emb.get("model", Config.embed_model),
        embed_api_key=emb.get("api_key", Config.embed_api_key),
        backend=ana.get("backend", Config.backend),
        model=ana.get("model", Config.model),
        effort=ana.get("effort", Config.effort),
        workers=int(ana.get("workers", Config.workers)),
        openai_url=ana.get("openai_url", Config.openai_url),
        openai_model=ana.get("openai_model", Config.openai_model),
        openai_api_key_env=ana.get("openai_api_key_env", ""),
        dejavu_prompts=int(dv.get("prompts", Config.dejavu_prompts)),
        dejavu_threshold=float(dv.get("threshold", Config.dejavu_threshold)),
        dejavu_top=int(dv.get("top", Config.dejavu_top)),
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
    cfg.embed_api_key = env.get("KIFU_EMBED_API_KEY", cfg.embed_api_key)
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

"""Remove secrets from session text before it is stored, embedded, analyzed or shown.

Sessions are full of credentials: keys pasted into prompts, tokens in config files the assistant read back,
passwords in connection strings. kifu redacts them when it scans, so the database — and everything built on
it, from the web app to the analysis backend — never holds them. The session files themselves stay untouched.

Two kinds of rules: token formats that are recognisable on their own (sk-ant-…, ghp_…, AKIA…, JWTs), and
values assigned to names that say "secret" (API_KEY=…, "password": "…"). Placeholders like ${API_KEY} or
<your-token> are left alone, and so are git hashes and UUIDs: over-redaction would make the text useless.
"""
import re

from . import config

# (kind, pattern) for tokens recognisable by their shape. Order matters: specific before generic.
TOKENS = [
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    ("openai-key", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
    ("stripe-key", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
    ("gitlab-token", r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    ("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("huggingface-token", r"\bhf_[A-Za-z0-9]{30,}\b"),
    ("telegram-bot-token", r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
]

_SECRET_NAME = r"[A-Za-z0-9_\-]*(?:password|passwd|passphrase|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key|client[_\-]?secret|auth[_\-]?key)[A-Za-z0-9_\-]*"
_PLACEHOLDER = re.compile(r"^(?:\$\{?|<|\{\{|%\(|x{4,}|\*{3,}|your[_\-]|changeme|example|placeholder|redacted|none|null|true|false|\[)", re.I)

ASSIGNMENTS = [
    # KEY=value, key: value (env files, YAML, shell, CLI flags)
    re.compile(rf"(?i)\b({_SECRET_NAME})(\s*[:=]\s*|\s+)(['\"]?)([^\s'\"`,;]{{8,}})\3"),
    # "key": "value" (JSON)
    re.compile(rf"(?i)(\"{_SECRET_NAME}\")(\s*:\s*)(\")([^\"]{{8,}})\""),
]
URL_CREDENTIALS = re.compile(r"\b([a-z][a-z0-9+.\-]*://[^\s:/@]+):([^\s/@]{3,})@", re.I)
BEARER = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_\-.=]{20,})")

_compiled = None


def _tokens():
    global _compiled
    if _compiled is None:
        extra = [("custom", p) for p in config.get().redact_patterns]
        _compiled = [(kind, re.compile(p, re.S)) for kind, p in TOKENS + extra]
    return _compiled


def redact(text):
    """The text with secrets replaced by [redacted:<kind>]. Returns the input unchanged when redaction is off."""
    if not text or not config.get().redact:
        return text
    for kind, pattern in _tokens():
        text = pattern.sub(f"[redacted:{kind}]", text)
    text = URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}:[redacted:password]@", text)
    text = BEARER.sub(lambda m: f"{m.group(1)}[redacted:bearer-token]", text)
    for pattern in ASSIGNMENTS:
        text = pattern.sub(_assignment, text)
    return text


_CODE_OR_URL = re.compile(r"[()\[\]{}<>]|://|^(?:self|this|os|env|process|config|settings|args)\.", re.I)


_QUANTITY = re.compile(r"[~≈]?[\d.,:]+\s*[kKmMbB%]?(?:\s*[–\-]\s*[~≈]?[\d.,:]+\s*[kKmMbB%]?)?")


def looks_secret(value):
    """A value assigned to a secret-sounding name, that also looks like a secret rather than code, a URL or a word."""
    if _PLACEHOLDER.match(value) or value.startswith("[redacted:") or _CODE_OR_URL.search(value):
        return False
    if _QUANTITY.fullmatch(value.strip("*_`")):
        return False          # 22:30, ~600k–1.3M, 27.564: token counts and times, not tokens
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", value) and not re.search(r"[0-9]", value):
        return False          # identifiers and plain words: threshold_value, required, config.token_name
    return bool(re.search(r"[0-9]", value) or re.search(r"[^A-Za-z0-9_.\-/]", value)
                or (len(value) >= 20 and re.search(r"[a-z]", value) and re.search(r"[A-Z]", value)))


def _assignment(m):
    value = m.group(4)
    if not looks_secret(value):
        return m.group(0)
    if m.group(2).strip() == "" and not (len(value) >= 16 and re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value)):
        return m.group(0)          # "token counts" and other prose: after a bare space only a long, random-looking value
    return f"{m.group(1)}{m.group(2)}{m.group(3)}[redacted:secret]{m.group(3)}"


# Text columns that can carry session content, for scrubbing a database filled before redaction existed.
COLUMNS = {
    "sessions": ["title", "ai_title"],
    "turns": ["prompt", "reply"],
    "evidence": ["value"],
    "moves": ["prompts", "reply_tail"],
    "threads": ["title", "summary", "quote", "next_step", "loose_ends"],
    "lines": ["title", "summary", "verdict", "next_step", "loose_ends"],
    "checks": ["commits", "note"],
}


def scrub_database(con, log=print):
    """Redact every stored text column in place. Idempotent; returns the number of changed values."""
    changed = 0
    for table, columns in COLUMNS.items():
        rows = con.execute(f"SELECT rowid AS rid, {', '.join(columns)} FROM {table}").fetchall()
        for row in rows:
            updates = {c: redact(row[c]) for c in columns if row[c] and redact(row[c]) != row[c]}
            if updates:
                sets = ", ".join(f"{c}=?" for c in updates)
                con.execute(f"UPDATE {table} SET {sets} WHERE rowid=?", (*updates.values(), row["rid"]))
                changed += len(updates)
    con.commit()
    log(f"redacted {changed} stored values")
    return changed

"""kifu: find the ideas you left behind in your Claude Code sessions."""
try:
    from importlib.metadata import version as _version

    __version__ = _version("kifu")
except Exception:  # running from a checkout without an installed package
    __version__ = "0+unknown"

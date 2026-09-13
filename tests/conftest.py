import datetime as dt
import shutil

import pytest

from kifu import api, config, db, demo, embed

NOW = dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC)


def use(cfg_path):
    cfg = config.load(cfg_path)
    config.set_current(cfg)
    embed._confirm_re = None          # compiled from the config's confirmations
    api._cache.clear()
    demo.install()
    return cfg


@pytest.fixture(scope="session")
def demo_template(tmp_path_factory):
    """One fully built demo store; tests get a fresh copy."""
    root = tmp_path_factory.mktemp("demo-template")
    demo.build(str(root), log=lambda m: None, now=NOW)
    return root


@pytest.fixture
def store(demo_template, tmp_path, monkeypatch):
    root = tmp_path / "demo"
    shutil.copytree(demo_template, root)
    cfg_path = root / "config.toml"
    text = cfg_path.read_text().replace(str(demo_template), str(root))
    cfg_path.write_text(text)
    # Subprocesses (jobs, hooks, the MCP server) read the environment: they must see the demo store, never the
    # config of the machine running the tests.
    monkeypatch.setenv("KIFU_CONFIG", str(cfg_path))
    monkeypatch.setenv("KIFU_DB", "")
    cfg = use(cfg_path)
    con = db.connect(cfg.db_path)
    yield cfg, con
    con.close()

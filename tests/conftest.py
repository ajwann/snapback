import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return home


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def run_hook(event: dict, env_home: Path) -> dict:
    """Run scripts/hook.py as Claude Code would, return parsed stdout (or {})."""
    import os
    env = dict(os.environ, HOME=str(env_home))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hook.py")], input=json.dumps(event),
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else {}

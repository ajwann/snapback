"""Manifest checks: plugin.json is complete, hooks point at real files, bin/ is executable."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_json_complete():
    m = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    for key in ("name", "version", "description", "author", "license", "homepage", "repository"):
        assert m.get(key), key
    assert m["name"] == ROOT.name


def test_hooks_reference_existing_scripts_and_exec_form():
    hooks = ROOT / "hooks" / "hooks.json"
    if not hooks.exists():
        return
    data = json.loads(hooks.read_text())
    assert "hooks" in data
    for groups in data["hooks"].values():
        for g in groups:
            for h in g["hooks"]:
                assert h["command"] == "python3"
                script = h["args"][0].replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
                assert Path(script).is_file(), script


def test_bin_scripts_executable():
    for b in (ROOT / "bin").glob("*") if (ROOT / "bin").exists() else []:
        assert b.stat().st_mode & 0o111, b


def test_license_and_readme():
    assert (ROOT / "LICENSE").is_file()
    assert len((ROOT / "README.md").read_text().split()) >= 40

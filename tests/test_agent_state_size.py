"""agent_state.json publishes that exceed the cap must not replace the file."""

import json

import agent_config as cfg
from account_sync import save_state_file
from agenttrade.publish import publish_dashboard_state
from cycle import _save_state


def _point_state_files(monkeypatch, tmp_path):
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    state_file = tmp_path / "agent_state.json"
    public_file = public_dir / "agent_state.json"
    state_file.write_text('{"keep": true}', encoding="utf-8")
    public_file.write_text('{"keep": true}', encoding="utf-8")
    monkeypatch.setattr(cfg, "APP_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(cfg, "PUBLIC_DASHBOARD_DIR", str(public_dir), raising=False)
    monkeypatch.setattr(cfg, "STATE_FILE", str(state_file), raising=False)
    monkeypatch.setattr(cfg, "PUBLIC_STATE_FILE", str(public_file), raising=False)
    return state_file, public_file


def test_default_cap_is_eight_mib():
    assert cfg.DEFAULT_AGENT_STATE_MAX_BYTES == 8 * 1024 * 1024


def test_invalid_cap_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AGENT_STATE_MAX_BYTES", "nope")
    assert cfg.agent_state_max_bytes() == cfg.DEFAULT_AGENT_STATE_MAX_BYTES


def test_small_projection_is_written(tmp_path, monkeypatch):
    state_file, public_file = _point_state_files(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_STATE_MAX_BYTES", "100000")
    publish_dashboard_state({"ok": 1, "trade_candidates": []})
    assert json.loads(state_file.read_text(encoding="utf-8"))["ok"] == 1
    assert json.loads(public_file.read_text(encoding="utf-8"))["ok"] == 1


def test_oversized_publish_does_not_replace_either_copy(tmp_path, monkeypatch):
    state_file, public_file = _point_state_files(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_STATE_MAX_BYTES", "80")
    blob = {"blob": "x" * 200, "trade_candidates": [], "blocked_ideas": []}
    publish_dashboard_state(blob)
    save_state_file(blob)
    _save_state(blob)
    assert state_file.read_text(encoding="utf-8") == '{"keep": true}'
    assert public_file.read_text(encoding="utf-8") == '{"keep": true}'
    assert cfg.dumps_agent_state(blob) is None

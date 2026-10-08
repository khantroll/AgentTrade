"""Deploy must not reset a live ledger or replace runtime files."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _cycles(db):
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT id, mode, status FROM cycle_runs ORDER BY id")]


def test_migrate_leaves_existing_equity_halt_and_history(tmp_path, monkeypatch):
    import agent_config as cfg
    from agenttrade import db as ledger
    from agenttrade.migrate_state import migrate_state

    monkeypatch.setattr(cfg, "APP_DIR", str(tmp_path))
    state_path = tmp_path / "agent_state.json"
    state_path.write_text(json.dumps({
        "equity": 105910,
        "portfolio_value": 105910,
        "cash": 12451,
        "trading_halted": False,
        "positions": [{"symbol": "AAPL", "qty": 1}],
        "decisions": [{"ticker": "AAPL", "action": "BUY", "confidence": 0.5}],
    }), encoding="utf-8")

    ledger.init_db()
    ledger.set_system_flag("STARTING_EQUITY", "50000")
    ledger.set_system_flag("HIGH_WATER_EQUITY", "80000")
    ledger.set_system_flag("TRADING_HALTED", "true")
    cycle_id = ledger.start_cycle_run("paper")
    ledger.finish_cycle_run(cycle_id, "completed", "real cycle")

    result = migrate_state(dry_run=False)
    assert result["ok"] is True
    assert result["skipped"] is True
    assert ledger.get_system_flag("STARTING_EQUITY") == "50000"
    assert ledger.get_system_flag("HIGH_WATER_EQUITY") == "80000"
    assert ledger.get_system_flag("TRADING_HALTED") == "true"
    cycles = _cycles(ledger)
    assert [row["mode"] for row in cycles] == ["paper"]
    assert "migration" not in {row["mode"] for row in cycles}

    dry = migrate_state(dry_run=True)
    assert dry["skipped"] is True
    assert not list(tmp_path.glob("agent_state.backup.*.json"))


def test_first_migrate_imports_once_and_a_second_call_does_not_reset(tmp_path, monkeypatch):
    import agent_config as cfg
    from agenttrade import db as ledger
    from agenttrade.migrate_state import migrate_state

    monkeypatch.setattr(cfg, "APP_DIR", str(tmp_path))
    state_path = tmp_path / "agent_state.json"
    state_path.write_text(json.dumps({
        "equity": 40000,
        "portfolio_value": 40000,
        "cash": 40000,
        "positions": [{"symbol": "MSFT", "qty": 2}],
        "decisions": [{"ticker": "MSFT", "action": "BUY"}],
    }), encoding="utf-8")

    first = migrate_state(dry_run=False)
    assert first["ok"] is True
    assert first["skipped"] is False
    assert ledger.get_system_flag("STARTING_EQUITY") == "40000"
    assert ledger.get_system_flag("HIGH_WATER_EQUITY") == "40000"
    assert ledger.get_system_flag("TRADING_HALTED") == "false"
    assert [row["mode"] for row in _cycles(ledger)] == ["migration"]

    state_path.write_text(json.dumps({
        "equity": 105910,
        "portfolio_value": 105910,
        "cash": 12451,
        "positions": [{"symbol": "NVDA", "qty": 1}],
        "decisions": [{"ticker": "NVDA", "action": "BUY"}],
    }), encoding="utf-8")
    ledger.set_system_flag("TRADING_HALTED", "true")
    second = migrate_state(dry_run=False)
    assert second["skipped"] is True
    assert ledger.get_system_flag("STARTING_EQUITY") == "40000"
    assert ledger.get_system_flag("HIGH_WATER_EQUITY") == "40000"
    assert ledger.get_system_flag("TRADING_HALTED") == "true"
    assert [row["mode"] for row in _cycles(ledger)] == ["migration"]


def _run_deploy(args):
    proc = subprocess.run(
        ["bash", str(ROOT / "update_deploy.sh"), *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    return proc


def test_dry_run_writes_nothing(tmp_path):
    app = tmp_path / "missing-app"
    proc = _run_deploy([
        "-y", "--dry-run", "--local-only",
        "--source", str(ROOT),
        "--app-dir", str(app),
        "--skip-pip", "--skip-verify", "--skip-tests",
    ])
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "No files were written" in proc.stdout
    assert "SQLite migration: not run" in proc.stdout
    assert not app.exists()


def test_deploy_skips_inplace_copy_and_keeps_runtime_files(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "agenttrade").mkdir()
    (app / ".env").write_text("SECRET=live\nALPACA_PAPER=true\n", encoding="utf-8")
    (app / "agent_state.json").write_text('{"equity": 1, "marker": "live"}', encoding="utf-8")
    (app / "llm_health.json").write_text('{"groq": {"cooldown_until": 1}}', encoding="utf-8")
    (app / "config.json").write_text('{"ALPACA_PAPER": "true"}', encoding="utf-8")
    (app / "bucket_tags.json").write_text('{"AAPL": "Growth"}', encoding="utf-8")
    (app / "trade_log.jsonl").write_text("LIVE-TRADE\n", encoding="utf-8")
    (app / "trading_agent.log").write_text("LIVE-LOG\n", encoding="utf-8")
    (app / "agenttrade.sqlite3").write_bytes(b"SQLITE-LIVE")

    first = _run_deploy([
        "-y", "--local-only",
        "--source", str(ROOT),
        "--app-dir", str(app),
        "--skip-pip", "--skip-verify", "--skip-tests",
    ])
    assert first.returncode == 0, first.stderr + first.stdout
    assert "SQLite migration not run" in first.stdout
    assert "SECRET=live" in (app / ".env").read_text(encoding="utf-8")
    assert (app / "agent_state.json").read_text(encoding="utf-8") == '{"equity": 1, "marker": "live"}'
    assert (app / "llm_health.json").read_text(encoding="utf-8").startswith('{"groq"')
    assert (app / "trade_log.jsonl").read_text(encoding="utf-8") == "LIVE-TRADE\n"
    assert (app / "trading_agent.log").read_text(encoding="utf-8") == "LIVE-LOG\n"
    assert (app / "agenttrade.sqlite3").read_bytes() == b"SQLITE-LIVE"
    assert (app / "cycle.py").is_file()
    backups = list((app / "backups").glob("update-*"))
    assert backups
    backup = backups[0]
    assert (backup / "config.json").read_text(encoding="utf-8").startswith("{")
    assert (backup / "llm_health.json").read_text(encoding="utf-8").startswith('{"groq"')
    assert "Growth" in (backup / "bucket_tags.json").read_text(encoding="utf-8")
    assert (backup / "trade_log.jsonl").read_text(encoding="utf-8") == "LIVE-TRADE\n"
    script = (ROOT / "update_deploy.sh").read_text(encoding="utf-8")
    assert "grep -q reconciliation" not in script
    assert "Pre-Cycle Health" in script
    assert "js/ directory not found" not in script

    second = _run_deploy([
        "-y", "--local-only",
        "--source", str(app),
        "--app-dir", str(app),
        "--skip-pip", "--skip-verify", "--skip-tests",
    ])
    assert second.returncode == 0, second.stderr + second.stdout
    assert "same file" in second.stdout
    assert (app / "trade_log.jsonl").read_text(encoding="utf-8") == "LIVE-TRADE\n"
    assert (app / "trading_agent.log").read_text(encoding="utf-8") == "LIVE-LOG\n"
    assert (app / "agenttrade.sqlite3").read_bytes() == b"SQLITE-LIVE"

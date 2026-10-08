"""The deploy file set is the repo, and a broken import fails the deploy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _copy_file_set(dest: Path, files: list[str], *, skip: set[str] | None = None) -> None:
    skipped = skip or set()
    for rel in files:
        if rel in skipped:
            continue
        source = ROOT / rel
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def test_file_set_covers_cycle_and_screener_imports():
    from deploy_manifest import deploy_file_list, is_runtime_path, transitive_local_modules

    files = set(deploy_file_list(ROOT))
    needed = transitive_local_modules(ROOT, ["cycle", "screener"])
    missing = sorted(needed - files)
    assert missing == [], missing
    for rel in (
        "cycle.py",
        "screener.py",
        "trading_day.py",
        "market_data.py",
        "performance_history.py",
        "health_check.py",
        "opportunity_rebalance.py",
        "settings.html",
        "deploy_manifest.py",
    ):
        assert rel in files
    for banned in (
        ".env",
        "agent_state.json",
        "llm_health.json",
        "token_usage.json",
        "config.json",
        "bucket_tags.json",
        "trade_log.jsonl",
        "performance_history.jsonl",
        "trading_agent.log",
        "cron.log",
    ):
        assert banned not in {Path(path).name for path in files}
        assert is_runtime_path(banned)


def test_file_list_from_a_copied_tree_without_git(tmp_path):
    from deploy_manifest import deploy_file_list

    dest = tmp_path / "copy"
    files = deploy_file_list(ROOT)
    _copy_file_set(dest, files)
    assert not (dest / ".git").exists()
    listed = set(deploy_file_list(dest))
    assert "cycle.py" in listed
    assert "screener.py" in listed
    assert "trading_day.py" in listed
    assert "market_data.py" in listed
    assert "settings.html" in listed
    assert "opportunity_rebalance.py" in listed
    assert ".env" not in {Path(path).name for path in listed}
    assert not any(".sqlite3" in path for path in listed)


def test_import_smoke_on_computed_file_set(tmp_path):
    from deploy_manifest import deploy_file_list

    dest = tmp_path / "app"
    files = deploy_file_list(ROOT)
    _copy_file_set(dest, files)
    db_path = tmp_path / "smoke.sqlite3"
    env = os.environ.copy()
    env["AGENTTRADE_DB_PATH"] = str(db_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(dest)
    proc = subprocess.run(
        [sys.executable, str(dest / "deploy_manifest.py"), "--import-smoke", "--root", str(dest)],
        cwd=str(tmp_path),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "import smoke ok" in proc.stdout
    assert not (dest / "trading_agent.log").exists()
    assert not (dest / "agenttrade.sqlite3").exists()


def test_import_smoke_fails_without_trading_day(tmp_path):
    from deploy_manifest import deploy_file_list

    dest = tmp_path / "app"
    _copy_file_set(dest, deploy_file_list(ROOT), skip={"trading_day.py"})
    env = os.environ.copy()
    env["AGENTTRADE_DB_PATH"] = str(tmp_path / "smoke.sqlite3")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(dest)
    proc = subprocess.run(
        [sys.executable, str(dest / "deploy_manifest.py"), "--import-smoke", "--root", str(dest)],
        cwd=str(tmp_path),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "trading_day" in proc.stderr


def test_failed_import_smoke_rolls_back_and_keeps_runtime_files(tmp_path):
    from deploy_manifest import deploy_file_list

    source = tmp_path / "src"
    _copy_file_set(source, deploy_file_list(ROOT), skip={"trading_day.py"})
    app = tmp_path / "app"
    app.mkdir()
    (app / "cycle.py").write_text("# previous cycle\n", encoding="utf-8")
    (app / ".env").write_text("SECRET=live\nALPACA_PAPER=true\n", encoding="utf-8")
    (app / "trade_log.jsonl").write_text("LIVE-TRADE\n", encoding="utf-8")
    (app / "agenttrade.sqlite3").write_bytes(b"SQLITE-LIVE")

    proc = subprocess.run(
        [
            "bash", str(ROOT / "update_deploy.sh"),
            "-y", "--local-only",
            "--source", str(source),
            "--app-dir", str(app),
            "--skip-pip", "--skip-verify", "--skip-tests",
        ],
        cwd=str(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "rolled back" in output
    assert (app / "cycle.py").read_text(encoding="utf-8") == "# previous cycle\n"
    assert not (app / "screener.py").exists()
    assert (app / ".env").read_text(encoding="utf-8").startswith("SECRET=live")
    assert (app / "trade_log.jsonl").read_text(encoding="utf-8") == "LIVE-TRADE\n"
    assert (app / "agenttrade.sqlite3").read_bytes() == b"SQLITE-LIVE"
    script = (ROOT / "update_deploy.sh").read_text(encoding="utf-8")
    assert "--import-smoke" in script
    assert "UPDATE_PY" not in script
    assert "Pre-Cycle Health" in script

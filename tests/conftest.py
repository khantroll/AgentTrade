"""Isolate tests from the recovered SQLite fixture and missing third-party packages."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock

import pytest

# Bind a throwaway ledger before any test module can import agenttrade.db.
# Never point at the recovered archive fixture agenttrade.sqlite3.
os.environ["AGENTTRADE_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="agenttrade_precollect_"),
    "precollect.sqlite3",
)
os.environ.setdefault("ALLOW_MARGIN", "false")
os.environ.setdefault("ALLOW_NEGATIVE_CASH", "false")
os.environ.setdefault("ALPACA_PAPER", "true")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECOVERED_SQLITE = os.path.join(REPO_ROOT, "agenttrade.sqlite3")
RECOVERED_SQLITE_SHA256 = "c2841e3c33719229f643746130b4f64f10c564626aa56d1e607bcd97bb39c1b6"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class _FakeHTTPError(Exception):
    """Stand-in for requests.HTTPError so alpaca_client can subclass it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self.response = kwargs.get("response")
        self.request = kwargs.get("request")


def _stub_if_missing(name: str, module=None) -> None:
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = module if module is not None else MagicMock()


def _stub_requests() -> None:
    try:
        __import__("requests")
        return
    except ImportError:
        pass
    fake = types.ModuleType("requests")
    fake.HTTPError = _FakeHTTPError
    fake.Response = MagicMock
    fake.get = MagicMock()
    fake.post = MagicMock()
    fake.put = MagicMock()
    fake.delete = MagicMock()
    fake.Session = MagicMock
    fake.exceptions = types.SimpleNamespace(
        RequestException=Exception,
        HTTPError=_FakeHTTPError,
        Timeout=Exception,
        ConnectionError=Exception,
    )
    sys.modules["requests"] = fake


_stub_requests()
for _mod in (
    "dotenv",
    "schedule",
    "yfinance",
    "vaderSentiment",
    "vaderSentiment.vaderSentiment",
    "flask",
    "flask_cors",
    "anthropic",
    "openai",
):
    _stub_if_missing(_mod)


@pytest.fixture(scope="session", autouse=True)
def _never_mutate_recovered_sqlite():
    if not os.path.isfile(RECOVERED_SQLITE):
        yield
        return
    before = _sha256(RECOVERED_SQLITE)
    size = os.path.getsize(RECOVERED_SQLITE)
    assert before == RECOVERED_SQLITE_SHA256, (
        "Recovered agenttrade.sqlite3 hash does not match SHA256SUMS.txt; "
        "refusing to run tests against a mutated fixture."
    )
    yield
    assert _sha256(RECOVERED_SQLITE) == before
    assert os.path.getsize(RECOVERED_SQLITE) == size


@pytest.fixture(autouse=True)
def _isolate_agenttrade_runtime(tmp_path, monkeypatch):
    db_path = tmp_path / "test_ledger.sqlite3"
    monkeypatch.setenv("AGENTTRADE_DB_PATH", str(db_path))
    monkeypatch.setenv("ALLOW_MARGIN", "false")
    monkeypatch.setenv("ALLOW_NEGATIVE_CASH", "false")
    monkeypatch.setenv("MAX_CONSECUTIVE_LOSSES", "5")
    monkeypatch.setenv("MAX_RISK_PER_TRADE_PCT", "0.005")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")

    import agent_config as cfg

    monkeypatch.setattr(cfg, "STATE_FILE", str(tmp_path / "agent_state.json"), raising=False)
    monkeypatch.setattr(cfg, "PUBLIC_DASHBOARD_DIR", str(tmp_path / "public"), raising=False)
    monkeypatch.setattr(cfg, "PUBLIC_STATE_FILE", str(tmp_path / "public" / "agent_state.json"), raising=False)
    monkeypatch.setattr(cfg, "ALLOW_MARGIN", False, raising=False)
    monkeypatch.setattr(cfg, "ALLOW_NEGATIVE_CASH", False, raising=False)
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True, raising=False)
    monkeypatch.setattr(cfg, "ALLOW_SAME_DAY_REBUY", False, raising=False)

    import agenttrade.db as db

    db._DB_INITIALIZED = False
    yield db_path
    db._DB_INITIALIZED = False

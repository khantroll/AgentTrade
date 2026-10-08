"""Files update_deploy.sh copies, and the post-copy import smoke test.

The file set is the tracked tree (``git ls-files``) when this directory is a
git checkout, and a walk of the same kinds of files when the tree was copied
without ``.git``. Runtime state is never included.

``python deploy_manifest.py --import-smoke`` imports every application module
and exits non-zero if any import fails. Deploy uses that to roll back.
"""

from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Iterable, Optional

# Names the live host owns. A deploy must not replace them.
RUNTIME_BASENAMES = frozenset({
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
    "DEPLOY_SHA",
    "DEPLOY_SHA.txt",
})

SKIP_DIR_NAMES = frozenset({
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "backups",
})

INCLUDE_SUFFIXES = frozenset({
    ".py", ".html", ".sh", ".php", ".js", ".css", ".txt", ".md",
    ".json", ".toml", ".yml", ".yaml", ".ini", ".cfg",
})

SPECIAL_NAMES = frozenset({
    ".env.example",
    "requirements.txt",
    "requirements-dev.txt",
    "Dockerfile",
})

# One-shot scripts that run on import. They are copied, but importing them
# is not a module check: diag.py reads agent_state.json, and pat.py rewrites
# files under /opt and the web root.
IMPORT_SKIP_MODULES = frozenset({"diag", "pat"})

# Missing third-party packages must not hide a broken local import during
# the smoke test. Production venvs have them; a copied tree in CI may not.
_THIRD_PARTY = (
    "dotenv",
    "schedule",
    "yfinance",
    "vaderSentiment",
    "vaderSentiment.vaderSentiment",
    "flask",
    "flask_cors",
    "anthropic",
    "openai",
    "requests",
)


def is_runtime_path(rel: str) -> bool:
    """True for live host state that deploy must leave untouched."""
    name = Path(rel).name
    if name in RUNTIME_BASENAMES:
        return True
    if name.endswith(".log") or name.endswith(".jsonl"):
        return True
    if ".sqlite3" in name:
        return True
    return False


def _include_file(rel: str) -> bool:
    if not rel or rel.endswith("/"):
        return False
    parts = Path(rel).parts
    if any(part in SKIP_DIR_NAMES for part in parts):
        return False
    if is_runtime_path(rel):
        return False
    name = Path(rel).name
    if name in SPECIAL_NAMES or name.startswith(".env.example"):
        return True
    return Path(rel).suffix.lower() in INCLUDE_SUFFIXES


def _git_names(root: Path, extra: list[str]) -> Optional[list[str]]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", *extra],
            cwd=str(root),
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    raw = proc.stdout.decode("utf-8", errors="surrogateescape")
    return [item for item in raw.split("\0") if item]


def _from_git(root: Path) -> Optional[list[str]]:
    """Tracked files, plus new files that are not gitignored.

    A checkout that has not committed ``deploy_manifest.py`` yet still
    deploys it. ``.env``, sqlite, and logs stay out because they are ignored.
    """
    if not (root / ".git").exists():
        return None
    tracked = _git_names(root, [])
    if tracked is None:
        return None
    others = _git_names(root, ["--others", "--exclude-standard"]) or []
    names = set(tracked)
    names.update(others)
    return sorted(rel for rel in names if _include_file(rel))


def _from_walk(root: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        for filename in filenames:
            full = Path(dirpath) / filename
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                continue
            if _include_file(rel):
                found.append(rel)
    return sorted(found)


def deploy_file_list(root: Optional[os.PathLike] = None) -> list[str]:
    """Relative paths to copy. Git index when available, else a directory walk."""
    base = Path(root or Path(__file__).resolve().parent).resolve()
    listed = _from_git(base)
    if listed is not None:
        return listed
    return _from_walk(base)


def _module_file(root: Path, module: str) -> Optional[Path]:
    if not module:
        return None
    parts = module.split(".")
    file_path = root.joinpath(*parts).with_suffix(".py")
    if file_path.is_file():
        return file_path
    init_path = root.joinpath(*parts, "__init__.py")
    if init_path.is_file():
        return init_path
    return None


def _imported_modules(tree: ast.AST, package: str) -> Iterable[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level and package:
                prefix = package.split(".")
                keep = prefix[: len(prefix) - (node.level - 1)] if node.level else prefix
                base = ".".join(part for part in keep if part)
                if node.module:
                    yield f"{base}.{node.module}" if base else node.module
                else:
                    yield base
            elif node.module:
                yield node.module
                # ``from agents import risk`` is a submodule when the file exists.
                for alias in node.names:
                    if alias.name and alias.name != "*":
                        yield f"{node.module}.{alias.name}"


def transitive_local_modules(root: Optional[os.PathLike], entry_modules: Iterable[str]) -> set[str]:
    """Repo-relative paths of local modules imported by ``entry_modules``, transitively."""
    base = Path(root or Path(__file__).resolve().parent).resolve()
    pending = list(entry_modules)
    seen_mods: set[str] = set()
    files: set[str] = set()
    while pending:
        name = pending.pop()
        if not name or name in seen_mods:
            continue
        path = _module_file(base, name)
        if path is None:
            # ``pkg.sub`` may have been guessed from a from-import of a symbol.
            if "." in name:
                parent = name.rsplit(".", 1)[0]
                if parent not in seen_mods:
                    pending.append(parent)
            continue
        seen_mods.add(name)
        rel = path.relative_to(base).as_posix()
        if rel in files:
            continue
        files.add(rel)
        package = name if path.name == "__init__.py" else (name.rsplit(".", 1)[0] if "." in name else "")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError):
            continue
        for imported in _imported_modules(tree, package):
            if imported not in seen_mods:
                pending.append(imported)
    return files


def app_module_names(root: Optional[os.PathLike] = None) -> list[str]:
    """Importable application modules. Tests are copied but not imported."""
    base = Path(root or Path(__file__).resolve().parent).resolve()
    names = []
    for rel in deploy_file_list(base):
        path = Path(rel)
        if path.suffix != ".py":
            continue
        if path.parts and path.parts[0] == "tests":
            continue
        if path.name == "__init__.py":
            module = ".".join(path.parent.parts)
        else:
            module = ".".join(path.with_suffix("").parts)
        if not module or module in IMPORT_SKIP_MODULES or module.split(".")[-1] in IMPORT_SKIP_MODULES:
            continue
        names.append(module)
    return sorted(set(names))


def _package_for(rel: str) -> str:
    path = Path(rel)
    if path.name == "__init__.py":
        return ".".join(path.parent.parts)
    if len(path.parts) == 1:
        return ""
    return ".".join(path.parent.parts)


def _resolve_from_module(node: ast.ImportFrom, package: str) -> Optional[str]:
    if node.level:
        prefix = package.split(".") if package else []
        keep = prefix[: len(prefix) - (node.level - 1)] if node.level else prefix
        base = ".".join(part for part in keep if part)
        if node.module:
            return f"{base}.{node.module}" if base else node.module
        return base or None
    return node.module or None


def _is_external_module(mod: str) -> bool:
    root = (mod or "").split(".")[0]
    if not root:
        return True
    if root in _THIRD_PARTY or root == "__future__":
        return True
    stdlib = getattr(sys, "stdlib_module_names", ())
    return root in stdlib


def verify_imported_names(root: Optional[os.PathLike] = None) -> list[str]:
    """Fail when a local ``from module import name`` does not exist.

    ``import cycle`` succeeds even if ``aware_now_iso`` is missing, because
    that import sits inside a function. Deploying a stale ``trading_day.py``
    or ``market_data.py`` is the production failure this catches.
    """
    base = Path(root or Path(__file__).resolve().parent).resolve()
    failures = []
    for rel in deploy_file_list(base):
        path = Path(rel)
        if path.suffix != ".py" or (path.parts and path.parts[0] == "tests"):
            continue
        if path.stem in IMPORT_SKIP_MODULES:
            continue
        source = base / rel
        try:
            tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError) as exc:
            failures.append(f"{rel}: {exc.__class__.__name__}: {exc}")
            continue
        package = _package_for(rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = _resolve_from_module(node, package)
            if not mod:
                continue
            for alias in node.names:
                name = alias.name
                if not name or name == "*":
                    continue
                if _module_file(base, mod) is None:
                    if _module_file(base, f"{mod}.{name}") is not None:
                        mod_to_check = f"{mod}.{name}"
                        attr = None
                    elif _is_external_module(mod):
                        continue
                    else:
                        failures.append(f"{rel}: cannot import name {name!r} from {mod!r}")
                        continue
                else:
                    mod_to_check = mod
                    attr = name
                try:
                    imported = importlib.import_module(mod_to_check)
                except Exception as exc:
                    failures.append(
                        f"{rel}: cannot import {mod_to_check!r} ({exc.__class__.__name__}: {exc})"
                    )
                    continue
                if attr and not hasattr(imported, attr):
                    if _module_file(base, f"{mod}.{attr}") is None:
                        failures.append(f"{rel}: cannot import name {attr!r} from {mod!r}")
    return failures


def _stub_third_party() -> None:
    import types
    from unittest.mock import MagicMock

    for name in _THIRD_PARTY:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except Exception:
            module = types.ModuleType(name)
            if name == "requests":
                class _HTTPError(Exception):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args)
                        self.response = kwargs.get("response")

                module.HTTPError = _HTTPError
                module.Response = MagicMock
                module.get = MagicMock()
                module.post = MagicMock()
                module.put = MagicMock()
                module.delete = MagicMock()
                module.Session = MagicMock
                module.exceptions = types.SimpleNamespace(
                    RequestException=Exception,
                    HTTPError=_HTTPError,
                    Timeout=Exception,
                    ConnectionError=Exception,
                )
            elif name == "dotenv":
                module.load_dotenv = lambda *args, **kwargs: False
            else:
                module.__dict__.setdefault("__getattr__", lambda _name: MagicMock())
            sys.modules[name] = module


def import_app_modules(root: Optional[os.PathLike] = None) -> list[str]:
    """Import every application module. Returns failure messages.

    The working directory and the SQLite path are a scratch folder so the
    check cannot append to ``trading_agent.log`` or open the live ledger.
    """
    base = Path(root or Path(__file__).resolve().parent).resolve()
    scratch = tempfile.mkdtemp(prefix="agenttrade-import-smoke-")
    previous_cwd = os.getcwd()
    previous_db = os.environ.get("AGENTTRADE_DB_PATH")
    os.environ.setdefault("AGENTTRADE_DB_PATH", os.path.join(scratch, "smoke.sqlite3"))
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(base))
    failures = []
    try:
        os.chdir(scratch)
        _stub_third_party()
        for name in app_module_names(base):
            try:
                importlib.import_module(name)
            except Exception as exc:
                failures.append(f"{name}: {exc.__class__.__name__}: {exc}")
                traceback.print_exc()
            finally:
                os.chdir(scratch)
        failures.extend(verify_imported_names(base))
    finally:
        os.chdir(previous_cwd)
        if previous_db is None:
            os.environ.pop("AGENTTRADE_DB_PATH", None)
        else:
            os.environ["AGENTTRADE_DB_PATH"] = previous_db
    return failures


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = None
    if "--root" in args:
        index = args.index("--root")
        root = args[index + 1]
        del args[index:index + 2]
    if "--list" in args:
        for rel in deploy_file_list(root):
            print(rel)
        return 0
    if "--import-smoke" in args:
        failures = import_app_modules(root)
        if failures:
            print(f"import smoke failed ({len(failures)}):", file=sys.stderr)
            for line in failures:
                print(f"  {line}", file=sys.stderr)
            return 1
        print(f"import smoke ok ({len(app_module_names(root))} modules)")
        return 0
    print("usage: deploy_manifest.py --list | --import-smoke [--root PATH]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

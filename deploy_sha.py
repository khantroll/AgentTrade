"""One deploy commit for DEPLOY_SHA.txt and the extensionless DEPLOY_SHA file.

Nothing in the trading loop used to read or write these. A host deploy updated
DEPLOY_SHA.txt and left DEPLOY_SHA on the previous commit, so a read-only
check saw two revisions. DEPLOY_SHA.txt is the canonical file. Both are always
rewritten from that one value when they disagree or only one exists.

The cycle does not advance the stamp to a newer git HEAD. ``update_deploy.sh``
and ``install.sh`` record the commit by calling ``write_deploy_sha.sh``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

CANONICAL_NAME = "DEPLOY_SHA.txt"
MIRROR_NAME = "DEPLOY_SHA"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _read_sha(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    parts = text.strip().split()
    if not parts:
        return ""
    token = parts[0].strip()
    return token if _SHA_RE.match(token) else ""


def write_both(directory: str | os.PathLike, sha: str) -> str:
    """Write the same commit into both deploy SHA files."""
    token = (sha or "").strip()
    if not _SHA_RE.match(token):
        raise ValueError(f"deploy sha must be 7-40 hex characters, got {sha!r}")
    dest = Path(directory)
    dest.mkdir(parents=True, exist_ok=True)
    payload = token + "\n"
    (dest / CANONICAL_NAME).write_text(payload, encoding="utf-8")
    (dest / MIRROR_NAME).write_text(payload, encoding="utf-8")
    return token


def reconcile_deploy_sha(directory: str | os.PathLike | None = None) -> str:
    """Return the recorded commit, rewriting both files when they disagree.

    An empty directory stays empty. This does not call git, so a cycle cannot
    move the stamp just because the checkout moved.
    """
    dest = Path(directory or os.getcwd())
    canonical = _read_sha(dest / CANONICAL_NAME)
    mirror = _read_sha(dest / MIRROR_NAME)
    if canonical and mirror and canonical == mirror:
        return canonical
    if canonical and mirror and canonical != mirror:
        log.warning(
            "[Deploy] %s (%s) and %s (%s) disagree. Rewriting both from %s.",
            CANONICAL_NAME,
            canonical,
            MIRROR_NAME,
            mirror,
            CANONICAL_NAME,
        )
        return write_both(dest, canonical)
    sha = canonical or mirror
    if not sha:
        return ""
    log.warning(
        "[Deploy] Only one deploy SHA file was present (%s). Writing both.",
        CANONICAL_NAME if canonical else MIRROR_NAME,
    )
    return write_both(dest, sha)

"""T0.5 CI guard — no private keys / credentials in the repo tree.

Even though ``.gitignore`` excludes ``*.pem`` / ``*.key``, a contributor could
force-add one with ``git add -f`` or mis-place a file. This test fails loudly
if any sensitive file reappears under the repo root.

Scope: any file whose suffix is in ``FORBIDDEN_SUFFIXES`` or whose basename
is in ``FORBIDDEN_NAMES``, anywhere under the repo root, excluding virtualenv
/ vendored paths listed in ``EXCLUDED_DIRS``.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_SUFFIXES = {".pem", ".key", ".pfx", ".p12"}
FORBIDDEN_NAMES = {
    ".kalshi_private_key.pem",
    ".credentials.json",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}

# Never walk into these — third-party or runtime-only trees.
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".claude",
    # Example/sample certs for docs can live here if ever needed.
    "tests/fixtures/certs",
}


def _walk_repo():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        # Prune excluded directories in-place so os.walk skips them.
        rel_dir = Path(dirpath).relative_to(REPO_ROOT)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in EXCLUDED_DIRS
            and str(rel_dir / d) not in EXCLUDED_DIRS
        ]
        for name in filenames:
            yield Path(dirpath) / name


def test_no_private_keys_in_repo():
    """Fail if a *.pem / *.key / credential file appears anywhere in the repo."""
    offenders: list[str] = []
    for path in _walk_repo():
        suffix = path.suffix.lower()
        name = path.name
        if suffix in FORBIDDEN_SUFFIXES or name in FORBIDDEN_NAMES:
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert not offenders, (
        "Sensitive file(s) found in repo tree — remove and rotate the secret:\n  "
        + "\n  ".join(sorted(offenders))
        + "\n\nCanonical location for the Kalshi RSA key is "
          "~/.kalshi_private_key.pem (referenced by $KALSHI_PRIVATE_KEY_PATH), "
          "NOT inside the repo."
    )


def test_gitignore_still_blocks_secrets():
    """Belt-and-suspenders: .gitignore must still list the forbidden patterns.

    If somebody removes these lines from .gitignore, this test catches it
    before a developer accidentally commits a key.
    """
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    required = ["*.pem", "*.key", ".env", ".credentials.json"]
    missing = [pat for pat in required if pat not in gitignore]
    assert not missing, (
        f".gitignore is missing required secret patterns: {missing}. "
        "Re-add them — removing them is a foot-gun for future contributors."
    )

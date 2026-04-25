"""T0.2 regression test — enforce the daemon DB write-discipline.

The persistent daemon shares one SQLite connection across threads. Every
write must go through `bot.db.db_write_ctx()` (or the function-form
`bot.db.db_write()`) which holds `DB_WRITE_LOCK` for the whole
execute→commit region. Raw `conn.commit()` calls outside `bot/db.py` are
a regression surface: they are either unprotected writes or redundant
commits inside an already-wrapped block.

This test greps the production tree for that pattern. If it fails, a new
write path was added without using the canonical wrapper — fix by
wrapping the execute(s) in `with db_write_ctx(conn):` and removing the
explicit commit.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files that are allowed to call .commit() directly:
# - bot/db.py itself owns the wrappers
# - legacy backup/snapshot files kept only for historical reference
# - test files (they set up fixtures on their own connections)
# - standalone utility scripts with their own short-lived connections
ALLOWED_COMMIT_FILES = {
    "bot/db.py",
    # Utility scripts with own short-lived connections.
    "backtest.py",
    "diagnose_sources.py",
    "eval_bot.py",
    "audit_orders.py",
}

# Matches a line whose non-whitespace content is exactly `X.commit()` for
# X in {conn, self.conn, c}. Tight on purpose so string literals, comments,
# or `cursor.commit()` on an unrelated object don't trigger.
COMMIT_RE = re.compile(r"^\s*(conn|self\.conn|c)\.commit\(\)\s*$")


def _python_files() -> list[Path]:
    """All *.py under repo root, excluding .venv, .git, __pycache__."""
    excluded_dirs = {
        ".venv", ".git", "__pycache__", ".pytest_cache", "node_modules",
        # .claude/worktrees holds duplicate repo copies for sub-agent
        # worktree isolation. Scanning them would double-count every
        # match against the production tree.
        ".claude",
    }
    return [
        p for p in REPO_ROOT.rglob("*.py")
        if not any(part in excluded_dirs for part in p.parts)
    ]


def test_no_raw_commits_outside_bot_db():
    """Every write in production code must use db_write_ctx / db_write."""
    violations: list[str] = []
    for path in _python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOWED_COMMIT_FILES:
            continue
        if rel.startswith("tests/"):
            continue  # Tests own their fixture connections.
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if COMMIT_RE.match(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert not violations, (
        "Raw conn.commit() outside bot/db.py — wrap in "
        "`with db_write_ctx(conn):` instead (see T0.2 in the audit plan).\n"
        "Violations:\n  " + "\n  ".join(violations)
    )


def test_no_connection_row_factory_mutation():
    """Setting row_factory on the shared daemon connection races across threads.

    Use a local cursor with its own row_factory instead:
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row
        rows = cursor.execute(...).fetchall()
    """
    pattern = re.compile(r"^\s*(conn|self\.conn)\.row_factory\s*=")
    violations: list[str] = []
    for path in _python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        # diagnose_sources.py / eval_bot.py / tools/* open their own connection
        # and never thread it — exempt them explicitly.
        if rel in {"diagnose_sources.py", "eval_bot.py"}:
            continue
        if rel.startswith("tools/"):
            continue
        if rel.startswith("tests/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.match(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert not violations, (
        "Shared-connection row_factory mutation detected — use a local "
        "cursor instead (see T0.2 / regression 15 in CLAUDE.md).\n"
        "Violations:\n  " + "\n  ".join(violations)
    )


def test_grep_guard_actually_runs():
    """Sanity: confirm we're scanning real files, not an empty set."""
    files = _python_files()
    assert len(files) > 10, (
        f"Grep guard found only {len(files)} .py files — path logic is broken."
    )

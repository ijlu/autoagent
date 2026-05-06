"""Structural invariant: every POST to /portfolio/orders must tag the order
with a client_order_id carrying the correct strategy prefix.

This test is load-bearing for the T3 fills ledger. The ledger's source_tagger
routes fills by client_order_id prefix:

    mm_wx_     → mm_quote (weather MM)
    mm_sc_     → safe_compounder
    mm_exit_   → exit (manage_positions graduated health-score exit)
    mm_dir_    → directional

Any order posted without a client_order_id arrives at the ledger as `manual`,
polluting per-strategy P&L attribution. This test fails closed: if anyone adds
a new `/portfolio/orders` POST without a tag, CI catches it.

Approach: parse trade.py and bot/daemon/weather_quoter.py with ast, find every
api_post("/portfolio/orders", ...) call, and assert the body dict contains a
client_order_id key with a string starting in one of the known prefixes.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every file that is allowed to post orders. If a new file wants to post orders,
# it must be added here AND its call sites must tag with one of the prefixes
# below. Forces a visible PR review of any new order-posting code path.
FILES_THAT_POST_ORDERS = [
    REPO_ROOT / "trade.py",
    REPO_ROOT / "bot" / "daemon" / "weather_quoter.py",
]

ALLOWED_PREFIXES = ("mm_wx_", "mm_sc_", "mm_exit_", "mm_dir_")


def _iter_order_post_calls(source: str) -> Iterable[tuple[int, ast.Call]]:
    """Yield (lineno, call_node) for every api_post('/portfolio/orders', ...) call."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "api_post"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == "/portfolio/orders":
            yield node.lineno, node


def _body_dict_keys(call: ast.Call, source_lines: list[str]) -> list[str] | None:
    """Return the keys of the body dict passed to api_post, or None if the body
    is constructed separately (e.g. as a named variable assigned earlier).

    For separately-assigned bodies, the caller must backtrack through the
    enclosing function looking for the assignment. We do that below.
    """
    if len(call.args) < 2:
        return None
    body = call.args[1]
    if isinstance(body, ast.Dict):
        return [
            k.value for k in body.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
    return None


def _find_body_assignment(
    tree: ast.Module, call: ast.Call, body_name: str,
) -> ast.Dict | None:
    """When api_post(path, order_body) is called with a named variable, walk the
    AST to find the most recent `order_body = {...}` assignment in the same
    function, plus any subsequent `order_body["key"] = value` statements.

    Returns a synthetic ast.Dict combining all keys seen. None if not found.
    """
    # Find the function enclosing the call.
    enclosing_fn: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            # call.lineno within this function's line span?
            last_line = max(
                (getattr(n, "lineno", 0) for n in ast.walk(node)),
                default=node.lineno,
            )
            if node.lineno <= call.lineno <= last_line:
                # Innermost wins — keep tightening.
                if enclosing_fn is None or node.lineno > enclosing_fn.lineno:
                    enclosing_fn = node
    if enclosing_fn is None:
        return None

    # Collect keys from (a) the dict literal assigned to body_name and (b) any
    # later `body_name["foo"] = ...` subscript assignments before the call.
    collected_keys: list[str] = []
    assigned = False
    for node in ast.walk(enclosing_fn):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == body_name:
                    if isinstance(node.value, ast.Dict):
                        collected_keys = [
                            k.value for k in node.value.keys
                            if isinstance(k, ast.Constant)
                            and isinstance(k.value, str)
                        ]
                        assigned = True
                elif (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == body_name
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(tgt.slice.value, str)
                    and node.lineno < call.lineno
                ):
                    collected_keys.append(tgt.slice.value)
    if not assigned:
        return None
    # Synthesize an ast.Dict with the collected keys (values irrelevant).
    return ast.Dict(
        keys=[ast.Constant(value=k) for k in collected_keys],
        values=[ast.Constant(value=None) for _ in collected_keys],
    )


@pytest.mark.parametrize("path", FILES_THAT_POST_ORDERS, ids=lambda p: p.name)
def test_every_order_post_has_client_order_id(path: Path) -> None:
    source = path.read_text()
    tree = ast.parse(source)
    lines = source.splitlines()

    call_sites = list(_iter_order_post_calls(source))
    assert call_sites, (
        f"{path.name}: expected at least one api_post('/portfolio/orders', ...) "
        f"call; found none. Did the file change?"
    )

    failures: list[str] = []
    for lineno, call in call_sites:
        keys = _body_dict_keys(call, lines)
        if keys is None:
            # body is a named variable — resolve.
            if len(call.args) >= 2 and isinstance(call.args[1], ast.Name):
                synthetic = _find_body_assignment(tree, call, call.args[1].id)
                if synthetic is not None:
                    keys = [
                        k.value for k in synthetic.keys
                        if isinstance(k, ast.Constant)
                    ]
        if keys is None:
            failures.append(
                f"{path.name}:{lineno} — could not resolve order body dict; "
                f"add a literal client_order_id key inline"
            )
            continue
        if "client_order_id" not in keys:
            failures.append(
                f"{path.name}:{lineno} — order body has no 'client_order_id' "
                f"key. Every bot-placed order must carry a prefix from "
                f"{ALLOWED_PREFIXES} so the fills ledger can attribute it."
            )

    assert not failures, (
        "Untagged order POST found:\n  " + "\n  ".join(failures)
    )


def test_trade_py_exit_and_directional_use_expected_prefixes() -> None:
    """Spot-check: the two paths we just tagged in T3 prep use the right
    prefixes, distinguishable from each other and from the two existing MM
    prefixes. This is a literal-string assertion — if the prefix is ever
    renamed, this test fires and the ledger's source_tagger must be updated
    in the same change.
    """
    src = (REPO_ROOT / "trade.py").read_text()
    assert 'f"mm_exit_{' in src, (
        "trade.py must construct client_order_id with 'mm_exit_' prefix "
        "at the synthetic-sell site"
    )
    assert 'f"mm_dir_{' in src, (
        "trade.py must construct client_order_id with 'mm_dir_' prefix "
        "at the directional-buy site"
    )
    assert 'f"mm_sc_{' in src, (
        "trade.py must construct client_order_id with 'mm_sc_' prefix "
        "at the safe-compounder site"
    )


def test_weather_quoter_uses_mm_wx_prefix() -> None:
    src = (REPO_ROOT / "bot" / "daemon" / "weather_quoter.py").read_text()
    assert 'f"mm_wx_{' in src, (
        "weather_quoter must construct client_order_id with 'mm_wx_' prefix"
    )


# Period safety (CLAUDE.md §Known Bug Pattern #1: Kalshi rejects periods in
# client_order_id) is enforced elsewhere:
#   - weather MM:         tests/test_weather_quoter.py::test_client_order_id_no_periods
#   - new mm_exit_/mm_dir_ sites: inline `ticker.replace('.', '_')` in trade.py
# Static AST verification is too brittle (multi-line safe_ticker assignments
# are false positives). The invariant above (every POST has client_order_id)
# is the load-bearing check.

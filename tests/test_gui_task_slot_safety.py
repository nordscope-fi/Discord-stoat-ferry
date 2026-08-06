"""Slot safety for NiceGUI background tasks (issue #123).

NiceGUI keeps its "current slot" stack in ``Slot.stacks``, a plain dict keyed by
``id(asyncio.current_task())`` -- **not** a ``contextvar``. Child tasks therefore
do not inherit it, and ``background_tasks.create`` is just ``loop.create_task``.
Every API that resolves ``ui.context.client`` (``ui.notify``, ``ui.navigate.to``,
``app.storage.tab``) raises ``RuntimeError`` inside such a task.

That killed the one-click GUI export on every platform from v2.6.14 to v2.11.0,
silently, because the exception went to a logger with no handler.

These tests are deliberately fixture-free. NiceGUI's ``user`` fixture replaces
``ui.notify`` with ``UserNotify`` (which appends to a list) and ``ui.navigate``
with ``UserNavigate`` (which reads ``Client.page_routes``); **neither touches
``context.client``**, so the fixture cannot prove anything here. Using it would
reproduce the original sin of this bug: a test that passes without exercising
what it claims to.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from nicegui import context, core, ui
from nicegui.client import Client
from nicegui.slot import Slot, get_task_id

from discord_ferry import gui

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def script_mode_off() -> Iterator[None]:
    """Disable the pseudo-client hatch, then put it back.

    ``Context.slot_stack`` fabricates a pseudo-client when
    ``not core.app.is_started`` -- which is true under pytest -- so ``ui.context``
    *succeeds* in a bare test and the defect is invisible. Forcing
    ``core.script_mode`` short-circuits that same ``if``, reproducing the running
    server's behaviour.

    The reset is mandatory: ``nicegui_reset_globals()`` calls ``core.reset()``
    only on ENTRY, never in its ``finally``, and this test does not use it at
    all. A leaked ``script_mode = True`` makes ``app.storage.user`` return a
    throwaway ``PseudoPersistentDict`` for the rest of the worker, silently
    corrupting every later GUI test.
    """
    previous = core.script_mode
    core.script_mode = True
    try:
        yield
    finally:
        core.script_mode = previous


async def test_bare_background_task_cannot_resolve_the_client(
    script_mode_off: None,
) -> None:
    """SC-123-8a: this is the #123 mechanism, reproduced.

    A child task starts with an empty slot stack, so ``ui.context.client`` raises.
    """
    outcome: dict[str, object] = {}

    async def child() -> None:
        outcome["stack"] = list(Slot.get_stack())
        try:
            outcome["client"] = context.client
        except RuntimeError as exc:
            outcome["error"] = str(exc)

    Slot.stacks[get_task_id()] = ["<page slot>"]
    try:
        await asyncio.get_running_loop().create_task(child())
    finally:
        Slot.stacks.pop(get_task_id(), None)

    assert outcome["stack"] == [], "child task unexpectedly inherited a slot stack"
    assert "error" in outcome, "expected RuntimeError; the defect did not reproduce"
    assert "slot stack for this task is empty" in str(outcome["error"])


async def test_with_client_restores_the_slot_in_a_background_task(
    script_mode_off: None,
) -> None:
    """SC-123-8b: ``with client:`` is what makes the fix work.

    Same child task as above, but re-entering the client's slot: ``context.client``
    now resolves, and to the *same* client object.
    """
    client = Client(ui.page("/_slot_safety_probe"))
    resolved: dict[str, object] = {}

    async def child() -> None:
        with client:
            resolved["client"] = context.client
        resolved["after"] = list(Slot.get_stack())

    await asyncio.get_running_loop().create_task(child())

    assert resolved["client"] is client
    assert resolved["after"] == [], "slot stack must be popped on exit"


async def test_slot_stack_is_pruned_when_the_task_is_cancelled(
    script_mode_off: None,
) -> None:
    """SC-123-8c: a cancelled task must not leave its stack behind.

    ``Slot.stacks`` is keyed by task id and ids are reused, so a leaked entry
    would silently hand a later task somebody else's client.
    """
    client = Client(ui.page("/_slot_safety_probe_cancel"))
    started = asyncio.Event()
    task_ids: list[int] = []

    async def child() -> None:
        with client:
            task_ids.append(get_task_id())
            started.set()
            await asyncio.sleep(60)

    task = asyncio.get_running_loop().create_task(child())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task_ids, "child never entered the slot"
    assert task_ids[0] not in Slot.stacks, "cancelled task leaked its slot stack"


# ---------------------------------------------------------------------------
# S6 — structural guard against reintroduction
# ---------------------------------------------------------------------------


class _BackgroundTaskAudit(ast.NodeVisitor):
    """Find client-scoped lookups inside ``background_tasks.create`` targets.

    Resolution is by SCOPE, not by name. ``gui.py`` contains two distinct
    ``async def _run()`` closures -- one inside ``_start_rollback``, one directly
    in ``migrate_page`` -- so a name-keyed walker would misattribute a violation
    or silently skip one of them.
    """

    FORBIDDEN = ("context", "tab")

    def __init__(self) -> None:
        self.scopes: list[dict[str, ast.AsyncFunctionDef]] = [{}]
        self.launched: set[ast.AsyncFunctionDef] = set()
        self.violations: list[tuple[str, int]] = []

    def _lookup(self, name: str) -> ast.AsyncFunctionDef | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.scopes[-1][node.name] = node
        self.scopes.append({})
        self.generic_visit(node)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.scopes.append({})
        self.generic_visit(node)
        self.scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "create"
            and isinstance(func.value, ast.Name)
            and func.value.id == "background_tasks"
            and node.args
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name)
        ):
            target = self._lookup(node.args[0].func.id)
            if target is not None:
                self.launched.add(target)
        self.generic_visit(node)


def _unguarded_client_lookups(fn: ast.AsyncFunctionDef) -> list[tuple[str, int]]:
    """Return client-scoped lookups in *fn* that are not inside ``with client:``."""
    guarded: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.With):
            for item in node.items:
                if isinstance(item.context_expr, ast.Name) and item.context_expr.id == "client":
                    guarded.update(id(child) for child in ast.walk(node))

    found: list[tuple[str, int]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Attribute) or id(node) in guarded:
            continue
        # ui.context.* and app.storage.tab
        if node.attr in _BackgroundTaskAudit.FORBIDDEN and isinstance(
            node.value, ast.Name | ast.Attribute
        ):
            found.append((fn.name, node.lineno))
    return found


def test_no_background_task_resolves_the_client_outside_with_client() -> None:
    """SC-123-31: the structural guard, run against the real gui.py."""
    tree = ast.parse(inspect.getsource(gui))
    audit = _BackgroundTaskAudit()
    audit.visit(tree)

    assert audit.launched, "found no background_tasks.create targets — the audit is broken"

    violations = [v for fn in audit.launched for v in _unguarded_client_lookups(fn)]
    assert not violations, (
        "client-scoped lookup inside a background task without `with client:` — "
        f"this is issue #123: {violations}"
    )


def test_audit_resolves_the_two_colliding_run_closures() -> None:
    """SC-123-33: scope-stack regression case.

    ``gui.py`` has two separate ``async def _run()``. Both must be discovered as
    distinct nodes; a name-keyed walker would collapse them into one.
    """
    tree = ast.parse(inspect.getsource(gui))
    audit = _BackgroundTaskAudit()
    audit.visit(tree)

    runs = [fn for fn in audit.launched if fn.name == "_run"]
    assert len(runs) == 2, f"expected two distinct `_run` closures, found {len(runs)}"
    assert runs[0].lineno != runs[1].lineno


def test_audit_flags_a_synthetic_violation() -> None:
    """SC-123-32: the guard actually fails when it should.

    Without this, a guard that silently matches nothing would pass forever — the
    exact failure mode of the `inspect.getsource()` assertions it replaces.
    """
    source = (
        "async def page():\n"
        "    async def _task():\n"
        "        await ui.context.client.connected()\n"
        "    background_tasks.create(_task())\n"
    )
    audit = _BackgroundTaskAudit()
    audit.visit(ast.parse(source))

    assert len(audit.launched) == 1
    violations = [v for fn in audit.launched for v in _unguarded_client_lookups(fn)]
    assert violations, "guard failed to flag an obvious violation"
    assert violations[0][0] == "_task"


def test_narrow_nicegui_test_plugin_is_registered() -> None:
    """SC-123-27: never register the umbrella plugin.

    ``nicegui.testing.plugin`` imports ``screen_plugin``, which does
    ``from selenium import webdriver`` at module level. Selenium is not a
    dependency, so the umbrella aborts collection for the WHOLE suite.
    """
    # Read the ASSIGNED value, not the file text: conftest.py names the umbrella
    # in a comment explaining why it must not be used, and a substring check would
    # match that comment.
    tree = ast.parse((Path(__file__).parent / "conftest.py").read_text(encoding="utf-8"))
    assigned: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in node.targets
        ):
            assigned = [
                elt.value
                for elt in getattr(node.value, "elts", [])
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]

    assert assigned == ["nicegui.testing.user_plugin"], (
        f"pytest_plugins must register only the narrow plugin, got {assigned}"
    )

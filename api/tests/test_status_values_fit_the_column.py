"""Every status the code can write must fit the column it is written to.

WHY THIS FILE EXISTS. Request.status is varchar(16). The code contained
`req.status = "decommission-failed"` — nineteen characters — sitting unexploded
since it was written, because no decommission has ever failed. The first one
would have raised a database error instead of recording the failure, which is
the worst possible moment for a second failure.

It survived a full test suite because the tests run on SQLite, and SQLite does
not enforce VARCHAR limits: it stores the nineteen characters happily. Only
Postgres refuses. This project has now been bitten by exactly that twice — the
other was an OCID in a varchar(64) column.

There is no migration tooling here, so widening a column is not a small change.
Checking the strings is.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from db import models

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCES = sorted(p for p in (ROOT / "api").glob("*.py"))


def _column_limit(model, attribute: str) -> int:
    column = model.__table__.columns[attribute]
    return int(getattr(column.type, "length", 0) or 0)


STATUS_LIMIT = min(
    _column_limit(models.Request, "status"),
    _column_limit(models.Approval, "status"),
)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level NAME = "literal" bindings, so an assignment through one of
    them can be followed to the string it really writes."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _assigned_literals(attribute: str) -> list[tuple[str, int, str]]:
    """Every string assigned to `.<attribute>` anywhere in the API — whether
    written inline or reached through a module-level constant.

    THE CONSTANT IS WHY THIS FILE DID NOT DO ITS JOB. It looked only for a string
    LITERAL on the right-hand side, and the value that broke production was not
    one:

        REAPPROVAL_NEEDED = "awaiting-reapproval"   # 19 chars
        ...
        req.status = REAPPROVAL_NEEDED             # a Name, invisible to the scan

    REQ-2026-0247's cost guard fired exactly as designed -- OKE was unpriced when
    approved, was certified mid-run, and the real figure was 499.67 a month
    against the 90.59 shown -- and then could not record its verdict. The
    transaction rolled back, the request stayed `auto-building`, and the sweep
    does not collect that status: orphaned, by the guard meant to protect it.

    Deliberately crude in the other direction still: it does not work out which
    model each assignment targets, because it compares against the NARROWEST
    status column, so a value that passes is safe wherever it lands.
    """
    found: list[tuple[str, int, str]] = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not (isinstance(target, ast.Attribute)
                        and target.attr == attribute):
                    continue
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.append((path.name, node.lineno, value.value))
                elif isinstance(value, ast.Name) and value.id in constants:
                    found.append((path.name, node.lineno, constants[value.id]))
    return found


def test_the_scan_finds_the_assignments_at_all():
    """A refactor that moved these would make every test below pass vacuously."""
    found = _assigned_literals("status")
    assert len(found) >= 8, f"only found {len(found)} — the scan has stopped working"


def test_the_scan_follows_a_constant_and_not_only_a_literal():
    """THE HOLE THIS FILE HAD. A status written through a module-level constant
    was invisible to it, and that is exactly how a nineteen-character value
    reached a varchar(16) column in production."""
    reached = {value for _, _, value in _assigned_literals("status")}

    from api import main as api_main

    assert api_main.REAPPROVAL_NEEDED in reached, (
        "a status assigned through a module-level constant is not being checked")


@pytest.mark.parametrize("where,line,value", _assigned_literals("status"),
                         ids=lambda v: v if isinstance(v, str) else str(v))
def test_every_status_written_fits_its_column(where, line, value):
    assert len(value) <= STATUS_LIMIT, (
        f"{where}:{line} writes status {value!r} ({len(value)} chars) into a "
        f"varchar({STATUS_LIMIT}) column. SQLite accepts it and Postgres will "
        f"not, so this passes every test and fails in production.")


def test_the_limit_was_actually_read_from_the_model():
    """If the column type stopped reporting a length, every test above would
    compare against zero-or-anything and prove nothing."""
    assert STATUS_LIMIT == 16, STATUS_LIMIT


def test_status_detail_has_room_for_a_machines_own_words():
    """Boot verification puts the machine's complaints in status_detail. If that
    column were narrow, a request would fail to record WHY it failed."""
    assert _column_limit(models.Request, "status_detail") >= 200

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


def _assigned_literals(attribute: str) -> list[tuple[str, int, str]]:
    """Every string literal assigned to `.<attribute>` anywhere in the API.

    Deliberately crude: it does not work out which model each assignment targets,
    because it does not need to. It compares against the NARROWEST status column,
    so a value that passes is safe wherever it lands.
    """
    found: list[tuple[str, int, str]] = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute) and target.attr == attribute
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    found.append((path.name, node.lineno, node.value.value))
    return found


def test_the_scan_finds_the_assignments_at_all():
    """A refactor that moved these would make every test below pass vacuously."""
    found = _assigned_literals("status")
    assert len(found) >= 8, f"only found {len(found)} — the scan has stopped working"


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

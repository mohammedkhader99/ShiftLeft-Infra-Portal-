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

    assert api_main.COST_CHANGED in reached, (
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


# --- and a trim must agree with the column it trims for -----------------------
#
# ADDED 2026-09-11 (H.4). `req.status_detail = "...".join(...)[:2000]` sat in the
# cancel path against a varchar(500) column. A long enough cancel reason passed
# the trim and would then have been refused by PostgreSQL, rolling back the
# cancel -- the same shape as the nineteen-character status above, written by
# somebody protecting against exactly this and picking the wrong number.
#
# SQLite does not enforce VARCHAR, so it never failed here either.

def _string_column_widths() -> dict[str, int]:
    """The narrowest String column of each name across every model.

    Narrowest on purpose, like STATUS_LIMIT above: a trim that fits the tightest
    column of that name is safe wherever the attribute actually lives, and
    working out which model an assignment targets is a static-analysis problem
    this file should not pretend to solve.
    """
    from sqlalchemy import String

    widths: dict[str, int] = {}
    for name in dir(models):
        table = getattr(getattr(models, name), "__table__", None)
        if table is None:
            continue
        for column in table.columns:
            length = getattr(column.type, "length", None)
            if isinstance(column.type, String) and length:
                widths[column.name] = min(widths.get(column.name, length), length)
    return widths


def _hand_written_trims() -> list[tuple[str, int, str, int]]:
    """Every `something.attr = <expr>[:N]` in the API, with its N."""
    found: list[tuple[str, int, str, int]] = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Subscript):
                continue
            sl = node.value.slice
            if not (isinstance(sl, ast.Slice) and sl.lower is None
                    and isinstance(sl.upper, ast.Constant)
                    and isinstance(sl.upper.value, int)):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    found.append((path.name, node.lineno, target.attr, sl.upper.value))
    return found


def test_no_hand_written_trim_claims_more_room_than_the_column_has():
    widths = _string_column_widths()
    wrong = [
        (where, line, attr, cut, widths[attr])
        for where, line, attr, cut in _hand_written_trims()
        if attr in widths and cut > widths[attr]
    ]
    assert not wrong, (
        "a trim allows more characters than its column holds; SQLite accepts it "
        f"and Postgres will not: {wrong}")


def test_the_width_map_is_actually_populated():
    """Every check above passes vacuously if this stops finding columns."""
    widths = _string_column_widths()
    assert widths.get("status") == 16, widths.get("status")
    assert widths.get("status_detail") == 500, widths.get("status_detail")


# --- the column enforces itself now, so SQLite meets the same limit ----------

def test_a_detail_too_long_is_trimmed_rather_than_refused():
    """Prose for a person. Losing its tail is a far smaller harm than losing the
    write that carries it — a cost guard that cannot record its own verdict is
    how REQ-2026-0247 was orphaned."""
    req = models.Request(reference="REQ-FIT-1")
    req.status_detail = "x" * 900

    assert len(req.status_detail) == 500
    assert req.status_detail.endswith("\u2026"), "and the trim is visible, not silent"


def test_a_detail_that_fits_is_left_exactly_alone():
    req = models.Request(reference="REQ-FIT-2")
    req.status_detail = "Nothing was built."

    assert req.status_detail == "Nothing was built."


def test_a_status_too_long_raises_instead_of_being_quietly_cut():
    """A truncated status is a value nothing in the state machine handles.
    Writing one would turn a loud failure into a request sitting in a state no
    sweep collects — the original incident, reproduced by its own fix."""
    req = models.Request(reference="REQ-FIT-3")
    with pytest.raises(ValueError) as exc:
        req.status = "decommission-failed"

    assert "at most 16" in str(exc.value)
    assert "decommission-failed" in str(exc.value), "it names the value"


def test_the_guard_reads_the_column_rather_than_a_number_of_its_own():
    """Widening the column must not need a second edit here — that second edit
    is the one nobody makes."""
    limit = _column_limit(models.Request, "status_detail")
    req = models.Request(reference="REQ-FIT-4")
    req.status_detail = "y" * (limit + 50)

    assert len(req.status_detail) == limit


def test_the_message_that_prompted_this_still_fits():
    """The cost guard's sentence was 408 of 500 characters — 82% full, with a
    figure interpolated into it. It has to survive an implausibly large one."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import api.main as api_main
    from db.session import Base

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    req = models.Request(reference="REQ-FIT-5", status="in-progress",
                         requester="a@b.com", request_type="create")
    session.add(req)
    session.commit()
    api_main._ask_for_reapproval(session, req, jira_key=None, monthly=9_999_999.99)
    session.commit()

    assert req.status == api_main.COST_CHANGED
    assert len(req.status_detail) <= _column_limit(models.Request, "status_detail")
    assert "9,999,999.99" in req.status_detail or "9999999.99" in req.status_detail

"""Every model a table, and something that creates it (F-OPS-09).

C2 added `certification_proof` as a new TABLE rather than columns on an existing
one, precisely because `create_all` creates missing tables and there is no
migration tool here. That reasoning was right and the mechanism was never
checked: `create_all` only ran from `db/seed.py::main()`, a script nobody runs on
deploy. The table did not exist, and the certification sweep raised UndefinedTable
every thirty seconds from the moment proofs were switched on — a feature reported
as live that was not running at all.

These tests are the check that was missing.
"""

from __future__ import annotations

import ast
import pathlib

from sqlalchemy import create_engine, inspect

from db.session import Base

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_create_all_produces_a_table_for_every_model():
    """The models and the schema cannot drift apart silently."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    created = set(inspect(engine).get_table_names())

    declared = set(Base.metadata.tables)
    missing = declared - created
    assert not missing, f"declared but never created: {sorted(missing)}"


def test_certification_proof_is_among_them():
    """Named explicitly. This is the table whose absence took C2 off the air, and
    a generic assertion would not say so to whoever reads a failure next."""
    assert "certification_proof" in Base.metadata.tables


def test_startup_actually_creates_tables():
    """THE gap. Declaring the models is not the same as anything running
    create_all: for months the only caller was a seeding script run by hand.

    Asserted against the source so it fails if the call is removed, rather than
    only when a new table is added and someone notices in production.
    """
    src = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    lifespan = next((n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == "_lifespan"), None)
    assert lifespan is not None, "no _lifespan handler to hook startup on"

    called = {n.func.id for n in ast.walk(lifespan)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_ensure_tables" in called, (
        "startup no longer ensures the schema exists; a new model's table would "
        "silently not be created, exactly as certification_proof was not")


def test_ensuring_tables_is_create_only():
    """It must never be able to lose data. create_all adds missing tables and
    touches nothing that exists — no drops, no alters, no column changes. A real
    schema change still needs a migration and a human.
    """
    src = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    start = src.index("def _ensure_tables")
    body = src[start:src.index("\n@asynccontextmanager", start)]

    assert "create_all" in body
    for forbidden in ("drop_all", "drop(", "ALTER", "DELETE", "TRUNCATE"):
        assert forbidden not in body, f"_ensure_tables must not {forbidden}"


def test_a_failure_to_ensure_tables_does_not_stop_the_api():
    """A database unreachable at startup is a louder problem than a missing
    table. Refusing to start would turn a recoverable state into an outage."""
    src = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    start = src.index("def _ensure_tables")
    body = src[start:src.index("\n@asynccontextmanager", start)]
    assert "except Exception" in body
    # ...but not silently: it has to say so.
    assert "warning" in body or "log" in body

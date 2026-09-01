"""What the seed says must fit the column it goes in.

`awaiting-reapproval` was nineteen characters and its column was a varchar(16).
Every test passed, because the tests run on SQLite and SQLite does not enforce a
VARCHAR limit -- it stores whatever you give it. Postgres does enforce one, so
the failure appeared only in the container, only at run time.

It happened again the same day: the withdrawal note written for `openshift` was
489 characters into a `varchar(300)`. Caught by reading the live schema rather
than by any test, which is the wrong way round.

So this compares the two directly. Every seeded string is measured against the
length its own model column declares, so a column that is later widened or
narrowed moves the limit with it and nothing here needs editing.

It is deliberately not a list of the strings it knows about: a new seed entry
that is too long is caught the day it is written, not the day it is deployed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String

from db import models, seed


def limit(model, column):
    """The declared length of a column, or None if it is unbounded."""
    col = model.__table__.columns[column]
    return col.type.length if isinstance(col.type, String) else None


# (the seeded table, the model it loads into, and which field goes in which
# column). Adding a row here is how a new seed table joins the guard.
SEEDED = [
    ("DELIVERY", 1, models.TechnologyDelivery, "note"),
    ("DELIVERY", 0, models.TechnologyDelivery, "delivery_model"),
]


@pytest.mark.parametrize("table_name, index, model, column", SEEDED)
def test_every_seeded_value_fits_its_column(table_name, index, model, column):
    cap = limit(model, column)
    assert cap, f"{model.__name__}.{column} declares no length"

    table = getattr(seed, table_name)
    too_long = {code: len(value[index])
                for code, value in table.items()
                if len(value[index]) > cap}

    assert not too_long, (
        f"{model.__name__}.{column} holds {cap} characters and these do not "
        f"fit: {too_long}. SQLite lets this pass and Postgres rejects it, so "
        f"it would fail only in the container.")


def test_the_technology_codes_themselves_fit():
    """The key is a column too, and a code long enough to overflow it would
    take the whole seed down rather than one row."""
    cap = limit(models.TechnologyDelivery, "technology_code")
    too_long = [c for c in seed.DELIVERY if len(c) > cap]

    assert not too_long, f"codes longer than {cap}: {too_long}"


def test_the_guard_would_notice():
    """A guard that cannot fail is not a guard. This is the check the two tests
    above perform, run against a value known to be too long."""
    cap = limit(models.TechnologyDelivery, "note")

    assert len("x" * (cap + 1)) > cap

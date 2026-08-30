"""Every service sets the artefact paths its own code reads.

WHY THIS GUARD EXISTS, AND WHY THE CHANGE IT GUARDS WOULD BE UNSAFE WITHOUT IT.

The defaults for these four variables used to be absolute POSIX paths --
"/generated/blueprints" and its siblings. Inside the container that is correct.
Outside one, on Windows, it resolves to the C: drive root, and anything that
publishes a draft writes there for real: a blueprint and its Terraform module
appeared outside the repository on 2026-08-21, were still being rewritten on
2026-08-29, and a diagnostic in this project read them back and reported a
blueprint that does not exist in the catalogue.

The fallback is now inside the repository, where it is git-ignored and harmless.
That is only safe while every service sets the variable explicitly. If one did
not, the container would silently write to /app/generated instead of the mounted
/generated -- invisible, and worse than the litter it replaced, because nothing
would ever look there.

So this reads the compose file and the source, and holds them to each other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from common import paths

REPO = paths.REPO_ROOT
COMPOSE = REPO / "docker-compose.yml"

#: Which service runs the code in each top-level package.
SERVICE_OF = {"api": "api", "orchestrator": "orchestrator"}


def compose_environment() -> dict[str, set[str]]:
    """{service: {environment variable names it sets}} from docker-compose.yml."""
    services: dict[str, set[str]] = {}
    current = None
    in_env = False
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        service = re.match(r"^  ([a-z][a-z0-9-]*):\s*$", line)
        if service:
            current, in_env = service.group(1), False
            services.setdefault(current, set())
            continue
        if current is None:
            continue
        if re.match(r"^    [a-z_]+:", line):
            in_env = line.strip().startswith("environment:")
            continue
        setting = re.match(r"^      ([A-Z][A-Z0-9_]*):", line)
        if in_env and setting:
            services[current].add(setting.group(1))
    return services


def readers() -> dict[str, set[str]]:
    """{variable: {services whose code reads it}}, found in the source itself.

    Read from the source rather than from a list kept beside it: a hand-kept list
    is the second source of truth that every defect this month turned out to be.
    """
    found: dict[str, set[str]] = {}
    for package, service in SERVICE_OF.items():
        for path in (REPO / package).rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for variable in paths.GENERATED_VARS:
                if variable in text:
                    found.setdefault(variable, set()).add(service)
    return found


def test_the_compose_file_can_be_read():
    """A parser that silently matches nothing passes every test below."""
    environment = compose_environment()

    assert {"api", "orchestrator"} <= set(environment)
    assert "GENERATED_ROOT" in environment["api"]


def test_the_source_actually_reads_these_variables():
    """Likewise: if nothing reads them, the test below is vacuous."""
    assert readers(), "no service reads any generated-path variable"


@pytest.mark.parametrize("variable", sorted(paths.GENERATED_VARS))
def test_a_service_sets_every_path_its_own_code_reads(variable):
    """THE GUARD. A container falling back to the repo-relative default would
    write to /app/generated rather than the mounted /generated, and nothing would
    ever look there."""
    environment = compose_environment()

    for service in readers().get(variable, set()):
        assert variable in environment.get(service, set()), (
            f"{service} reads {variable} but docker-compose.yml does not set it "
            f"for that service, so in the container it would fall back to a "
            f"repo-relative path that is not the mounted volume.")


def test_the_fallbacks_are_inside_the_repository(monkeypatch):
    """Not at the filesystem root, which on Windows is the C: drive."""
    for variable in paths.GENERATED_VARS:
        monkeypatch.delenv(variable, raising=False)
        resolved = paths.generated_dir(variable)

        assert REPO in resolved.parents or resolved == REPO / "generated", resolved


def test_a_blank_value_is_treated_as_unset(monkeypatch):
    """A variable present but empty would otherwise resolve to the filesystem
    root -- a misconfigured deployment writing to `/`."""
    monkeypatch.setenv("GENERATED_PROFILE_DIR", "   ")

    assert paths.generated_dir("GENERATED_PROFILE_DIR") == REPO / "generated" / "profiles"


def test_an_explicit_value_still_wins(monkeypatch):
    """The container sets these, and what it sets must be obeyed."""
    monkeypatch.setenv("GENERATED_PROFILE_DIR", "/generated/profiles")

    assert paths.generated_dir("GENERATED_PROFILE_DIR") == Path("/generated/profiles")


def test_an_unknown_variable_is_refused_with_the_reason():
    """A new path variable must be added to GENERATED_VARS, where the guard above
    can see it -- not resolved quietly by a typo'd name.

    THE MESSAGE IS THE POINT, and the first version of this test missed that.
    A bare `GENERATED_VARS[variable]` raises KeyError all by itself, so asserting
    only the exception type passed with the explicit check deleted -- the plant
    proved it. What the check adds is a sentence telling the next person what to
    do, so that is what is asserted."""
    with pytest.raises(KeyError) as raised:
        paths.generated_dir("GENERATED_SOMETHING_ELSE")

    assert "GENERATED_VARS" in str(raised.value), (
        "the refusal does not say how to fix it: " + str(raised.value))

"""The front-end build must type-check, not merely transpile.

The build script was `vite build` alone. Vite STRIPS TypeScript types without
checking them, so every type error in the portal's front end shipped silently and
would only appear in a browser, at whatever moment a user reached that code path.

Three were sitting there when this was first run, and all three were real:

  * `getReport` was typed without `generated_at`, a field the endpoint always
    returns, so the page read a property TypeScript believed did not exist.
  * `Component.size` was typed `string` while the form itself sets `null` for a
    platform service, which has nothing to size. Three call sites then indexed a
    lookup table by `null` -- each a silent no-op that happened to look correct.
  * Carbon's `Table` was given a `style` prop its type does not declare (the
    component does forward it, so this one worked).

None of these would have been found by the Python suite, and none of them were
found by the build, because the build was not looking.
"""

from __future__ import annotations

import json
from pathlib import Path

from common import paths

PACKAGE_JSON = paths.REPO_ROOT / "webapp" / "frontend" / "package.json"


def scripts() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8")).get("scripts", {})


def test_the_package_file_is_where_this_thinks_it_is():
    """A path that does not exist would make every test below vacuous."""
    assert PACKAGE_JSON.is_file(), PACKAGE_JSON


def test_the_build_runs_the_type_checker():
    build = scripts().get("build", "")

    assert "tsc" in build, (
        "webapp/frontend build does not type-check. `vite build` strips types "
        "without checking them, so type errors ship silently and surface in a "
        f"user's browser. Current build script: {build!r}")


def test_the_type_checker_runs_BEFORE_the_bundler():
    """`vite build && tsc` would emit the broken bundle first and fail after,
    which in a Docker build leaves the image half-made and the error easy to
    miss. The check has to gate the bundle, not follow it."""
    build = scripts().get("build", "")

    assert build.index("tsc") < build.index("vite"), build


def test_the_type_checker_is_installed_by_the_image():
    """The Dockerfile runs a plain `npm install`, so devDependencies are present.
    If that ever became `--production` or `--omit=dev`, `tsc` would vanish and
    the build would fail with a confusing "not found" rather than a type error."""
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    dockerfile = (paths.REPO_ROOT / "webapp" / "Dockerfile").read_text(encoding="utf-8")

    assert "typescript" in {
        **package.get("dependencies", {}), **package.get("devDependencies", {})}
    assert "--production" not in dockerfile and "--omit=dev" not in dockerfile

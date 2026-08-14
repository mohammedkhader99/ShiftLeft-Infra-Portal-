"""The API image contains api/, common/ and db/ — and NOT orchestrator/.

This is a deployment fact, not a style preference: the orchestrator is a separate
service holding the cloud credentials (ARCHITECTURE §3/§4), and its code is not
shipped in the API image.

An `import orchestrator...` inside a try/except therefore behaves in two
completely different ways:

  * under pytest it succeeds, because the repo root is on sys.path and every
    directory is present, so tests pass; and
  * in the container it raises ModuleNotFoundError, and the except branch runs.

That is how the OS-image filter and the Apache-on-Ubuntu refusal shipped inert.
Every test was green, and the running portal accepted Apache on an Ubuntu image
because the except branch returned "no opinion". The bug was invisible to the
whole suite by construction.

So the rule is checked structurally instead: nothing under api/ may reference the
orchestrator package. Capabilities the API needs are asked for over the signed
HTTP channel that already exists.
"""

import ast
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1]
# Tests are not shipped in the image and legitimately reach across to assert on
# the orchestrator's own files.
SKIP_DIRS = {"tests", "__pycache__"}


def _api_modules() -> list[Path]:
    return [p for p in API_DIR.rglob("*.py")
            if not SKIP_DIRS & set(p.relative_to(API_DIR).parts)]


def _orchestrator_imports(path: Path) -> list[str]:
    """Every `import orchestrator...` in a file, at any nesting depth.

    Parsed rather than grepped so a mention inside a docstring or comment — this
    codebase explains the rule in prose all over the place — is not mistaken for
    a real import.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "orchestrator"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "orchestrator":
                found.append(node.module)
    return found


def test_no_api_module_imports_the_orchestrator():
    offenders = {
        str(p.relative_to(API_DIR)): imports
        for p in _api_modules()
        if (imports := _orchestrator_imports(p))
    }
    assert not offenders, (
        f"these API modules import the orchestrator: {offenders}. That import "
        "succeeds under pytest and raises in the container, so whatever it "
        "guards will pass every test and do nothing in production. Ask the "
        "orchestrator over the signed channel instead — see "
        "api/blueprint_capabilities.")


def test_the_guard_would_catch_a_real_import(tmp_path):
    """A guard nobody has seen fail is a guard nobody knows works."""
    offender = tmp_path / "offender.py"
    offender.write_text("from orchestrator import configure\n", encoding="utf-8")
    assert _orchestrator_imports(offender) == ["orchestrator"]

    nested = tmp_path / "nested.py"
    nested.write_text("def f():\n    import orchestrator.blueprint_registry\n",
                      encoding="utf-8")
    assert _orchestrator_imports(nested) == ["orchestrator.blueprint_registry"], \
        "a lazy import inside a function is the same bug, just later"


def test_prose_about_the_orchestrator_is_not_flagged(tmp_path):
    """The codebase explains this rule in docstrings; a grep-based guard would
    fire on its own explanation and get deleted for crying wolf."""
    innocent = tmp_path / "innocent.py"
    innocent.write_text(
        '"""Do not import orchestrator here."""\n'
        '# from orchestrator import configure\n'
        'NOTE = "orchestrator"\n', encoding="utf-8")
    assert _orchestrator_imports(innocent) == []


@pytest.mark.parametrize("module", ["component_options", "cloud_options",
                                    "blueprint_capabilities", "validation"])
def test_the_modules_that_needed_it_most_are_clean(module):
    """Named individually because these are the ones that reached for it: they
    make capability decisions, which is exactly what the orchestrator knows."""
    assert _orchestrator_imports(API_DIR / f"{module}.py") == []

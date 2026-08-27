"""C13a: the recipe stops asserting a daemon it invented.

REQ-2026-0212 is the case. The closed loop worked — the agent corrected
`dotnet8` to `dotnet-sdk-8.0` by itself, with no human, and the machine
installed real .NET 8.0.130:

    --- packages ---
    dotnet-sdk-8.0-8.0.130-1.0.1.el9_8.x86_64

and the proof was then failed for this:

    dotnet8=inactive
    PORTAL FAILURE: dotnet8 did not start

There is no `dotnet8` daemon. There is no daemon for Java, Python, Node or any
other runtime. The guessed recipe declared `services: [code]` for every
technology it had never seen, which is not a cheap guess — it is a false claim
about the software, and it fails a machine that did everything right.

WHAT REPLACES IT IS A MEASUREMENT, not a smaller guess. The machine reports
which units the installed packages actually provide (`units_<key>`), so a
package that ships no unit is an ANSWER rather than an absence, and the next
rung can redraft with the real name instead of inventing one.
"""

from __future__ import annotations

import os
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

from api import ai_blueprint
from orchestrator import configure

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)


def _render(code="nginx"):
    return configure.render([{"technology_code": code}], "rhel",
                            configure.boot_report_url("PROOF-X", "oci-service-vm"))


# --- the guess stops claiming a daemon ----------------------------------------

def test_the_guess_declares_no_service():
    """THE test. `services: [code]` asserted that every unknown technology is a
    daemon named after itself, and failed .NET for not starting one."""
    profile = ai_blueprint._guessed_profile("dotnet8", "oci")

    assert profile["rhel"]["services"] == [], (
        "the guess still invents a daemon, so any runtime — .NET, Java, Python "
        "— fails its proof for not starting a service that cannot exist")


def test_the_guess_still_names_the_package_and_a_version_command():
    """Dropping the service must not drop the two claims that ARE testable."""
    profile = ai_blueprint._guessed_profile("dotnet8", "oci")

    assert profile["rhel"]["packages"] == ["dotnet8"]
    assert profile["version_command"], "nothing would prove the software exists"


# --- what replaces it: the machine measures -----------------------------------

def test_the_machine_reports_which_units_a_package_provides():
    text = _render("nginx")

    assert "units_nginx=" in text, (
        "the guess was removed and nothing measures the real service, so the "
        "answer is simply lost")
    assert "rpm -ql" in text, "the units are guessed rather than asked of the package"


def test_the_units_are_asked_of_every_installed_package():
    text = _render("redis7")
    assert "units_redis" in text or "units_redis7" in text


def test_the_report_script_is_valid_posix_shell():
    """A syntax error here is discovered by a machine that boots, installs
    nothing and reports nothing — the most expensive way to find a typo."""
    doc = yaml.safe_load(_render("nginx"))
    script = next(f["content"] for f in doc["write_files"]
                  if f["path"].endswith("report.sh"))

    shell = "/bin/sh" if os.path.exists("/bin/sh") else None
    if shell is None:                      # Windows checkout; the container test covers it
        pytest.skip("no POSIX shell on this host")
    result = subprocess.run([shell, "-n"], input=script, text=True,
                            capture_output=True)
    assert result.returncode == 0, result.stderr[:400]


def test_the_whole_document_still_parses_as_cloud_config():
    assert yaml.safe_load(_render("nginx"))["write_files"]


# --- a curated recipe is untouched --------------------------------------------

def test_a_recipe_that_really_has_a_service_still_declares_it():
    """Only the GUESS stopped claiming a daemon. A curated profile that names a
    real service — nginx does — must still start and check it."""
    text = _render("nginx")

    assert "systemctl is-active nginx" in text, (
        "a real service is no longer checked, so software that installs but "
        "never runs would certify")


# --- C13b: the machine resolves the binary the recipe got wrong ---------------

def _profile(tmp_path, monkeypatch, **over):
    import json
    prof = {"code": "dotnet8", "builds_on": "oci/service-vm", "ports": [],
            "expects": "8", "version_command": "dotnet8 --version 2>&1",
            "rhel": {"packages": ["dotnet-sdk-8.0"], "services": []}}
    prof.update(over)
    (tmp_path / "dotnet8.json").write_text(json.dumps(prof))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    return configure.render([{"technology_code": "dotnet8"}], "rhel",
                            configure.boot_report_url("PROOF-X", "oci-service-vm"))


def test_a_missing_binary_asks_the_packages_what_they_installed(tmp_path, monkeypatch):
    """REQ-2026-0212 in one line. The agent had already corrected the package to
    `dotnet-sdk-8.0` and the machine installed it — and the proof failed because
    the recipe still ran `dotnet8 --version`. No naming rule produces `dotnet`
    from `dotnet-sdk-8.0` reliably, so the machine asks."""
    text = _profile(tmp_path, monkeypatch)

    # SCOPED TO THE VERSIONS SECTION. `rpm -ql dotnet-sdk-8.0` also appears in
    # the units block, so an unscoped assertion passed against a build that had
    # removed the rescue entirely — the same two-sources mistake that made an
    # earlier `rpm -q` assertion useless.
    versions = text.split("--- versions ---")[-1].split("--- services ---")[0]

    assert "rpm -ql dotnet-sdk-8.0" in versions, (
        "the machine never asks the package what binary it provides")
    assert "binary_dotnet8=" in versions, (
        "the binary it used is not reported, so the ladder cannot redraft with it")


def test_the_binary_matching_the_technology_is_preferred(tmp_path, monkeypatch):
    """A package may install several binaries. `/usr/bin/dotnet` is the one a
    request for dotnet8 meant."""
    text = _profile(tmp_path, monkeypatch)
    assert '/dotnet[^/]*$' in text, "any binary would do, in file order"


def test_BOTH_paths_reach_the_version_comparison(tmp_path, monkeypatch):
    """The bug I nearly shipped. The first version put the fallback in the
    MISSING branch and left `GOT=` in the other one, so a rescued binary was
    run and its version never read — a fix that fixed nothing."""
    text = _profile(tmp_path, monkeypatch)

    lines = [l.strip() for l in text.splitlines()]
    got = next(i for i, l in enumerate(lines) if l.startswith("GOT=") or "GOT=$(" in l)
    guard = next(i for i, l in enumerate(lines) if 'if [ -n "$RAW" ]' in l)
    assert guard < got, (
        "the version comparison is not shared by both paths, so a binary the "
        "machine rescued is run and never read")


def test_a_recipe_whose_binary_EXISTS_is_unchanged(tmp_path, monkeypatch):
    """Curated recipes must keep running exactly the command they name. The
    fallback is a rescue, not a replacement."""
    text = _profile(tmp_path, monkeypatch)
    assert "command -v dotnet8" in text
    assert "RAW=$(dotnet8 --version 2>&1)" in text


def test_a_recipe_with_no_packages_has_nothing_to_ask(tmp_path, monkeypatch):
    """An ARCHIVE install has no package to interrogate, so the rescue cannot
    run and MISSING must still be reported rather than a version invented.

    Written as an archive WITH its unit, not as an empty recipe: a profile that
    installs nothing, or unpacks an archive and starts nothing, is refused
    outright — the proof-of-nothing rule — so neither ever reaches this code."""
    text = _profile(tmp_path, monkeypatch,
                    rhel={"packages": [], "services": ["dotnet8"]},
                    archive={"url": "https://example.invalid/x.tar.gz",
                             "sha256": "a" * 64, "dest": "/opt/dotnet8",
                             "user": "dotnet8",
                             "unit": {"description": "d",
                                      "exec_start": "/opt/dotnet8/bin/run"}})

    assert "version_dotnet8=MISSING" in text, (
        "with no package to ask, the machine claimed a version anyway")
    assert "rpm -ql" not in text.split("--- versions ---")[-1], (
        "it interrogated packages that do not exist")


def test_a_package_that_owns_no_binary_still_finds_the_command(tmp_path, monkeypatch):
    """REQ-2026-0215, and the second time .NET failed on a machine that had it
    installed and working.

    `rpm -ql dotnet-sdk-8.0` lists no /usr/bin entry at all: `/usr/bin/dotnet`
    belongs to `dotnet-host`, which dnf installed as a DEPENDENCY. The rescue
    asked what the named package OWNS when the question that matters is whether
    the command is on the machine.

    The stem is tried last and only if it is executable, so it is a verified
    answer rather than a second guess — and `binary_` reports which."""
    text = _profile(tmp_path, monkeypatch)
    versions = text.split("--- versions ---")[-1].split("--- services ---")[0]

    assert "command -v dotnet 2>/dev/null" in versions, (
        "when the package owns no binary the machine gives up, even though the "
        "command is right there")
    assert versions.index("rpm -ql") < versions.index("command -v dotnet 2>"), (
        "the stem is tried before the package's own file list, so a measured "
        "answer loses to a guessed one")


def test_no_diagnostic_key_is_parsed_as_a_FACT_by_the_verdict(tmp_path, monkeypatch):
    """REQ-2026-0217, and the machine had done everything right.

        dotnet-sdk-8.0-8.0.130                  installed
        version_binary_dotnet8=/usr/bin/dotnet   the rescue found the binary
        version_dotnet8=UNPROMISED (8.0.130)     .NET 8.0.130, read correctly

    and the proof failed on "binary_dotnet8 is the wrong version —
    /usr/bin/dotnet", because `boot_reports.verdict` parses every key beginning
    `version_` as a version and my diagnostic key began with it.

    The same shape as the package manager's prose being mistaken for a fact,
    which was solved by prefixing it. A key added to EXPLAIN something must not
    be read as a CLAIM about something."""
    from orchestrator import boot_reports

    import re

    text = _profile(tmp_path, monkeypatch)
    keys = set(re.findall(r'echo "([a-z][a-z0-9_]*)=', text))
    diagnostics = {k for k in keys if k.startswith("binary_")}
    assert diagnostics, "the binary the machine used is no longer reported"

    for key in diagnostics:
        assert not key.startswith(("version_", "http_", "image_", "repo_",
                                   "firewall_")), (
            f"{key} begins with a prefix the verdict parses, so an explanation "
            f"will be judged as a claim")

    # And prove it end to end on a report shaped like the real one.
    verdict = boot_reports.verdict(
        "os_family=rhel\n--- versions ---\n"
        "binary_dotnet8=/usr/bin/dotnet\n"
        "version_dotnet8=UNPROMISED (8.0.130)\n")
    assert verdict["ok"], verdict["problems"]

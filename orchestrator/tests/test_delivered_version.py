"""The machine reports the version it ACTUALLY has, and the portal compares.

WHY THIS FILE EXISTS. A catalogue entry called "Redis 7" installed a package
called `redis`, and on Oracle Linux 9 that is Redis 6.2 — the modular packages
are filtered out until the stream is enabled. The machine booted, the service was
active, redis-cli answered PONG, and every check the portal had said healthy. The
only thing wrong was the number, which nothing looked at.

The same trap is set in two more places right now: `python312` installs `python3`
and `nodejs20` installs `nodejs` on Debian, and neither recipe pins a version at
all. Whether the distribution's default happens to match the name is a question
only a machine can answer, and until it is asked "verified" would mean nothing
more than "some package installed".

So the machine is asked what it got, it compares that with what the catalogue
promised, and the portal refuses to call a mismatch a success.
"""

from __future__ import annotations

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import boot_reports, configure

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)


def _script(codes, family="rhel", version=""):
    components = [{"technology_code": c} for c in codes]
    if version:
        components[0]["version"] = version
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    doc = yaml.safe_load(configure.render(components, family, url))
    return next(f["content"] for f in doc["write_files"]
                if f["path"].endswith("report.sh"))


# --- Every version-named entry is checked -------------------------------------

VERSIONED = ["redis7", "java21", "python312", "nodejs20"]


@pytest.mark.parametrize("code", VERSIONED)
@pytest.mark.parametrize("family", ["rhel", "debian"])
def test_a_technology_whose_name_promises_a_version_is_checked(code, family):
    """An entry named for a version that nothing verifies is a promise waiting to
    be broken."""
    script = _script([code], family)
    assert f"version_{code}=OK" in script
    assert f"version_{code}=WRONG" in script, "there must be a negative branch"


def test_every_versioned_catalogue_entry_declares_how_to_check_itself():
    """The guard for the NEXT one added. A technology whose name carries a number
    and whose entry has no version_command would slip back into being unverifiable
    without anyone noticing."""
    missing = [
        code for code, spec in configure.TEMPLATES.items()
        if any(ch.isdigit() for ch in code) and not spec.get("version_command")
    ]
    assert not missing, (
        f"these entries name a version but cannot be checked: {missing}")


def test_a_technology_with_no_version_in_its_name_is_not_forced_to_have_one():
    """nginx's version is the REQUESTER's choice, not the catalogue's promise."""
    assert "expects" not in configure.TEMPLATES["nginx"]


def test_the_requesters_chosen_version_is_what_gets_checked():
    """When the requester picks nginx 1.24, the promise is 1.24 — not whatever
    the distribution defaults to."""
    script = _script(["nginx"], "rhel", version="1.24")
    assert "1.24|1.24.*)" in script
    assert "version_nginx=" in script


# --- The comparison itself ----------------------------------------------------

def test_the_match_is_a_prefix_not_a_substring():
    """THE trap. `7` appears inside `6.2.7`, so a substring test would pass the
    exact bug this exists to catch: Redis 6.2 sold as Redis 7."""
    script = _script(["redis7"], "rhel")
    assert "7|7.*)" in script, "matched with a dot boundary, not `case *7*`"
    assert "*7*" not in script


def test_the_raw_output_is_kept_in_the_report():
    """`wanted 3.12 got 3.9` tells you there is a problem. The raw line tells you
    what the machine actually ran, which is what gets it fixed."""
    assert "[$RAW]" in _script(["python312"], "rhel")


@pytest.mark.parametrize("code,command", [
    ("redis7", "redis-server --version"),
    ("java21", "java -version"),
    ("python312", "python3 --version"),
    ("nodejs20", "node --version"),
])
def test_each_technology_is_asked_in_its_own_language(code, command):
    """These tools announce themselves completely differently; one generic
    command would work for none of them."""
    assert command in _script([code], "rhel")


# --- The verdict --------------------------------------------------------------

def test_the_wrong_version_is_a_failure():
    report = ("redis-server 6.2.7\nredis=active\n"
              "version_redis7=WRONG wanted 7 got 6.2.7 [Redis server v=6.2.7]\n")
    result = boot_reports.verdict(report)
    assert result["ok"] is False
    assert "redis7 is the wrong version" in result["problems"][0]


def test_the_right_version_passes():
    assert boot_reports.verdict("version_redis7=OK (7.2.14)\n")["ok"] is True


def test_a_version_that_could_not_be_read_is_a_failure():
    """A missing binary reports `got none`, which must not read as 'no problem'."""
    report = "version_nodejs20=WRONG wanted 20 got none [sh: node: not found]\n"
    assert boot_reports.verdict(report)["ok"] is False


def test_the_failure_says_what_was_wanted_and_what_arrived():
    """A human reading 'wrong version' still has to go and look. The numbers are
    what make it actionable."""
    report = "version_python312=WRONG wanted 3.12 got 3.9.21 [Python 3.9.21]\n"
    problem = boot_reports.verdict(report)["problems"][0]
    assert "3.12" in problem and "3.9.21" in problem


def test_an_installed_package_does_not_excuse_a_wrong_version():
    """The precise shape of the Redis 6.2 failure: everything else was green."""
    report = ("redis 6.2.7-1.el9\nredis=active\nfirewall_6379=open\n"
              "version_redis7=WRONG wanted 7 got 6.2.7 [Redis server v=6.2.7]\n"
              "PORTAL: first-boot configuration finished\n")
    assert boot_reports.verdict(report)["ok"] is False

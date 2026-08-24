"""The machine's half of C7: ask the questions whose answers we were guessing.

Three things this adds to a machine's self-report, each replacing something a
human previously wrote down one technology at a time:

  * WHAT ELSE THE REPOSITORIES OFFER, when the guessed package is missing.
    REQ-2026-0188 was told "rabbitmq NOT INSTALLED" by a machine holding dnf and
    the full repository metadata. `rabbitmq-server` was one query away.
  * WHICH PORTS THE SOFTWARE ACTUALLY OPENED, by difference. RabbitMQ's report
    had an empty ports section because the drafted profile declared none.
  * WHETHER THE VERSION COMMAND'S BINARY EXISTS AT ALL. That same report said
    `version_rabbitmq=UNPROMISED (12)` for software that was never installed —
    the LINE NUMBER of a `command not found` error, read as a version.

The script runs under /bin/sh on a real cloud image. Every test here renders it
and checks the text, and one of them parses it with a real shell, because a
syntax error in this file is discovered by a machine that boots, installs
nothing, and reports nothing — the most expensive way to find a typo.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import boot_reports, configure

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")

GUESS = {"code": "rabbitmq", "builds_on": "oci/service-vm", "ports": [],
         "version_command": "rabbitmq --version 2>&1",
         "rhel": {"packages": ["rabbitmq"], "services": ["rabbitmq"]}}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)


def render(profile, code, tmp_path, monkeypatch, family="rhel"):
    (tmp_path / f"{code}.json").write_text(json.dumps(profile))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    return configure.render([{"technology_code": code}], family, url)


def script(profile, code, tmp_path, monkeypatch):
    doc = yaml.safe_load(render(profile, code, tmp_path, monkeypatch))
    return next(f["content"] for f in doc["write_files"]
                if f["path"].endswith("report.sh"))


# --- the name rule that replaces a dictionary ---------------------------------

def test_the_obvious_name_and_the_server_suffix_are_both_candidates():
    """`rabbitmq` finds nothing; `rabbitmq-server` is the package. That one fact
    cost a real machine and was about to cost a hand-written dictionary row."""
    assert configure.candidate_packages("rabbitmq") == ["rabbitmq", "rabbitmq-server"]


def test_a_catalogue_version_in_the_name_is_stripped_as_a_candidate():
    """`redis7` is a catalogue label; the package is `redis`. Same for nodejs20."""
    assert "redis" in configure.candidate_packages("redis7")
    assert "nodejs" in configure.candidate_packages("nodejs20")


def test_the_original_name_is_still_tried_first():
    """Stripping digits must not overshoot: nginx is nginx, and a technology
    whose name legitimately ends in a number would break if the bare form won."""
    assert configure.candidate_packages("redis7")[0] == "redis7"


@pytest.mark.parametrize("code", [
    "a; rm -rf /", "$(curl http://x)", "../../etc", "rabbit mq", "", "-leading",
])
def test_a_name_that_could_not_be_a_package_yields_no_candidates(code):
    """These go to dnf as root. A catalogue code is not a trusted string."""
    assert configure.candidate_packages(code) == []


# --- discovery on the machine -------------------------------------------------

def test_the_machine_is_asked_what_it_could_have_installed(tmp_path, monkeypatch):
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "available_rabbitmq=" in text, "the machine is never asked"
    assert "repoquery" in text
    assert "rabbitmq-server" in text, "the alternative name is never queried"


def test_discovery_is_skipped_when_the_package_is_already_installed(
        tmp_path, monkeypatch):
    """A working install must stay quiet. The query costs milliseconds, but a
    report full of answers nobody needs is how a report stops being read."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "if rpm -q rabbitmq >/dev/null 2>&1; then :; else" in text


def test_epel_is_only_reached_for_when_the_configured_repositories_answer_nothing(
        tmp_path, monkeypatch):
    """Enabling a repository is a standing grant of trust. It is not a first
    resort, and the name is fixed rather than discovered."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert configure._EPEL_RELEASE in text
    assert text.index("repoquery") < text.index(configure._EPEL_RELEASE), (
        "EPEL is installed before the configured repositories are even asked")


def test_the_machine_never_installs_what_it_discovered(tmp_path, monkeypatch):
    """THE security line of this increment. A name found at run time has passed
    no allow-list; it must travel back, face profile_rules, and be installed on
    the next rung — never by the machine that found it.

    Asserted by USE, not by absence: `$FOUND` legitimately appears in the loop
    that decides whether to keep searching. What must never happen is a
    discovered value reaching a command that changes the machine.
    """
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    for line in text.splitlines():
        if "$FOUND" not in line and '"$N"' not in line:
            continue
        allowed = ("FOUND=$(dnf" in line or "[ -n \"$FOUND\" ]" in line
                   or "[ -z \"$FOUND\" ]" in line
                   or "available_" in line)
        assert allowed, f"a discovered value reaches: {line.strip()}"


def _runcmd(rendered: str) -> str:
    """Only the commands the machine RUNS. The report script is written out
    first and quotes several of the same strings, so searching the whole
    document finds text inside a heredoc and calls it an ordering."""
    return rendered[rendered.index("runcmd:"):]


# --- the ports the software really opened -------------------------------------

def test_the_ports_are_measured_by_difference_not_by_attribution(
        tmp_path, monkeypatch):
    """RabbitMQ runs as `beam.smp`. Any rule matching a socket to a process by
    the technology's name would miss it entirely — and sshd was listening before
    the install, so a before/after difference can never claim port 22."""
    rendered = render(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "ports-before" in rendered, "no baseline is ever taken"
    assert "comm -13" in rendered
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "opened_ports=" in text


def test_the_baseline_is_taken_before_anything_is_installed(tmp_path, monkeypatch):
    run = _runcmd(render(GUESS, "rabbitmq", tmp_path, monkeypatch))
    # Existence asserted separately from ordering: with the baseline removed
    # entirely this raised ValueError from .index() rather than failing, and a
    # test that explodes tells you less than one that says what is missing.
    assert "ports-before" in run, "no baseline is taken before the install"
    assert run.index("ports-before") < run.index("dnf install"), (
        "the baseline is taken after the install, so it includes the software's "
        "own ports and the difference is always empty")


def test_a_missing_baseline_reports_unknown_and_not_none(tmp_path, monkeypatch):
    """"Nothing new opened" and "nobody measured" are different facts. Reporting
    the second as the first is how a missing measurement reads as a measured
    zero — a check that cannot fail."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "opened_ports=unknown" in text


# --- the version defect from REQ-2026-0188 ------------------------------------

def test_the_binary_is_checked_before_any_number_is_believed(
        tmp_path, monkeypatch):
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "command -v rabbitmq" in text
    assert "version_rabbitmq=MISSING" in text
    assert text.index("command -v rabbitmq") < text.index("RAW=$("), (
        "the version command runs before anything checks it exists")


def test_a_missing_binary_fails_the_machine():
    """`UNPROMISED (12)` passed. This is what should have happened."""
    result = boot_reports.verdict("version_rabbitmq=MISSING (rabbitmq)\n")
    assert result["ok"] is False
    assert "not on the machine" in result["problems"][0]


# --- it has to actually run ---------------------------------------------------

@pytest.mark.skipif(not shutil.which("sh"), reason="no POSIX shell available")
def test_the_report_script_is_valid_posix_shell(tmp_path, monkeypatch):
    """It runs under /bin/sh on a cloud image, where `<(...)` is a syntax error
    rather than a feature — as the first draft of the port diff used. A syntax
    error here is found by a machine that boots, reports nothing, and bills."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    path = tmp_path / "report.sh"
    path.write_text(text, newline="\n")
    done = subprocess.run([shutil.which("sh"), "-n", str(path)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_whole_thing_is_still_valid_cloud_config(tmp_path, monkeypatch):
    doc = yaml.safe_load(render(GUESS, "rabbitmq", tmp_path, monkeypatch))
    assert isinstance(doc, dict) and doc.get("write_files")


# --- a repository that arrives as a package -----------------------------------

EPEL_PROFILE = {
    "code": "rabbitmq", "builds_on": "oci/service-vm", "ports": [5672],
    "version_command": "rabbitmq-server --version 2>&1",
    "repo": {"release_package": "oracle-epel-release-el9"},
    "rhel": {"packages": ["rabbitmq-server"], "services": ["rabbitmq-server"]},
}


def test_a_release_package_repository_is_installed_not_fetched(
        tmp_path, monkeypatch):
    rendered = render(EPEL_PROFILE, "rabbitmq", tmp_path, monkeypatch)
    assert "oracle-epel-release-el9" in rendered
    assert "config-manager --add-repo" not in rendered, (
        "it tried to fetch a .repo file for a repository that ships as a package")


def test_the_repository_is_enabled_before_the_package_that_needs_it(
        tmp_path, monkeypatch):
    run = _runcmd(render(EPEL_PROFILE, "rabbitmq", tmp_path, monkeypatch))
    assert run.index("dnf install -y oracle-epel-release-el9") < run.index(
        "dnf install -y rabbitmq-server"), (
        "the package is installed before its repository exists")


def test_the_machine_reports_whether_the_release_package_arrived(
        tmp_path, monkeypatch):
    """Asked of rpm, not of the filesystem: a release package may write its
    definitions under any name, and what matters is whether it is installed."""
    text = script(EPEL_PROFILE, "rabbitmq", tmp_path, monkeypatch)
    assert "repo_rabbitmq=" in text
    assert "rpm -q oracle-epel-release-el9" in text


# --- discovery answers a question only a GUESS leaves open ---------------------

ARCHIVE_PROFILE = {
    "code": "keycloak", "builds_on": "oci/service-vm", "ports": [8080],
    "expects": "26", "version_command": "/opt/keycloak/bin/kc.sh --version",
    "archive": {"url": "https://example.com/keycloak-26.7.2.tar.gz",
                "sha256": "a" * 64, "dest": "/opt/keycloak", "user": "keycloak",
                "unit": {"exec_start": "/opt/keycloak/bin/kc.sh start-dev"}},
    "rhel": {"packages": ["java-21-openjdk-headless"], "services": ["keycloak"]},
}


def test_software_that_ships_as_a_tarball_is_not_searched_for(tmp_path, monkeypatch):
    """Keycloak IS an archive. Searching the repositories for it finds nothing,
    installs EPEL for no reason, and reports a `none` whose only meaning is
    "this was never a package". Discovery answers "is this packaged under a
    different name", and that question is only open for a guess."""
    text = script(ARCHIVE_PROFILE, "keycloak", tmp_path, monkeypatch)
    assert "available_keycloak=" not in text
    assert configure._EPEL_RELEASE not in text


def test_a_vendor_repository_recipe_is_not_searched_for_either(
        tmp_path, monkeypatch):
    text = script(EPEL_PROFILE, "rabbitmq", tmp_path, monkeypatch)
    assert "available_rabbitmq=" not in text


def test_the_guard_asks_about_the_package_that_was_declared(tmp_path, monkeypatch):
    """`redis7` declares the package `redis`. Guarding on the CODE means
    `rpm -q redis7` fails on a perfectly healthy machine, so discovery would run
    on every boot of a recipe that worked — the same false-negative shape as
    grepping repolist for `vault` when the repository is named `hashicorp`."""
    profile = {"code": "thing", "builds_on": "oci/service-vm", "ports": [],
               "version_command": "thing --version 2>&1",
               "rhel": {"packages": ["thing-actual"], "services": ["thing-actual"]}}
    text = script(profile, "thing", tmp_path, monkeypatch)
    assert "rpm -q thing-actual >/dev/null" in text
    assert "rpm -q thing >/dev/null" not in text


# --- the machine must report its OWN working (REQ-2026-0189) -------------------
#
# The first real run of C7 reported `available_rabbitmq=none` and nothing could
# say whether RabbitMQ is genuinely absent or whether the EPEL step failed and
# only Oracle's base repositories were ever searched — every command in that
# block ends in `|| true`. A missing measurement reading as a measured zero is
# the exact defect this increment exists to end.
#
# These assert the PRODUCER. Two plants that removed these lines from the
# rendered script passed every reader test in api/, because the reader was
# covered and the machine was not.

def test_the_machine_says_whether_epel_actually_arrived(tmp_path, monkeypatch):
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "epel_rabbitmq=" in text, "nothing records whether EPEL was enabled"
    for outcome in ("present", "added", "unavailable"):
        assert f"epel_rabbitmq={outcome}" in text, (
            f"the {outcome} case is never reported, so it reads as the others")


def test_epel_is_checked_by_rpm_after_installing_not_assumed(
        tmp_path, monkeypatch):
    """`dnf install || true` tells you nothing. The question is whether the
    package is THERE afterwards."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    after = text[text.index(f"dnf install -y {configure._EPEL_RELEASE}"):]
    assert f"rpm -q {configure._EPEL_RELEASE}" in after, (
        "it installs EPEL and never checks whether it arrived")


def test_the_control_probe_makes_a_negative_falsifiable(tmp_path, monkeypatch):
    """`bash` is in the base repositories of every image this portal builds. If
    the query cannot find that, it could not have found anything — and a `none`
    from such a search is not a fact about the software."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "queryable_rabbitmq=" in text, "the search cannot be shown to work"
    assert "repoquery" in text and "bash" in text


def test_the_probe_runs_whether_or_not_a_package_was_found(tmp_path, monkeypatch):
    """A probe that only runs on the failure path cannot corroborate a success,
    and one that only runs on success proves nothing at all."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    probe = text.index("PROBE=")
    assert probe > text.index("--- available ---")
    assert probe < text.index('echo "available_rabbitmq='), (
        "the probe is reported after the answer it is meant to qualify")


def test_the_working_lines_are_not_words_the_verdict_calls_broken(
        tmp_path, monkeypatch):
    """verdict() treats any `key=failed` as a broken machine. A diagnostic step
    that did not complete must not fail an otherwise healthy proof — a false
    alarm costs exactly as much trust as a missed failure."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("echo \"epel_", "echo \"queryable_")):
            assert "=failed" not in line and "=inactive" not in line, line


# --- installed is not enabled (REQ-2026-0190) ---------------------------------
#
# That request reported `epel=present, queryable=yes, available=none` and STILL
# could not settle the question: `rpm -q oracle-epel-release-el9` proves the
# release package is installed, not that the repository it carries is ENABLED. A
# repo can sit in /etc/yum.repos.d with enabled=0 and be searched by nothing.
# dnf's own enabled list is the only thing that closes it.

def test_the_machine_names_the_repositories_it_actually_searched(
        tmp_path, monkeypatch):
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "searched_rabbitmq=" in text, (
        "a `none` that cannot name where it looked settles nothing")
    assert "repolist --enabled" in text, (
        "it asks something other than dnf what dnf had enabled")


def test_the_repository_list_is_one_parseable_line(tmp_path, monkeypatch):
    """`dnf repolist` prints a table. verdict() reads key=value per line, so a
    raw table would scatter half a dozen unparseable lines through the report."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    line = next(l for l in text.splitlines() if "searched_rabbitmq=" in l)
    assert "paste -sd," in text, "the list is never flattened to one line"
    assert "${REPOS:-unknown}" in line, (
        "an unreadable repo list would report as an empty search rather than "
        "as an unknown one")


def test_it_is_reported_before_the_answer_it_qualifies(tmp_path, monkeypatch):
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert text.index("searched_rabbitmq=") < text.index('echo "available_rabbitmq=')


# --- installed is not ENABLED, proved on a machine (REQ-2026-0191) ------------
#
# The `searched_` line was added to distinguish "this software is genuinely
# absent" from "EPEL was never searched". On its first real run it distinguished
# them, and the answer was the second:
#
#     epel_rabbitmq=present
#     searched_rabbitmq=ol9_UEKR8,ol9_addons,ol9_appstream,
#                       ol9_baseos_latest,ol9_ksplice,ol9_oci_included
#     available_rabbitmq=none
#
# Oracle ships `oracle-epel-release-el9` ON the OL9 image — hence `present`
# rather than `added` — and the repository it carries is defined with enabled=0.
# The release package being installed said nothing about whether anything
# searched it, which is exactly what that line exists to reveal.

def test_a_repository_that_is_defined_but_disabled_is_enabled(
        tmp_path, monkeypatch):
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "config-manager --enable" in text, (
        "the release package is installed and its repository left disabled, so "
        "nothing ever searches it")


def test_the_repository_to_enable_is_discovered_not_named(tmp_path, monkeypatch):
    """The id differs between Oracle Linux releases. Hard-coding
    `ol9_developer_EPEL` would be the same recalled-rather-than-measured mistake
    as Vault's `expects: "1"` — right until the day it is not."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert "repolist --disabled" in text
    assert "ol9_developer_EPEL" not in text, "a repository id was recalled"


def test_only_repositories_already_on_the_machine_are_enabled(
        tmp_path, monkeypatch):
    """Enabling is not adding. Every candidate comes from `dnf repolist`, so
    each was already defined by Oracle's own release package — nothing new is
    trusted, and no URL is introduced."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    enable = next(l for l in text.splitlines() if "config-manager --enable" in l)
    assert "repolist --disabled" in enable, (
        "the repository to enable does not come from what the machine has")
    assert "http" not in enable, "a URL reached the enable step"


def test_enabling_happens_before_the_search_that_needs_it(tmp_path, monkeypatch):
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    enable = text.index("config-manager --enable")
    # The SECOND search — the one after EPEL is dealt with — must follow it.
    second = text.index("repoquery", text.index("epel_rabbitmq=unavailable"))
    assert enable < second, "it searches again before enabling anything"


def test_the_searched_list_is_reported_after_enabling(tmp_path, monkeypatch):
    """Otherwise it names the repositories from before the change and the
    soundness rule judges the wrong search."""
    text = script(GUESS, "rabbitmq", tmp_path, monkeypatch)
    assert text.index("config-manager --enable") < text.index("searched_rabbitmq=")

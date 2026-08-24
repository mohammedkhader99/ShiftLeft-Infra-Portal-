"""What the machine found out, read back off its own report (C7).

REQ-2026-0188 asked for RabbitMQ. The agent guessed `dnf install rabbitmq`,
booted a real machine, and the report came back:

    package rabbitmq is not installed
    rabbitmq NOT INSTALLED
    PORTAL FAILURE: package install did not complete

True, useless, and the end of the road — the ladder had no vendor repository for
RabbitMQ and no archive, so the request went to manual fulfilment. Meanwhile that
machine was running Oracle Linux with dnf and the complete repository metadata in
front of it, and the answer it could not be bothered to give was `rabbitmq-server`.

THE POINT OF THIS MODULE is that the fix is not "write down rabbitmq-server". It
is that a technology should never have to be written down at all. C6e generalised
the LADDER and left the KNOWLEDGE hand-curated, so every new component cost one
failed request plus a dictionary row typed in by a human. The machine can answer
for itself, once, for every candidate at the price of one.

WHAT ARRIVES HERE IS UNTRUSTED. It is text a machine wrote, fetched over the
signed channel, and every name in it is destined to be handed to a package
manager as root. It is parsed defensively and validated against the same
allow-lists that govern a drafted profile — `profile_rules` decides, not this
module, and never a substring check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from common import profile_rules

# Repository ids that mean "this came from EPEL", which is enabled by installing
# a release package rather than by fetching a .repo file. Matched on the whole
# value after lowercasing, never as a substring: `ol9_developer_EPEL` and
# `epel` are both EPEL, and a repository merely CALLED something-epel-something
# by a third party is not.
_EPEL_REPO_IDS = frozenset({
    "epel", "epel-next", "ol9_developer_epel", "ol8_developer_epel",
    "ol9_developer_epel_modular",
})


@dataclass
class Finding:
    """What one machine learned about installing one technology."""

    code: str
    package: str = ""
    repo_id: str = ""
    ports: list[int] = field(default_factory=list)

    @property
    def needs_epel(self) -> bool:
        return self.repo_id.strip().lower() in _EPEL_REPO_IDS

    @property
    def usable(self) -> bool:
        """Whether this says anything the agent can actually act on.

        A finding with no package names nothing to install. A finding whose
        package is only reachable from a repository we have no sanctioned way to
        enable is worse than useless — it would draft a recipe that installs
        nothing and spend a machine proving it.
        """
        if not self.package:
            return False
        return not self.repo_id or self.needs_epel


def _reachable(repo_id: str, enabled: set[str]) -> bool:
    """Whether a package in this repository can actually be installed.

    ONE RULE, USED BY BOTH PATHS. It existed twice — once for a package named
    exactly and once for a package found by searching — and the two diverged the
    moment one was corrected: `Finding.usable` pattern-matches repository names
    against a list of EPEL ids, which is the right question for a repo the agent
    must reach for and the wrong one for `ol9_appstream`, a base repository that
    needs nothing done to it. So an exact hit in appstream was refused while a
    searched hit in the same repository was accepted.

    The honest test is measured, not matched: the machine reported which
    repositories it had ENABLED when it searched, so a package found in one of
    them is installable by definition. EPEL is allowed even when the machine did
    not list it, because enabling EPEL is a step this portal knows how to take.
    """
    if not repo_id:
        # No repository named: already reachable, or the machine did not say.
        return True
    if repo_id.strip().lower() in _EPEL_REPO_IDS:
        return True
    return bool(enabled) and repo_id in enabled


def _searched_repos(report: str, code: str) -> set[str]:
    """The repositories the machine actually had enabled when it searched."""
    key = profile_rules.report_key(code)
    for line in (report or "").splitlines():
        line = line.strip()
        if line.startswith(f"searched_{key}="):
            value = line.split("=", 1)[1].strip()
            if value and value != "unknown":
                return {r.strip() for r in value.split(",") if r.strip()}
    return set()


def _ports_from(report: str) -> list[int]:
    """The opened_ports line, for a finding assembled outside read()."""
    for line in (report or "").splitlines():
        line = line.strip()
        if line.startswith("opened_ports="):
            return _ports(line.split("=", 1)[1])
    return []


def _ports(value: str) -> list[int]:
    """Port numbers from an `opened_ports=` line.

    `none` (nothing new opened) and `unknown` (no baseline was taken) both yield
    nothing, and deliberately so — but they are different facts on the machine's
    report and a human reading it can tell them apart.
    """
    out: list[int] = []
    for part in (value or "").replace(" ", "").split(","):
        if part.isdigit() and 0 < int(part) < 65536 and int(part) not in out:
            out.append(int(part))
    return sorted(out)


# Packages that match a search but are not the software. Every ecosystem ships
# these alongside the thing itself, and a wildcard finds them all: installing
# `dotnet-apphost-pack-8.0` succeeds, installs nothing runnable, and reports
# healthy — which is the silent-success shape this project keeps paying for.
_NOT_THE_SOFTWARE = (
    "-devel", "-debuginfo", "-debugsource", "-doc", "-docs", "-javadoc",
    "-test", "-tests", "-example", "-examples", "-source", "-src",
    "-targeting-pack", "-apphost-pack", "-templates", "-symbols", "-headers",
)


def rank_matches(matches: list[tuple[str, str]], code: str) -> list[tuple[str, str]]:
    """What a wildcard search found, best candidate first.

    REQ-2026-0197 asked for .NET 8. `dotnet8`, `dotnet8-server`, `dotnet` and
    `dotnet-server` do not exist on Oracle Linux 9 — but `dotnet-sdk-8.0` does,
    and no name-shape rule was ever going to generate it. Searching finds it;
    the problem then becomes choosing among what a search returns, because
    `*dotnet*` also matches a dozen packs, templates and debug symbols.

    THREE PREFERENCES, IN ORDER, AND EACH ONE IS A GUESS THE MACHINE WILL TEST.
    Nothing here is certain — what makes it safe is that the choice is proved on
    a real machine before anything is certified, and a wrong pick costs one
    sandbox VM and says so.

      * a package carrying the version the catalogue asked for, because
        `dotnet8` means 8 and `dotnet-sdk-9.0` is a different thing;
      * a package whose name STARTS with the stem, so `dotnet-sdk-8.0` beats
        `aspnetcore-runtime-8.0` for a request that said dotnet;
      * the shortest remaining name, because qualifiers are how a distribution
        says "this is an accessory, not the thing".
    """
    stem = (code or "").rstrip("0123456789").lower()
    version = (code or "")[len(stem):]

    def usable(name: str) -> bool:
        # STRIP THE VERSION FIRST. `dotnet-templates-8.0` ends in `-8.0`, not in
        # `-templates`, so an endswith check on the raw name never fires and a
        # templates package ranked third for a request that wanted a runtime.
        bare = re.sub(r"-\d+(?:\.\d+)*$", "", (name or "").lower())
        return bool(name) and not bare.endswith(_NOT_THE_SOFTWARE)

    def key(entry: tuple[str, str]):
        name = entry[0].lower()
        return (
            0 if (version and version in name) else 1,
            0 if name.startswith(stem) else 1,
            len(name),
            name,
        )

    return sorted([m for m in matches if usable(m[0])], key=key)


def searched_matches(report: str, code: str) -> list[tuple[str, str]]:
    """(package, repository) pairs a wildcard search reported, unranked."""
    key = profile_rules.report_key(code)
    out: list[tuple[str, str]] = []
    for line in (report or "").splitlines():
        line = line.strip()
        if not line.startswith(f"matches_{key}="):
            continue
        for pair in line.split("=", 1)[1].split(","):
            name, _, repo = pair.partition("|")
            name = name.strip()
            # THE ALLOW-LIST DECIDES, as everywhere else: this name is destined
            # for a package manager running as root and it arrived as text a
            # machine wrote.
            if name and profile_rules.CODE.match(name):
                out.append((name, repo.strip()))
    return out


def was_searched(report: str) -> bool:
    """Whether this report came from a machine that was ASKED what was available.

    Three outcomes must stay distinguishable, and collapsing any two of them
    causes a different bug:

      * a package was found            -> act on it
      * the machine looked, found none -> a settled fact; do not re-buy it
      * the machine was never asked    -> we know nothing, and a refutation
                                          recorded from such a report cannot
                                          answer the question we now put

    The third is not hypothetical: every refutation recorded before C7 shipped
    has a report with no discovery section at all. Reading those as "looked and
    found nothing" would permanently skip the one rung that could now succeed —
    RabbitMQ would go to manual fulfilment for thirty days holding a refutation
    whose machine was never asked the question.
    """
    return "--- available ---" in (report or "")


def read(report: str, codes: list[str]) -> dict[str, Finding]:
    """Findings for each technology named, from one machine's report text.

    Keyed by the ORIGINAL catalogue code, not by the sanitised report key, so a
    caller never has to reverse `report_key` (which is lossy: `oracle-db` and
    `oracle_db` both report as `oracle_db`).
    """
    by_key = {profile_rules.report_key(c): c for c in codes or []}
    found: dict[str, Finding] = {}
    ports: list[int] = []

    for line in (report or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)

        if key == "opened_ports":
            ports = _ports(value)
            continue
        if not key.startswith("available_"):
            continue

        code = by_key.get(key[len("available_"):])
        if code is None:
            continue
        # `name|repoid`, as the report script emits it — the separator is
        # always present on a real answer because the query format contains it.
        #
        # THE SENTINEL IS NOT A PACKAGE. The script emits `${FOUND:-none}`, and
        # `none` is a perfectly well-formed package name as far as the allow-list
        # is concerned: without this the agent would read "I found nothing" as
        # "install a package called none", draft a profile for it, and spend a
        # machine proving that `dnf install none` does not work. Requiring the
        # separator distinguishes an answer from the absence of one by SHAPE
        # rather than by trusting a magic word.
        if "|" not in value:
            continue
        name, _, repo_id = value.partition("|")
        name = name.strip()
        # THE ALLOW-LIST DECIDES. This name goes to dnf as root on the next
        # rung, and it arrived as text from a machine.
        if not name or not profile_rules.CODE.match(name):
            continue
        found[code] = Finding(code=code, package=name, repo_id=repo_id.strip())

    for finding in found.values():
        finding.ports = list(ports)
    return found


def search_was_sound(report: str, code: str) -> bool:
    """Whether the machine's search could have found the answer if it existed.

    REQ-2026-0189 reported `available_rabbitmq=none` and nothing could say
    whether RabbitMQ is genuinely absent or whether the search itself failed —
    every command in that block ends in `|| true`. A `none` from a broken search
    is not a fact about the world, and remembering it as one takes a working
    component off the menu for thirty days on the strength of our own bug.

    Two things make a search sound, and the machine now reports both:

      * `queryable_<key>=yes` — the control probe found `bash`, which is in the
        base repositories of every image this portal builds. If the query cannot
        find that, it could not have found anything.
      * EPEL was reached for and arrived (`present`/`added`) if the configured
        repositories answered nothing. `unavailable` means only Oracle's own
        repositories were ever searched, which is half a search.
      * `searched_<key>` names the repositories dnf actually had ENABLED, and
        EPEL is among them. Installed is not enabled, and only this line can
        tell the two apart.
    """
    key = profile_rules.report_key(code)
    values: dict[str, str] = {}
    for line in (report or "").splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            values[k] = v.strip()
    if values.get(f"queryable_{key}") != "yes":
        return False
    if values.get(f"epel_{key}", "present") not in ("present", "added"):
        return False
    # AND EPEL MUST HAVE BEEN IN THE LIST THE MACHINE ACTUALLY SEARCHED.
    #
    # REQ-2026-0190 reported `epel=present, queryable=yes, available=none` and
    # still could not settle anything: `rpm -q oracle-epel-release-el9` proves
    # the release package is INSTALLED, not that the repository it carries is
    # ENABLED. A repo sitting in /etc/yum.repos.d with enabled=0 is searched by
    # nothing, so "RabbitMQ is not in EL9 or EPEL" and "EPEL was never searched"
    # remained indistinguishable — the same defect one level deeper.
    #
    # dnf's own enabled list is the only thing that answers it.
    searched = values.get(f"searched_{key}", "")
    if not searched or searched == "unknown":
        return False
    if f"epel_{key}" not in values:
        # The configured repositories answered before EPEL was ever needed, so
        # there is nothing to corroborate.
        return True
    return any("epel" in repo.lower() for repo in searched.split(","))


def listening_inside(report: str, code: str) -> list[int]:
    """Ports the machine saw LISTENING inside the container, from /proc.

    A different question from what is published on the host, and the only one
    that can narrow anything: a published port binds on the host whether or not
    the container listens, so podman's proxy answers either way and a host-side
    socket diff never narrows.

    `library/rabbitmq` DECLARES six ports — AMQP, AMQPS, epmd, clustering and
    two Prometheus endpoints — and a default container listens on far fewer.
    Opening all six in a real machine's firewall is more surface than the
    service needs.
    """
    key = profile_rules.report_key(code)
    for line in (report or "").splitlines():
        line = line.strip()
        if line.startswith(f"listening_inside_{key}="):
            return _ports(line.split("=", 1)[1])
    return []


def finding_for(report: str, code: str) -> dict | None:
    """What the ladder should conclude from one machine's report, in three states.

    Lived in a closure in api.main until a plant test walked straight past it —
    removing the `was_searched` guard broke nothing, because nothing could reach
    the code to test it. Logic worth getting right is worth being able to test.

      * ``None`` — the machine was never asked. Every report written before C7
        shipped looks like this, and a refutation resting on one cannot answer
        the question the ladder now puts.
      * ``{}``   — it looked and there was nothing. A settled fact; do not spend
        another machine re-buying it.
      * a dict   — a package to try, with what it will take to reach it.
    """
    if not was_searched(report):
        return None
    enabled = _searched_repos(report, code)
    finding = read(report, [code]).get(code)
    if finding is not None and finding.package and _reachable(finding.repo_id, enabled):
        return {"package": finding.package, "repo_id": finding.repo_id,
                "ports": list(finding.ports)}
    if finding is None or not finding.usable:
        # NAMING IT FAILED; SEARCHING MAY NOT HAVE. `dotnet-sdk-8.0` exists and
        # no shape rule generates it, so the wildcard result is consulted before
        # concluding the software is absent.
        for name, repo in rank_matches(searched_matches(report, code), code):
            # REACHABLE IS MEASURED, NOT GUESSED. `Finding.usable` asks whether
            # the repository is EPEL, which is the right question for a package
            # the agent must reach for — and the wrong one here: the machine
            # already told us which repositories it had enabled when it searched,
            # so a package found in one of them is reachable by definition.
            # Pattern-matching repository names would have refused
            # `dotnet-sdk-8.0` from ol9_appstream, a base repository that needs
            # nothing done to it at all.
            if not _reachable(repo, enabled):
                continue
            return {"package": name, "repo_id": repo,
                    "ports": _ports_from(report)}
        # A NEGATIVE IS ONLY A FACT IF THE SEARCH COULD HAVE FOUND SOMETHING.
        # An unsound search is indistinguishable, from here, from never having
        # asked — and `None` is what says that honestly.
        return {} if search_was_sound(report, code) else None
    return {"package": finding.package, "repo_id": finding.repo_id,
            "ports": list(finding.ports)}


def profile_from(finding: Finding, target: str = "oci") -> dict | None:
    """A technology profile drafted from what a machine reported, or None.

    Every value here was measured on a machine minutes ago rather than recalled:
    the package name came from the package manager's own metadata, the repository
    id from the same query, and the ports from the difference between what was
    listening before the install and after it.

    NO `expects`. Nothing here promises a version — the repository serves what it
    serves — so the machine is asked and its answer is recorded, not compared.
    """
    if not finding.usable:
        return None
    profile: dict = {
        "code": finding.code,
        "builds_on": "oci/service-vm" if target == "oci" else "",
        "ports": list(finding.ports),
        "version_command": f"{finding.package} --version 2>&1",
        "rhel": {"packages": [finding.package], "services": [finding.package]},
        "_note": (
            f"DRAFT — every value measured on a machine, not recalled: a real "
            f"machine reported that {finding.package!r} is what "
            f"{finding.code!r} is actually packaged as"
            + (f" (from {finding.repo_id})" if finding.repo_id else "")
            + (f", and that it opened port(s) "
               f"{', '.join(str(p) for p in finding.ports)}"
               if finding.ports else "")
            + ". Proven by booting another machine; that machine decides."),
    }
    if finding.needs_epel:
        # A repository enabled by installing a release package. The NAME IS
        # FIXED, taken from the closed set in profile_rules — never from the
        # report — because a release package installs a repository and its
        # signing key together, and root would then trust that publisher for
        # everything it ever serves. The machine is allowed to tell us WHICH
        # known repository a package lives in; it is not allowed to nominate one.
        profile["repo"] = {"release_package": "oracle-epel-release-el9"}
    return profile

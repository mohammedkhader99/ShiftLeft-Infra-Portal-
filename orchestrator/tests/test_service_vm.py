"""The generic "service on a VM" blueprint (F-CAT-10, GAP-ANALYSIS step 6).

Most of the catalogue is not a distinct cloud resource — it is a package on a
machine. Those technologies share one blueprint, and what differs between them
(package, systemd unit, port) is data in `configure.TEMPLATES`.

The tests that matter here are the ones guarding SILENT success, because that is
how this blueprint fails: a VM that builds fine with nothing installed on it, or
a service running correctly behind a port nobody opened. Both report success.
"""

import pytest

from orchestrator import blueprint_registry, configure, provisioner

MANIFEST_REF = "oci/service-vm"
KIND = "oci-service-vm"


def _manifest():
    return blueprint_registry.for_resource_kind(KIND)


def _oci_env(monkeypatch):
    monkeypatch.setenv("OCI_TENANCY_OCID", "t")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "c")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    monkeypatch.setenv("OCI_COMPUTE_SUBNET_OCID", "ocid1.subnet..x")


# --- The blueprint is wired up -----------------------------------------------

def test_the_blueprint_ships_and_dispatches_to_its_own_module():
    bp = _manifest()
    assert bp and bp["ref"] == MANIFEST_REF
    module = provisioner._module_dir("oci", KIND)
    assert module.name == "service-vm"
    assert (module / "main.tf").is_file()


def test_every_technology_it_claims_has_a_recipe():
    """The guard that keeps the catalogue honest: a blueprint listing a
    technology it has no profile for would advertise automation and then build a
    machine with nothing on it."""
    for code in _manifest()["builds"]:
        prof = configure.profile_for(code)
        assert prof, f"{code} is in the blueprint's builds but has no profile"
        assert prof["packages"], f"{code} has a profile that installs nothing"


# --- Silent success guard 1: an unconfigured build ---------------------------

def test_it_refuses_to_build_when_first_boot_configuration_is_off(monkeypatch):
    """With CONFIG_ENABLED off, render() returns "" and this blueprint would
    create a bare VM and report success. Refusing names the setting instead."""
    _oci_env(monkeypatch)
    monkeypatch.delenv("CONFIG_ENABLED", raising=False)
    with pytest.raises(provisioner.ProvisionError) as exc:
        provisioner._require_cloud("oci", KIND, creating=True)
    assert "CONFIG_ENABLED" in str(exc.value)

    monkeypatch.setenv("CONFIG_ENABLED", "true")
    provisioner._require_cloud("oci", KIND, creating=True)  # no longer refused


def test_destroying_is_never_blocked_by_the_configuration_gate(monkeypatch):
    """Turning the switch off after provisioning must not trap the resource."""
    _oci_env(monkeypatch)
    monkeypatch.delenv("CONFIG_ENABLED", raising=False)
    provisioner._require_cloud("oci", KIND, creating=False)


# --- Silent success guard 2: a service behind a closed port ------------------

def test_the_ports_opened_are_the_ports_declared(monkeypatch):
    """One declaration drives both the network rules and the OS firewall. If they
    could drift, a correctly installed service would simply be unreachable."""
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    components = [{"technology_code": "nginx"}]

    declared = configure.ports_for(components)
    assert declared == [80]

    user_data = configure.render(components)
    assert "--add-port=80/tcp" in user_data
    assert "firewall-cmd --reload" in user_data

    # ...and the same list reaches Terraform.
    _oci_env(monkeypatch)
    v = provisioner._cloud_vars("oci", "web", {"env": "uat"}, KIND,
                                {"user_data": user_data, "service_ports": declared})
    assert v["service_ports"] == [80]
    assert v["instance_name"] == "web"
    assert v["user_data"] == user_data


def test_the_rendered_cloud_config_is_valid_yaml(monkeypatch):
    """It is fed to cloud-init, which parses it as YAML. Producing something that
    only LOOKS like cloud-config gets you a booted VM that ran nothing."""
    import yaml
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    for codes in (["nginx"], ["redis7"], ["nginx", "redis7"], ["java21"]):
        text = configure.render([{"technology_code": c} for c in codes])
        doc = yaml.safe_load(text)
        assert isinstance(doc, dict), f"{codes} did not parse as a mapping"
        assert text.startswith("#cloud-config"), "cloud-init needs that first line"


def test_every_runcmd_entry_is_a_command_not_a_mapping(monkeypatch):
    """THE bug this file exists to prevent.

    A bare YAML scalar containing ': ' is a MAPPING. `echo 'PORTAL: failed'`
    became {"echo 'PORTAL": "failed'"}, cloud-init raised "Failed to shellify"
    and abandoned the WHOLE runcmd block — so the marker file appeared, nothing
    was installed, and the VM looked fine. Every message here contains a colon,
    so this is not an edge case; it is the normal path.
    """
    import yaml
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    doc = yaml.safe_load(configure.render([{"technology_code": "nginx"}]))
    runcmd = doc.get("runcmd") or []
    assert runcmd, "nginx should produce commands"
    bad = [c for c in runcmd if not isinstance(c, str)]
    assert not bad, f"these parsed as {type(bad[0]).__name__}, not commands: {bad}"
    # ...and the colon survived into the command, rather than splitting it.
    assert any("PORTAL FAILURE: package install did not complete" in c for c in runcmd)


def test_redis_enables_the_module_stream_before_installing(monkeypatch):
    """Found by booting a VM, not by reading code.

    `dnf install redis` on Oracle Linux 9 installs 6.2: the modular packages are
    filtered out until the stream is enabled, so a catalogue entry named "Redis
    7" delivered Redis 6 — installed, running, answering, and a major version
    behind what was promised. With the stream enabled it installs 7.2.14,
    confirmed on a real machine.
    """
    import yaml
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    runcmd = yaml.safe_load(configure.render([{"technology_code": "redis7"}]))["runcmd"]
    enable = [i for i, c in enumerate(runcmd) if "module enable" in c and "redis:7" in c]
    install = [i for i, c in enumerate(runcmd) if "install -y redis" in c]
    assert enable, "redis7 must enable the redis:7 module stream"
    assert install, "redis7 must install redis"
    assert enable[0] < install[0], (
        "the stream has to be enabled BEFORE the install, or the default stream "
        "is already resolved and the wrong major version comes down")


def test_the_chosen_version_becomes_the_module_stream(monkeypatch):
    """The component detail form's whole justification.

    A version dropdown that does not change what gets installed is the
    Redis-6-sold-as-7 bug with a menu in front of it: the catalogue, the price and
    the approval all say 1.24 and the machine runs 1.20. The chosen version has to
    reach the install command or it should not be offered at all.
    """
    import yaml
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    runcmd = yaml.safe_load(
        configure.render([{"technology_code": "nginx", "version": "1.24"}]))["runcmd"]
    enable = [c for c in runcmd if "module enable" in c]
    assert enable, "a chosen version must enable the matching module stream"
    assert "nginx:1.24" in enable[0]
    assert "nginx:1.20" not in " ".join(runcmd), "the default stream must not win"


def test_a_version_the_requester_did_not_choose_falls_back_to_the_pinned_stream(monkeypatch):
    """Every request raised before the detail form carries no version, and must
    still get the stream the profile pins — this is what keeps Redis on 7."""
    import yaml
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    runcmd = yaml.safe_load(configure.render([{"technology_code": "redis7"}]))["runcmd"]
    assert any("redis:7" in c for c in runcmd)


def test_the_marker_file_records_the_version_that_was_asked_for(monkeypatch):
    """So a machine running the wrong version can be told apart from a machine
    that was asked for the wrong version — the first is our bug, the second is
    not, and on the VM they look identical."""
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    text = configure.render([{"technology_code": "nginx", "version": "1.24"}])
    assert "technologies=nginx 1.24" in text


def test_a_technology_without_a_module_stream_emits_none(monkeypatch):
    """Most technologies need no stream; an empty declaration must add nothing."""
    import yaml
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    runcmd = yaml.safe_load(configure.render([{"technology_code": "nginx"}]))["runcmd"]
    assert not [c for c in runcmd if "module enable" in c]


def test_a_technology_with_no_declared_port_opens_none(monkeypatch):
    """Redis has no port on purpose: it ships without authentication, so opening
    6379 to the subnet would publish an unauthenticated data store. Installed and
    running locally is the intended outcome, not a half-finished one."""
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    components = [{"technology_code": "redis7"}]
    assert configure.ports_for(components) == []
    user_data = configure.render(components)
    assert "--add-port" not in user_data
    # ...but it is still genuinely installed and started.
    assert "redis" in user_data
    assert "systemctl enable --now redis" in user_data


def test_ports_from_several_technologies_are_merged_without_duplicates(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    assert configure.ports_for([{"technology_code": "nginx"},
                                {"technology_code": "apache"},
                                {"technology_code": "redis7"}]) == [80]


def test_an_admin_can_correct_a_port_without_a_code_change(monkeypatch):
    """Package and port names are image-dependent; CONFIG_PACKAGE_MAP already
    overrides the former, and must carry the latter with it."""
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.setenv("CONFIG_PACKAGE_MAP",
                       '{"nginx": {"packages": ["nginx"], "services": ["nginx"], "ports": [8080]}}')
    assert configure.ports_for([{"technology_code": "nginx"}]) == [8080]


# --- Nothing is claimed as proven --------------------------------------------

def test_only_booted_technologies_claim_to_be_verified():
    """VERIFIED_CODES is a claim that a human booted the VM and checked the
    service answered. Generating a blueprint does not earn an entry in it.

    nginx and redis7 were booted on 11 Aug 2026 and asked directly — nginx
    returned HTTP 200, redis returned PONG on 7.2.14 — with the evidence recorded
    beside the set.

    Extended 15 Aug 2026 by REQ-2026-0139 (Oracle Linux 9.8) and REQ-2026-0140
    (Ubuntu 24.04.4): one machine each, five technologies on each, and every one
    reported the version it actually received. java21 and redis7 passed on both;
    python312 passed on Ubuntu only and nodejs20 on Oracle Linux only — the two
    failures are recorded in REFUTED, not quietly omitted.
    """
    assert configure.VERIFIED_CODES == {
        "nginx", "redis7", "java21", "python312", "nodejs20", "apache"}, (
        "a code was marked verified — that claim needs a real boot test behind it")


def test_the_module_enables_run_command_explicitly():
    """Run Command is how the portal ASKS a machine whether its software really
    installed — the only check that needs no inbound access to a private-only VM.

    It is enabled by default on Oracle Linux images and NOT on Ubuntu ones, so
    inheriting the image default made a machine's verifiability depend on which
    OS the requester happened to pick. Found before booting an Ubuntu VM, not
    after wasting one. A VM we cannot interrogate is a VM whose success we can
    only assume, and assuming success is how a service-vm that installed nothing
    shipped in the first place.
    """
    from pathlib import Path
    main_tf = (Path(__file__).resolve().parents[1]
               / "terraform" / "oci" / "service-vm" / "main.tf").read_text(encoding="utf-8")
    assert "agent_config" in main_tf, "the module must state the agent plugins, not inherit them"
    assert "Compute Instance Run Command" in main_tf
    assert "ENABLED" in main_tf
    assert "is_management_disabled = false" in main_tf


def test_only_booted_combinations_claim_to_be_verified():
    """Each claim is a (technology, OS family) PAIR and needs its own machine.

    This used to compare two flat sets, which can only express their
    cross-product: recording that nginx works on Ubuntu would have silently
    claimed redis7 does too, on the strength of a VM nobody booted. Plausible
    package names standing in for checked ones is exactly what delivered Redis
    6.2 under a catalogue entry called "Redis 7".
    """
    assert configure.VERIFIED == {
        ("nginx", "rhel"), ("nginx", "debian"),
        ("redis7", "rhel"), ("redis7", "debian"),
        ("java21", "rhel"), ("java21", "debian"),
        ("python312", "debian"), ("python312", "rhel"),
        ("nodejs20", "rhel"),      # Ubuntu gave 18.19.1 — see REFUTED
        # Proven by its OWN blueprint's template, not by this module. The record
        # holds proven COMBINATIONS whatever proved them — reading it as "proven
        # through configure.py" is what made the gate refuse a working web server.
        ("apache", "rhel"),
    }, ("a combination was marked verified — boot a VM on it first, and record "
        "the evidence beside the entry")


def test_a_verified_technology_is_not_verified_on_every_family():
    """THE point of the pair. nginx on Ubuntu is proven; redis7 on Ubuntu is a
    package name someone read in a manual."""
    assert configure.is_verified("python312", "debian") is True
    assert configure.is_verified("nodejs20", "rhel") is True
    assert configure.is_verified("nodejs20", "debian") is False


def test_a_disproven_combination_says_what_the_machine_measured():
    """Stronger than absence from VERIFIED: this one was built and caught."""
    assert "18" in configure.refusal("nodejs20", "debian")
    assert configure.refusal("java21", "rhel") == "", "nothing disproved this one"
    # python312 on Oracle Linux was refuted at 3.9.25, the refusal was retired
    # when the recipe changed, and REQ-2026-0146 then proved the new recipe at
    # 3.12.13. The full arc: measured wrong, fixed, re-measured, claimed.
    assert configure.refusal("python312", "rhel") == ""
    assert configure.is_verified("python312", "rhel") is True


def test_the_derived_sets_are_not_mistaken_for_the_record():
    """They are kept for existing readers, and each is more generous than the
    truth — so anything deciding on them must be shown to be safe."""
    assert configure.VERIFIED_CODES == {
        "nginx", "redis7", "java21", "python312", "nodejs20", "apache"}
    assert configure.VERIFIED_FAMILIES == {"rhel", "debian"}
    implied = {(c, f) for c in configure.VERIFIED_CODES
               for f in configure.VERIFIED_FAMILIES}
    # The flat sets imply ten combinations and eight were booted. The gap is
    # everything NOT proven, which is not one thing but two:
    #
    #   nodejs20 / debian   disproven — built, measured at 18.19.1, and declined
    #   python312 / rhel    unproven  — the recipe was fixed after being caught
    #                       at 3.9.25, and no machine has run the new one yet
    #
    # An earlier version of this test asserted the gap was EXACTLY the disproven
    # set. That held only while every unproven pair happened to also be a
    # disproven one, and it stopped being true the moment a recipe was fixed —
    # which is the normal course of events, not an anomaly. The invariant that
    # actually holds is containment.
    unproven = implied - configure.VERIFIED
    # Two pairs. Node 20 on Ubuntu is DISPROVEN — it does not exist in Ubuntu's
    # repositories and an external source was declined. apache on Ubuntu is
    # simply untried: the template has a Debian branch and no machine has run it,
    # which is why the blueprint declares rhel alone.
    assert unproven == {("nodejs20", "debian"), ("apache", "debian")}
    assert set(configure.REFUTED) <= unproven, (
        "a combination is recorded as disproven AND as verified — the two "
        "records disagree about the same machine")

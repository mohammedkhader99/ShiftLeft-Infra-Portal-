"""Every blueprint that boots a machine must make that machine report on itself.

WHY THIS FILE EXISTS. The self-report was built into orchestrator/configure.py,
which renders first-boot configuration for blueprints that ask for it. The Apache
blueprint does not ask: it renders its own cloud-init and ignores what
configure.py produced. So the feature was finished, its own tests passed, and an
Apache machine would still have reported nothing at all. The same was true of
Kafka. Neither was caught by a test — both were caught by hand, in the pre-flight
for REQ-2026-0134, minutes before a VM would have been built blind.

That is the shape of nearly every serious bug in this project: not a missing
test, but a boundary nothing tested ACROSS. configure.py's tests proved
configure.py reports. Nothing proved every machine does.

So this file asks the question at the level it matters: for each blueprint that
boots a machine, does the cloud-init that machine ACTUALLY BOOTS WITH carry a
report? Which mechanism carries it differs per blueprint, and each manifest has
to say which — an exemption must be argued in writing, not assumed by silence.
"""

from __future__ import annotations

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import configure

ROOT = pathlib.Path(__file__).resolve().parents[2]
BLUEPRINTS = ROOT / "orchestrator" / "blueprints"
TERRAFORM = ROOT / "orchestrator" / "terraform"

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/TOKEN/n/axuri6bvn1y8"
       "/b/shiftleft-boot-reports/o/")


def _manifests() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(BLUEPRINTS.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out.append((path.name, doc))
    return out


def _boots_a_machine(manifest: dict) -> bool:
    """A blueprint that declares which OS families it supports boots a machine.

    Buckets and managed databases declare none, because there is no operating
    system for a requester to be given or refused.
    """
    return bool(manifest.get("os_families"))


def _templatefile_call(body: str) -> str:
    """The values a module hands to templatefile(), as text.

    Crude brace matching rather than an HCL parser, because the only question
    asked of it is whether a name appears among those values.
    """
    at = body.find("templatefile(")
    if at < 0:
        return ""
    depth, out = 0, []
    for char in body[at + len("templatefile"):]:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        out.append(char)
    return "".join(out)


MACHINE_BLUEPRINTS = [(n, m) for n, m in _manifests() if _boots_a_machine(m)]
IDS = [n for n, _ in MACHINE_BLUEPRINTS]


def test_there_are_machine_blueprints_to_check():
    """If a rename made the list empty, every test below would pass vacuously and
    this file would go on reporting success while checking nothing."""
    assert len(MACHINE_BLUEPRINTS) >= 4, IDS


# --- Each blueprint must SAY how it reports ----------------------------------

@pytest.mark.parametrize("name,manifest", MACHINE_BLUEPRINTS, ids=IDS)
def test_every_machine_blueprint_declares_its_mechanism(name, manifest):
    """Silence is the failure mode this whole file exists to stop. A new
    blueprint that says nothing must fail here, not on a live machine."""
    assert manifest.get("boot_report") in ("template", "user_data", "none"), (
        f"{name} boots a machine but does not say how that machine reports on "
        f"itself. Add boot_report: template | user_data | none.")


@pytest.mark.parametrize("name,manifest", MACHINE_BLUEPRINTS, ids=IDS)
def test_an_exemption_must_be_argued_in_writing(name, manifest):
    """`none` is allowed — OKE's nodes really do boot an image this project never
    renders — but only where somebody wrote down why. An exemption nobody had to
    justify is how the rule quietly stops applying."""
    if manifest.get("boot_report") != "none":
        return
    text = (BLUEPRINTS / name).read_text(encoding="utf-8")
    reason = re.search(r"#[^\n]*\bnone\b[^\n]*=[^\n]*EXEMPT", text)
    assert reason, (f"{name} exempts itself from boot reporting without saying "
                    f"why. State the reason in a comment above boot_report.")


# --- `template`: the module's own cloud-init must carry the report ------------

TEMPLATE_BLUEPRINTS = [(n, m) for n, m in MACHINE_BLUEPRINTS
                       if m.get("boot_report") == "template"]
TEMPLATE_IDS = [n for n, _ in TEMPLATE_BLUEPRINTS]


@pytest.mark.parametrize("name,manifest", TEMPLATE_BLUEPRINTS, ids=TEMPLATE_IDS)
def test_the_modules_own_cloud_init_writes_and_runs_the_report(name, manifest):
    """THE test. Apache declared os_families and rendered its own cloud-init, and
    that cloud-init had no report in it — which is precisely what this asserts."""
    module = TERRAFORM / manifest["module"]
    templates = list((module / "templates").glob("cloud-init*.tftpl"))
    assert templates, f"{name} says `template` but has no cloud-init template."
    for template in templates:
        text = template.read_text(encoding="utf-8")
        assert "boot_report_url" in text, (
            f"{template.relative_to(ROOT)} never uses boot_report_url, so the "
            f"machine it boots reports nothing.")
        assert "infra-portal-report.sh" in text, (
            f"{template.relative_to(ROOT)} does not write the report script.")


@pytest.mark.parametrize("name,manifest", TEMPLATE_BLUEPRINTS, ids=TEMPLATE_IDS)
def test_the_module_accepts_the_url_and_passes_it_to_the_template(name, manifest):
    """A template that reads boot_report_url and a module that never declares or
    passes it fails at plan time — after approval, which is the expensive place
    to find out."""
    module = TERRAFORM / manifest["module"]
    declared = "".join(p.read_text(encoding="utf-8")
                       for p in module.glob("*.tf"))
    assert re.search(r'variable\s+"boot_report_url"', declared), (
        f"{manifest['module']} does not declare variable boot_report_url.")
    assert re.search(r"boot_report_url\s*=", declared), (
        f"{manifest['module']} declares boot_report_url but never passes it "
        f"into its templatefile call, so the template always sees the default.")


@pytest.mark.parametrize("name,manifest", TEMPLATE_BLUEPRINTS, ids=TEMPLATE_IDS)
def test_a_multi_node_blueprint_gives_each_node_its_own_report(name, manifest):
    """One URL for a three-node Kafka cluster means node 3 overwrites node 1 and
    two machines are never examined — a silent failure wearing a report."""
    module = TERRAFORM / manifest["module"]
    body = "".join(p.read_text(encoding="utf-8") for p in module.glob("*.tf"))
    # The precise question is not "does this module use count anywhere" — Apache
    # uses one for an optional security group and still builds a single machine.
    # It is "is this TEMPLATE rendered once per node", which shows as count.index
    # among the values handed to templatefile.
    call = _templatefile_call(body)
    if "count.index" not in call:
        return
    assert re.search(r"boot_report_url[^\n]*replace\(|replace\([^\n]*boot_report_url",
                     call), (
        f"{manifest['module']} renders its cloud-init once per node but gives "
        f"every node the same report URL, so all but one go unexamined.")


@pytest.mark.parametrize("name,manifest", MACHINE_BLUEPRINTS, ids=IDS)
def test_a_blueprint_cannot_build_more_machines_than_it_can_tell_apart(name, manifest):
    """Every module builds `count` instances from one rendered cloud-init.

    Today each count defaults to 1, so the single report per resource is exactly
    right. Raise one — a one-line change, and the obvious thing to do when a
    requester asks for three web servers — and all three machines PUT to the same
    object. The last to boot wins, two go unexamined, and the portal reports the
    whole stack healthy on the word of one machine.

    This is the same fault the Kafka template already had to solve per node. The
    rule is not "counts must stay at 1"; it is "you may only build as many
    machines as you can tell apart".
    """
    module = TERRAFORM / manifest["module"]
    body = "".join(p.read_text(encoding="utf-8") for p in module.glob("*.tf"))
    # Unbounded on purpose. A {0,400} window here silently skipped service-vm,
    # whose instance block is longer than that, and the test passed while the
    # blueprint was scaled to three machines sharing one report URL — a guard
    # that quietly checks nothing is worse than no guard at all.
    instance = re.search(r'resource\s+"oci_core_instance"[^{]*\{(.*?)\n\}', body, re.S)
    assert instance, (
        f"{manifest['module']} boots machines but no oci_core_instance was found "
        f"— this guard has stopped being able to see them.")
    # A BARE variable is a count someone can raise. An expression — the shared
    # module's `var.resource_kind == "oci-instance" ? 1 : 0` — is a switch, and
    # can never produce a second machine however it is configured.
    counted = re.search(r"count\s*=\s*var\.(\w+)\s*\n", instance.group(1))
    if not counted:
        return
    variable = counted.group(1)

    per_instance = re.search(
        r"boot_report_url[^\n]*(replace|count\.index)|"
        r"(replace|count\.index)[^\n]*boot_report_url", body)
    if per_instance:
        return  # it can tell its machines apart; any count is fine

    declared = re.search(rf'variable\s+"{variable}"\s*\{{(.{{0,400}}?)\n\}}', body, re.S)
    default = re.search(r"default\s*=\s*(\d+)", declared.group(1)) if declared else None
    assert default and int(default.group(1)) == 1, (
        f"{manifest['module']} builds var.{variable} machines from one cloud-init "
        f"and gives them all the same report URL, so all but one go unexamined. "
        f"Either vary the URL per instance, or leave the default at 1.")
    manifest_count = (manifest.get("vars") or {}).get(variable)
    assert manifest_count in (None, 1), (
        f"{name} sets {variable}={manifest_count}, but {manifest['module']} cannot "
        f"tell those machines' reports apart.")


# --- `user_data`: configure.py must be the thing that renders it --------------

USER_DATA_BLUEPRINTS = [(n, m) for n, m in MACHINE_BLUEPRINTS
                        if m.get("boot_report") == "user_data"]
UD_IDS = [n for n, _ in USER_DATA_BLUEPRINTS]


@pytest.mark.parametrize("name,manifest", USER_DATA_BLUEPRINTS, ids=UD_IDS)
def test_a_user_data_blueprint_really_does_take_user_data(name, manifest):
    """Claiming `user_data` while rendering your own cloud-init is exactly the
    Apache mistake, stated the other way round."""
    module = TERRAFORM / manifest["module"]
    body = "".join(p.read_text(encoding="utf-8") for p in module.glob("*.tf"))
    assert re.search(r'variable\s+"user_data"', body), (
        f"{manifest['module']} says its report arrives in user_data but "
        f"declares no user_data variable to receive it.")
    own = list((module / "templates").glob("cloud-init*.tftpl"))
    assert not own, (
        f"{manifest['module']} renders its own cloud-init ({[p.name for p in own]}) "
        f"and would ignore what configure.py produced. It is a `template` "
        f"blueprint, and its template needs the report in it.")


@pytest.mark.parametrize("name,manifest", USER_DATA_BLUEPRINTS, ids=UD_IDS)
def test_configure_py_reports_for_every_family_that_blueprint_offers(
        name, manifest, monkeypatch):
    """Not "configure.py can report" but "it reports for the families THIS
    blueprint offers a requester" — the two came apart once already, when the
    generic path learned about Ubuntu and a template did not."""
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)
    url = configure.boot_report_url("REQ-2026-0199", manifest["resource_kind"])
    assert url, "a configured PAR must produce a URL"
    for family in manifest["os_families"]:
        text = configure.render([{"technology_code": "nginx"}], family, url)
        doc = yaml.safe_load(text)
        assert any(f["path"].endswith("report.sh") for f in doc["write_files"]), (
            f"{name} offers {family} but configure.py writes no report for it.")
        assert "infra-portal-report.sh" in doc["runcmd"][-1], (
            f"{name}/{family} writes a report script and never runs it.")


# --- The declaration has to survive the trip from file to running code --------

@pytest.mark.parametrize("name,manifest", MACHINE_BLUEPRINTS, ids=IDS)
def test_the_registry_carries_the_declaration_through(name, manifest):
    """Every test above reads the YAML file. Nothing at runtime does.

    blueprint_registry.discover() copies a WHITELIST of keys out of each
    manifest, and a key it does not know about is dropped in silence. So
    boot_report was declared correctly in all five files, every test passed, and
    the provisioner saw None for every blueprint — it would have passed the URL
    to nobody and the Apache machine would still have reported nothing.

    Found by running the real code path by hand rather than by any test, which is
    exactly the habit this file was written to make unnecessary.
    """
    from orchestrator import blueprint_registry
    loaded = blueprint_registry.for_resource_kind(manifest["resource_kind"]) or {}
    assert loaded, f"{name} does not load through the registry at all"
    assert loaded.get("boot_report") == manifest["boot_report"], (
        f"{name} declares boot_report: {manifest['boot_report']} in the file, but "
        f"the registry hands the provisioner {loaded.get('boot_report')!r}. Add "
        f"the key to blueprint_registry.discover().")


@pytest.mark.parametrize("name,manifest", TEMPLATE_BLUEPRINTS, ids=TEMPLATE_IDS)
def test_the_provisioner_actually_passes_the_url(name, manifest, monkeypatch):
    """The end of the chain: does the variable reach terraform.tfvars.json?"""
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)
    from orchestrator import provisioner
    kind = manifest["resource_kind"]
    sizing = {"boot_report_url": configure.boot_report_url("REQ-2026-0199", kind)}
    variables = provisioner._cloud_vars("oci", "test-name", {}, kind, sizing)
    assert variables.get("boot_report_url", "").endswith(f"REQ-2026-0199-{kind}.txt"), (
        f"{name} renders its own cloud-init but the provisioner sends it no URL, "
        f"so the template falls back to its default and reports nothing.")


@pytest.mark.parametrize("name,manifest", USER_DATA_BLUEPRINTS, ids=UD_IDS)
def test_a_user_data_blueprint_is_sent_no_stray_variable(name, manifest, monkeypatch):
    """Its module declares no such variable, and Terraform warns on every apply
    for one it was given and never asked for. Routine warnings are where a real
    one goes unread."""
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)
    from orchestrator import provisioner
    kind = manifest["resource_kind"]
    sizing = {"boot_report_url": configure.boot_report_url("REQ-2026-0199", kind)}
    variables = provisioner._cloud_vars("oci", "test-name", {}, kind, sizing)
    assert "boot_report_url" not in variables


# --- The two mechanisms must agree on where reports land ----------------------

def test_both_mechanisms_write_to_the_same_place(monkeypatch):
    """The portal reads one bucket. A blueprint writing somewhere else would look
    like a machine that never reported, which reads as a failure."""
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)
    from orchestrator import boot_reports
    url = configure.boot_report_url("REQ-2026-0199", "oci-apache")
    assert url.startswith(PAR)
    assert boot_reports.bucket() in url


# --- A machine with nothing to install still has something to prove -----------

def test_a_bare_vm_still_files_a_report(monkeypatch):
    """compute-vm and rhel9 install nothing, and render() used to return "" for
    them — so those machines booted with no cloud-init and filed no report,
    making the blueprints unprovable BY CONSTRUCTION. The certification gate then
    refused them for lack of evidence they could never produce.

    A bare VM's job is to exist, so an empty report still proves the three things
    that matter: the image boots, cloud-init ran, and the machine reached Object
    Storage — because the report arrived at all.
    """
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL",
                       "https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
                       "/b/shiftleft-boot-reports/o/")
    from orchestrator import configure
    url = configure.boot_report_url("REQ-2026-0199", "oci-instance")
    text = configure.render([{"technology_code": "compute-vm"}], "rhel", url)
    assert text.strip(), "a bare VM renders no cloud-init at all"
    doc = yaml.safe_load(text)
    assert any(f["path"].endswith("report.sh") for f in doc["write_files"])
    assert any("infra-portal-report.sh" in c for c in doc["runcmd"])


def test_a_bare_vm_with_no_par_still_renders_nothing(monkeypatch):
    """The change must not start emitting cloud-init where none was emitted
    before: with no reporting configured there is nothing to say."""
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("OCI_BOOT_REPORT_PAR_URL", raising=False)
    from orchestrator import configure
    assert configure.render([{"technology_code": "compute-vm"}], "rhel", "") == ""


def test_windows_is_declared_unverifiable_rather_than_linux():
    """oci-compute claimed os_families [rhel, debian] and builds win2019, so the
    gate asked whether a WINDOWS machine had been proven on Oracle Linux and
    answered "unproven on debian,rhel" — a nonsense a reader would have chased.

    Declared unverifiable, NOT as having no families: those are different. No
    families means no machine boots (a bucket) and the gate allows it; this boots
    a real machine that no reporter can run on, and must be refused.
    """
    import yaml as _yaml
    manifest = _yaml.safe_load(
        (ROOT / "orchestrator" / "blueprints" / "oci-compute.yaml").read_text(
            encoding="utf-8"))
    blocked = manifest.get("cannot_verify") or {}
    assert "win2019" in blocked
    assert "Windows" in blocked["win2019"]
    assert "win2019" in (manifest.get("builds") or []), (
        "it is still built — only its verifiability is being declared")


# --- A blueprint must not claim an OS it never configures ---------------------

@pytest.mark.parametrize("name,manifest", _manifests(), ids=[n for n, _ in _manifests()])
def test_a_blueprint_declaring_no_boot_report_declares_no_os_family(name, manifest):
    """`boot_report: none` and `os_families` together are a contradiction.

    A blueprint reports nothing precisely because it renders no cloud-init, and a
    blueprint that renders no cloud-init configures no operating system. OKE
    claimed [rhel] while doing neither, and the certification gate then demanded
    boot evidence it could never produce — "unproven on rhel". Certification
    needed proof, proof needed a build, a build needed certification, and that
    deadlock would have caught EVERY new blueprint after it.

    Such a blueprint is proven by orchestrator/resource_state.py instead: the
    resource itself reaching a working state, checked on every build including
    the first.
    """
    if manifest.get("boot_report") != "none":
        return
    assert not manifest.get("os_families"), (
        f"{name} reports nothing (boot_report: none) yet claims to configure "
        f"{manifest['os_families']}. One of the two is wrong: either it renders "
        f"cloud-init and should report, or it configures no OS and should say so.")


@pytest.mark.parametrize("name,manifest", _manifests(), ids=[n for n, _ in _manifests()])
def test_a_blueprint_that_reports_nothing_can_be_proven_some_other_way(name, manifest):
    """Exempt from boot reporting is not exempt from proof. A blueprint that
    files no report and has no resource-state check could never be proven at
    all, and would sit uncertifiable forever with nobody able to say why."""
    if manifest.get("boot_report") != "none":
        return
    from orchestrator import resource_state
    kind = manifest.get("resource_kind", "")
    assert kind in resource_state.CHECKABLE, (
        f"{name} files no boot report and {kind} has no resource-state check, so "
        f"nothing can ever prove it. Add one to resource_state.CHECKABLE.")

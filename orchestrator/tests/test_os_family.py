"""First-boot configuration across OS families (Ubuntu support).

Offering Ubuntu images made the config layer's single hard-coded package manager
a correctness problem rather than a limitation. `dnf install nginx` on Ubuntu
fails, the `|| echo` swallows it, cloud-init finishes, the marker file is written
and the VM boots healthy with nothing on it — character for character the failure
that took a day to find in GAP-ANALYSIS step 9.

So every test here is about NOT rendering a command for the wrong distribution,
and about refusing rather than guessing when we have no recipe.
"""

import pytest
import yaml

from orchestrator import configure


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.delenv("CONFIG_OS_FAMILY", raising=False)


def _runcmd(codes, family="", versions=None):
    versions = versions or {}
    components = [{"technology_code": c, "version": versions.get(c, "")} for c in codes]
    text = configure.render(components, family)
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict), "cloud-init parses this as YAML"
    assert all(isinstance(x, str) for x in doc.get("runcmd", [])), \
        "a runcmd entry parsed as a mapping — see test_service_vm"
    return doc.get("runcmd", []), text


# --- The package manager matches the machine ---------------------------------

def test_a_debian_machine_is_never_told_to_run_dnf():
    """THE guard this file exists for."""
    runcmd, _ = _runcmd(["nginx"], "debian")
    joined = " ".join(runcmd)
    assert "dnf" not in joined, "an Ubuntu machine was told to use dnf"
    assert "yum" not in joined
    assert "apt-get install" in joined


def test_a_rhel_machine_is_never_told_to_run_apt():
    runcmd, _ = _runcmd(["nginx"], "rhel")
    joined = " ".join(runcmd)
    assert "apt-get" not in joined
    assert "dnf install" in joined


def test_the_package_name_follows_the_family():
    """Apache is httpd on Oracle Linux and apache2 on Ubuntu. Installing the
    wrong name is a failure the `|| echo` hides."""
    rhel, _ = _runcmd(["apache"], "rhel")
    debian, _ = _runcmd(["apache"], "debian")
    assert "httpd" in " ".join(rhel) and "apache2" not in " ".join(rhel)
    assert "apache2" in " ".join(debian) and "httpd" not in " ".join(debian)


def test_the_service_name_follows_the_family_too():
    """Redis's unit is `redis` on Oracle Linux and `redis-server` on Ubuntu.
    Enabling the wrong unit leaves the package installed and not running."""
    rhel, _ = _runcmd(["redis7"], "rhel")
    debian, _ = _runcmd(["redis7"], "debian")
    assert "systemctl enable --now redis " in " ".join(rhel) + " "
    assert "systemctl enable --now redis-server" in " ".join(debian)


# --- The firewall matches the machine ----------------------------------------

def test_the_firewall_tool_follows_the_family():
    """firewall-cmd does not exist on Ubuntu. Getting this wrong leaves a
    correctly installed service behind a closed port — indistinguishable from a
    broken install."""
    rhel, _ = _runcmd(["nginx"], "rhel")
    debian, _ = _runcmd(["nginx"], "debian")
    assert any("firewall-cmd --permanent --add-port=80/tcp" in c for c in rhel)
    assert not any("firewall-cmd" in c for c in debian)
    assert any("ufw allow 80/tcp" in c for c in debian)


def test_no_declared_port_opens_no_firewall_on_either_family():
    for family in ("rhel", "debian"):
        runcmd, _ = _runcmd(["redis7"], family)
        assert not any("ufw allow" in c or "add-port" in c for c in runcmd), family


# --- Version pinning is a Red Hat mechanism ----------------------------------

def test_a_debian_machine_gets_no_module_enable():
    """Module streams are a Red Hat concept. Emitting one on Ubuntu would be a
    command that cannot work."""
    runcmd, _ = _runcmd(["nginx", "redis7"], "debian", {"nginx": "1.24"})
    assert not any("module enable" in c for c in runcmd)


def test_a_rhel_machine_still_pins_the_chosen_version():
    runcmd, _ = _runcmd(["nginx"], "rhel", {"nginx": "1.24"})
    assert any("nginx:1.24" in c for c in runcmd)


# --- Refusing beats guessing --------------------------------------------------

def test_software_with_no_recipe_for_the_family_is_skipped_not_guessed(monkeypatch):
    """A technology we can only install on Red Hat must not be rendered onto an
    Ubuntu machine with Red Hat package names."""
    monkeypatch.setitem(configure.TEMPLATES, "rhelonly",
                        {"ports": [], "rhel": {"packages": ["rhel-only-pkg"],
                                               "services": []}})
    runcmd, text = _runcmd(["nginx", "rhelonly"], "debian")
    assert "rhel-only-pkg" not in " ".join(runcmd)
    assert "nginx" in " ".join(runcmd), "the installable one still installs"
    # ...and the machine says so about itself, rather than being quietly short.
    assert "not_installable_on_debian=rhelonly" in text
    assert any("rhelonly has no install recipe for debian" in c for c in runcmd)


def test_nothing_installable_renders_nothing_at_all(monkeypatch):
    monkeypatch.setitem(configure.TEMPLATES, "rhelonly",
                        {"ports": [], "rhel": {"packages": ["x"], "services": []}})
    assert configure.render([{"technology_code": "rhelonly"}], "debian") == ""


def test_an_unknown_family_falls_back_to_the_configured_default(monkeypatch):
    """Not to a guess. CONFIG_OS_FAMILY is what a request naming no image has
    always used, and that behaviour must not change."""
    monkeypatch.setenv("CONFIG_OS_FAMILY", "debian")
    runcmd, _ = _runcmd(["nginx"], "not-a-family")
    assert "apt-get install" in " ".join(runcmd)


def test_no_family_given_behaves_exactly_as_before(monkeypatch):
    """The regression guard for every request raised before images were offered."""
    monkeypatch.delenv("CONFIG_OS_FAMILY", raising=False)
    runcmd, _ = _runcmd(["nginx"])
    assert "dnf install -y nginx" in " ".join(runcmd)


# --- Mapping an OS name to a family ------------------------------------------

@pytest.mark.parametrize("os_name,expected", [
    ("Oracle Linux", "rhel"),
    ("Canonical Ubuntu", "debian"),
    ("CentOS", "rhel"),
    ("Debian", "debian"),
    ("Oracle Autonomous Linux", "rhel"),
    ("Windows", ""),
    ("", ""),
])
def test_the_family_is_read_from_the_operating_system_oci_reports(os_name, expected):
    assert configure.family_for_os(os_name) == expected


def test_an_unrecognised_os_is_not_assumed_to_be_red_hat():
    """Returning "" rather than "rhel" is the point: assuming Red Hat for an
    unknown OS is how a machine gets told to run dnf on something that has never
    heard of it."""
    assert configure.family_for_os("Windows Server 2019") == ""
    assert configure.family_for_os("TempleOS") == ""


def test_the_marker_records_which_family_was_used():
    """So a machine that installed nothing can be asked what it thought it was."""
    _, text = _runcmd(["nginx"], "debian")
    assert "os_family=debian" in text


def test_every_technology_declares_at_least_one_family():
    """A template with no family block installs nowhere — an entry that looks
    like automation and delivers none."""
    for code in configure.TEMPLATES:
        assert configure.supported_families(code), f"{code} is installable nowhere"


# --- The family reaches the machine ------------------------------------------

def _payload(family):
    component = {"technology_code": "nginx", "size": "medium"}
    if family:
        component["os_family"] = family
    return {"policy_input": {"components": [component]}}


def test_the_family_on_the_component_drives_what_the_machine_is_told():
    """The last hop. Everything above is worthless if _compute_spec renders with
    the orchestrator's default instead of the family the API resolved from the
    requester's chosen image."""
    import orchestrator.main as omain

    rhel = omain._compute_spec(_payload("rhel"), "")["user_data"]
    debian = omain._compute_spec(_payload("debian"), "")["user_data"]
    assert "dnf install" in rhel and "apt-get" not in rhel
    assert "apt-get install" in debian and "dnf" not in debian


def test_a_component_with_no_family_uses_the_configured_default():
    import orchestrator.main as omain
    assert "dnf install" in omain._compute_spec(_payload(""), "")["user_data"]


def test_the_ports_opened_also_follow_the_family(monkeypatch):
    """service_ports drives the NETWORK rules. If it were computed for the wrong
    family, a machine could have a subnet rule for a port nothing listens on."""
    import orchestrator.main as omain
    monkeypatch.setitem(configure.TEMPLATES, "rhelonly",
                        {"ports": [8080], "rhel": {"packages": ["x"], "services": []}})
    payload = {"policy_input": {"components": [
        {"technology_code": "rhelonly", "size": "medium", "os_family": "debian"}]}}
    assert omain._compute_spec(payload, "")["service_ports"] == [], \
        "a port was opened for software that cannot be installed here"

"""The Apache blueprint's own cloud-init, across OS families.

Apache does NOT go through orchestrator/configure.py. Its blueprint renders
templates/cloud-init.yaml.tftpl itself, so every OS-family fix made to the
generic path missed it entirely — and the portal was offering Apache on Ubuntu
images while that template asked apt for a package called `httpd`.

The template is Terraform, not Python, so these tests read it as text and assert
on the structure of its branches. That is weaker than executing it, and the
weakness is the point of the last test here: nothing is claimed as verified until
a machine has run it.
"""

from pathlib import Path

import pytest

TEMPLATE = (Path(__file__).resolve().parents[1]
            / "terraform" / "oci" / "apache-httpd" / "templates"
            / "cloud-init.yaml.tftpl")
MAIN_TF = (Path(__file__).resolve().parents[1]
           / "terraform" / "oci" / "apache-httpd" / "main.tf")
VARIABLES_TF = (Path(__file__).resolve().parents[1]
                / "terraform" / "oci" / "apache-httpd" / "variables.tf")


@pytest.fixture(scope="module")
def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Only the lines that become commands.

    Comments explain WHY the other family's tool is not used here, so they name
    it — `# Debian ships mod_ssl inside the apache2 package` sits in the Debian
    branch. Asserting over prose made two of these tests fail on their own
    explanations, which is a test bug, not a template bug.
    """
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("#"))


def _branch(text: str, family: str) -> str:
    """The commands that run for one family.

    The template branches on `os_family == "debian"`, so the debian arm is
    between that test and the matching `else`, and rhel is the rest.
    """
    start = text.index('%{ if os_family == "debian" ~}', text.index("runcmd:"))
    middle = text.index("%{ else ~}", start)
    end = text.index("%{ endif ~}\n  - echo 'PORTAL: first-boot", middle)
    body = _strip_comments(text[start:middle] if family == "debian"
                           else text[middle:end])
    # Only what actually RUNS. Everything after `||` is a diagnostic message, and
    # a message may legitimately name the other family's vocabulary while
    # explaining itself. Judging prose as though it were a command is how these
    # tests first failed on their own comments.
    return "\n".join(line.split(" || ")[0] for line in body.splitlines())


# --- The module accepts and uses the family ----------------------------------

def test_the_module_takes_an_os_family_input():
    assert 'variable "os_family"' in VARIABLES_TF.read_text(encoding="utf-8")
    assert "os_family = var.os_family" in MAIN_TF.read_text(encoding="utf-8"), \
        "the variable must reach the template, or it decides nothing"


def test_an_unknown_family_is_refused_rather_than_defaulted():
    """Falling through to the Red Hat path would ask apt for `httpd` and leave a
    VM with no web server on it — the exact silent success this fixes."""
    variables = VARIABLES_TF.read_text(encoding="utf-8")
    assert "validation {" in variables
    assert 'contains(["rhel", "debian"], var.os_family)' in variables


# --- Neither family is given the other's commands ----------------------------

def test_the_debian_branch_never_mentions_red_hat_names(template):
    debian = _branch(template, "debian")
    for wrong in ("httpd", "firewall-cmd", "/etc/pki/", "mod_ssl"):
        assert wrong not in debian, f"the Ubuntu path would run {wrong!r}"


def test_the_rhel_branch_never_mentions_debian_names(template):
    rhel = _branch(template, "rhel")
    for wrong in ("apache2", "ufw ", "a2enmod", "a2ensite", "/etc/ssl/"):
        assert wrong not in rhel, f"the Oracle Linux path would run {wrong!r}"


def test_each_family_installs_its_own_package_name(template):
    assert "- apache2" in template and "- httpd" in template
    # ...and the package list is branched, not both installed everywhere.
    packages = template[template.index("packages:"):template.index("write_files:")]
    assert '%{ if os_family == "debian" ~}' in packages


@pytest.mark.parametrize("family,unit", [("debian", "apache2"), ("rhel", "httpd")])
def test_the_service_enabled_is_the_one_the_family_ships(template, family, unit):
    assert f"systemctl enable --now {unit}" in _branch(template, family)


@pytest.mark.parametrize("family,tool", [("debian", "ufw allow"),
                                         ("rhel", "firewall-cmd")])
def test_the_firewall_tool_matches_the_family(template, family, tool):
    """firewall-cmd does not exist on Ubuntu. Getting this wrong leaves a working
    web server behind a closed port, which looks exactly like a broken install."""
    assert tool in _branch(template, family)


@pytest.mark.parametrize("family,path", [("debian", "/etc/apache2/apache2.conf"),
                                         ("rhel", "/etc/httpd/conf/httpd.conf")])
def test_the_config_file_written_to_is_the_one_that_exists(template, family, path):
    assert path in template


def test_debian_enables_mod_ssl_which_red_hat_gets_from_a_package(template):
    """Ubuntu ships mod_ssl inside apache2 but leaves it disabled; Red Hat's
    mod_ssl package enables itself. Same outcome, different mechanism."""
    assert "a2enmod ssl" in _branch(template, "debian")
    # Red Hat gets it from a PACKAGE, so it appears in the package list rather
    # than in any command.
    packages = template[template.index("packages:"):template.index("write_files:")]
    assert "- mod_ssl" in packages


# --- Failures are diagnosable from the machine -------------------------------

def test_every_step_reports_its_own_failure(template):
    """`|| true` hides a failure; `|| echo PORTAL: ...` records it. A machine
    that installed nothing must say so on itself."""
    steps = [line for line in _strip_comments(template).splitlines()
             if line.strip().startswith("- systemctl")
             or line.strip().startswith("- ufw allow")
             or "add-service" in line]
    assert steps
    for step in steps:
        assert "PORTAL:" in step, f"this step fails silently: {step.strip()}"


def test_the_marker_records_which_family_was_used(template):
    assert "os_family=${os_family}" in template


# --- Nothing is claimed as proven --------------------------------------------

def test_apache_on_ubuntu_is_not_claimed_as_certified():
    """The template is asserted on as TEXT here, which proves it says the right
    things — not that Ubuntu's apache2 actually comes up. Until a VM has run it
    and answered on port 80, the manifest must not offer Apache on Ubuntu.

    Flip this only alongside a real boot test, the same bar nginx and Redis met.
    """
    import yaml
    manifest = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "blueprints"
         / "oci-apache-httpd.yaml").read_text(encoding="utf-8"))
    assert manifest["os_families"] == ["rhel"], (
        "Apache on Ubuntu is written but not booted — certify it with evidence, "
        "not with a passing template test")

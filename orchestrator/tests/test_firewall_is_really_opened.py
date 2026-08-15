"""A service nobody can reach is not a working service.

WHY THIS FILE EXISTS. REQ-2026-0136 installed nginx on Ubuntu, started it, and
left it listening on 0.0.0.0:80. Every check the portal made agreed it was
healthy, and the portal recorded it provisioned. Another machine on the same
subnet got `No route to host`.

Two independent faults, and both had to be fixed:

1. THE PORT WAS NEVER OPENED. The first-boot script ran `ufw allow 80/tcp` on the
   belief that "ufw ships inactive on Ubuntu cloud images, and `ufw allow` records
   the rule either way". OCI's Ubuntu images do not ship ufw at all. The command
   failed, and the image's baked-in iptables REJECT stayed exactly where it was.

2. NOTHING NOTICED. The self-report measured the port with `curl localhost` and
   `ss`, which both describe the DAEMON — they pass perfectly on a machine no
   client can reach. And the machine had actually logged the failure, as
   `PORTAL: could not open 80/tcp`, but the verdict only counted a line as a
   failure if it contained the word FAILED.

So the tests below hold three things: the port is opened by a mechanism the image
really has, the report asks the firewall rather than the daemon, and a step that
did not work is always recognisable as one.
"""

from __future__ import annotations

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import boot_reports, configure

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = sorted((ROOT / "orchestrator" / "terraform" / "oci").glob(
    "*/templates/cloud-init*.tftpl"))

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)


def _rendered(family="debian"):
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    text = configure.render([{"technology_code": "nginx"}], family, url)
    return text, yaml.safe_load(text)


def _report_script(doc):
    return next(f["content"] for f in doc["write_files"]
                if f["path"].endswith("report.sh"))


# --- 1. The port is opened by something the image actually has ----------------

def test_ubuntu_does_not_rely_on_ufw_alone():
    """THE bug. ufw is not installed on OCI's Ubuntu images, so a step that only
    calls ufw silently does nothing and the reject rule survives."""
    _text, doc = _rendered("debian")
    opener = [c for c in doc["runcmd"] if "80" in c and ("ufw" in c or "iptables" in c)]
    assert opener, "nothing opens port 80 at all"
    assert any("iptables" in c for c in opener), (
        "Ubuntu opens the port only with ufw, which these images do not ship")


def test_ufw_is_still_preferred_when_it_is_genuinely_running():
    """Writing raw iptables rules underneath an active ufw gets them wiped on its
    next reload — so the fix must not simply stop using ufw."""
    _text, doc = _rendered("debian")
    opener = next(c for c in doc["runcmd"] if "--dport 80" in c)
    assert "ufw" in opener and "iptables" in opener
    assert opener.index("ufw") < opener.index("iptables"), "ufw must be tried first"


def test_the_rule_survives_a_reboot():
    """An iptables rule added at runtime is gone on the next boot, which would
    turn a working machine into a broken one with nobody touching it."""
    _text, doc = _rendered("debian")
    assert any("netfilter-persistent" in c or "iptables-save" in c
               for c in doc["runcmd"]), "the rule is never written down"


def test_red_hat_still_uses_firewalld():
    """firewalld IS present there, and this must not be 'fixed' into iptables."""
    _text, doc = _rendered("rhel")
    assert any("firewall-cmd" in c and "80" in c for c in doc["runcmd"])
    assert not any("iptables" in c for c in doc["runcmd"])


# --- 2. The report asks the firewall, not the daemon --------------------------

@pytest.mark.parametrize("family", ["rhel", "debian"])
def test_the_report_asks_the_firewall_itself(family):
    """curl-to-localhost and ss both pass on a machine nobody can reach."""
    _text, doc = _rendered(family)
    script = _report_script(doc)
    assert "firewall_80=" in script, (
        "the report never asks whether the port is actually open")


@pytest.mark.parametrize("family", ["rhel", "debian"])
def test_the_firewall_check_is_asked_the_way_the_port_was_opened(family):
    """A check that asks differently from how the change was made produces false
    alarms, which erode trust in the report as surely as a missed failure.

    Caught in review: this blueprint's Red Hat branch opens the firewall with
    --add-service=http, and a --query-port=80/tcp check would have reported
    CLOSED on every correctly built Oracle Linux machine.
    """
    _text, doc = _rendered(family)
    opener = next(c for c in doc["runcmd"] if "80" in c and "PORTAL FAILURE" in c)
    script = _report_script(doc)
    check = next(line for line in script.splitlines() if "firewall_80=" in line)
    if "firewall-cmd" in opener:
        assert "firewall-cmd" in check
        assert ("--add-port" in opener) == ("--query-port" in check), (
            "opened by port but not checked by port, or the reverse")
    else:
        assert "iptables" in check or "ufw" in check


def test_a_closed_port_is_reported_as_closed_not_absent():
    """Saying nothing when the check fails would read as 'no problem found'."""
    _text, doc = _rendered("debian")
    check = next(line for line in _report_script(doc).splitlines()
                 if "firewall_80=" in line)
    assert "CLOSED" in check, "there is no negative branch — silence means healthy"


def test_a_technology_with_no_ports_gets_no_firewall_check():
    """redis7 deliberately opens nothing. Asking about a port it never wanted
    would report a failure on a correctly built machine."""
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    doc = yaml.safe_load(configure.render([{"technology_code": "redis7"}], "rhel", url))
    assert "firewall_" not in _report_script(doc)


# --- 3. A step that did not work is always recognisable -----------------------

def test_the_verdict_catches_a_closed_firewall():
    """The whole point: this exact report was called healthy."""
    report = ("os_family=debian\ntechnologies=nginx\n"
              "nginx 1.24.0-2ubuntu7.15\nnginx=active\n"
              "http_80=200\nLISTEN 0 511 0.0.0.0:80\nfirewall_80=CLOSED\n")
    result = boot_reports.verdict(report)
    assert result["ok"] is False
    assert any("nothing outside this machine can reach" in p
               for p in result["problems"])


def test_an_open_firewall_passes():
    report = ("nginx 1.24.0\nnginx=active\nhttp_80=200\n"
              "LISTEN 0 511 0.0.0.0:80\nfirewall_80=open\n")
    assert boot_reports.verdict(report)["ok"] is True


def test_the_verdict_catches_a_failed_step_whatever_its_wording():
    """`PORTAL: could not open 80/tcp` was a real failure on a real machine and
    was read as healthy, because the verdict was looking for the word FAILED."""
    report = "nginx 1.24.0\nnginx=active\nPORTAL FAILURE: could not open 80/tcp in the firewall\n"
    result = boot_reports.verdict(report)
    assert result["ok"] is False


@pytest.mark.parametrize("line", [
    "PORTAL: package install FAILED",
    # The real one. This exact line sat in REQ-2026-0136's report while the
    # verdict called the machine healthy, and it STILL did after the marker was
    # introduced, because the compatibility branch only matched FAILED. Found by
    # re-reading the live report rather than by any test.
    "PORTAL: could not open 80/tcp",
    "PORTAL: nginx failed to start",
    "PORTAL: redis7 has no install recipe for debian; nothing was installed for it",
])
def test_reports_from_older_machines_are_still_understood(line):
    """Machines built before the marker existed are already running and cannot be
    asked again, so their wording has to keep working."""
    assert boot_reports.verdict(f"nginx=active\n{line}\n")["ok"] is False


@pytest.mark.parametrize("line", [
    "PORTAL: first-boot configuration finished",
    "PORTAL: kafka install starting 2026-08-15T06:00:00+00:00",
    "PORTAL: storage already formatted, leaving it alone",
])
def test_an_older_machines_progress_lines_are_not_read_as_failures(line):
    """Matching prose cuts both ways: too loose and every healthy machine fails."""
    report = ("nginx 1.24.0\nnginx=active\nhttp_80=200\nLISTEN 0 511 0.0.0.0:80\n"
              f"firewall_80=open\n{line}\n")
    assert boot_reports.verdict(report)["ok"] is True, line


def test_an_informational_line_is_not_a_failure():
    """'first-boot configuration finished' is the SUCCESS line."""
    report = ("nginx 1.24.0\nnginx=active\nhttp_80=200\nLISTEN 0 511 0.0.0.0:80\n"
              "firewall_80=open\nPORTAL: first-boot configuration finished\n")
    assert boot_reports.verdict(report)["ok"] is True


# --- Every blueprint, not just the generated one ------------------------------

@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.parent.parent.name)
def test_no_blueprint_opens_a_port_with_ufw_alone(path):
    """The Apache blueprint had the identical ufw bug in its own template. Fixing
    configure.py and leaving a template behind is the single most repeated
    mistake in this project."""
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        # Comments discuss ufw at length precisely BECAUSE of this bug; it is the
        # commands that have to be right.
        if line.strip().startswith("#") or "ufw allow" not in line:
            continue
        assert "iptables" in line, (
            f"{path.parent.parent.name} opens a port with ufw alone: {line.strip()}")


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.parent.parent.name)
def test_every_blueprint_marks_its_failures_the_same_way(path):
    """One marker, so the verdict never has to match prose."""
    text = path.read_text(encoding="utf-8")
    suspicious = re.findall(r"PORTAL: [^'\"]*(?:could not|failed|FAILED)[^'\"]*", text)
    assert not suspicious, (
        f"{path.parent.parent.name} reports failures the verdict may not "
        f"recognise: {suspicious}")

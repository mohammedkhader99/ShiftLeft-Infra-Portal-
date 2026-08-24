"""Machines reporting on themselves, and the portal reading the answer.

The portal inferred success from Terraform exiting zero. That inference was wrong
four times in one week — a service-vm that installed nothing, an Apache whose
runcmd was discarded, a Redis 6 sold as a 7, an nginx that lost port 80 to httpd
— and every one booted healthy and was reported provisioned.

Run Command answers the question directly and does not exist on OCI's Ubuntu
images (measured: 16 agent plugins on Oracle Linux, 11 on Ubuntu, absent from the
latter), and a private VM cannot be reached inbound. So the machine reports
outward through a write-only pre-authenticated request, which works on any OS.
"""

import os

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import boot_reports, configure

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/TOKEN/n/axuri6bvn1y8"
       "/b/shiftleft-boot-reports/o/")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)


def _rendered(codes=("nginx",), family="debian", reference="REQ-2026-0140",
              kind="oci-service-vm"):
    url = configure.boot_report_url(reference, kind)
    text = configure.render([{"technology_code": c} for c in codes], family, url)
    return text, yaml.safe_load(text)


# --- The machine is told to report -------------------------------------------

def test_the_report_url_is_per_resource_not_per_request():
    """A stack builds several machines and each reports separately, or the second
    would overwrite the first and one machine would go unexamined."""
    a = configure.boot_report_url("REQ-2026-0140", "oci-service-vm")
    b = configure.boot_report_url("REQ-2026-0140", "oci-apache")
    assert a != b
    assert a.endswith("REQ-2026-0140-oci-service-vm.txt")


def test_no_par_configured_means_no_report_and_no_change():
    """Absent configuration must leave the boot exactly as it was."""
    os.environ.pop("OCI_BOOT_REPORT_PAR_URL", None)
    assert configure.boot_report_url("REQ-2026-0140", "oci-service-vm") == ""
    _text, doc = _rendered()
    assert not any(f["path"].endswith("report.sh") for f in doc["write_files"])
    assert not any("infra-portal-report.sh" in c for c in doc["runcmd"])


@pytest.mark.parametrize("family", ["rhel", "debian"])
def test_every_family_reports(family):
    """The whole point: Ubuntu cannot be interrogated any other way, so a family
    that quietly skipped this would be unverifiable forever."""
    _text, doc = _rendered(family=family)
    assert any(f["path"].endswith("report.sh") for f in doc["write_files"])
    assert doc["runcmd"][-1].startswith("/usr/local/bin/infra-portal-report.sh")


def test_the_report_runs_last():
    """Earlier, and it would describe a machine still installing."""
    _text, doc = _rendered()
    assert "infra-portal-report.sh" in doc["runcmd"][-1]
    assert not any("infra-portal-report.sh" in c for c in doc["runcmd"][:-1])


def test_the_rendered_document_is_still_valid_cloud_config():
    """A script full of quotes, braces and pipes now travels inside YAML. If it
    broke the document, it would take the whole runcmd block with it — which is
    the exact failure this codebase has had twice."""
    text, doc = _rendered()
    assert text.startswith("#cloud-config")
    assert isinstance(doc, dict)
    assert all(isinstance(c, str) for c in doc["runcmd"])


def test_the_script_checks_packages_services_ports_and_the_log():
    _text, doc = _rendered()
    script = next(f["content"] for f in doc["write_files"]
                  if f["path"].endswith("report.sh"))
    assert "nginx NOT INSTALLED" in script, "a missing package must say so"
    assert "systemctl is-active nginx" in script
    assert "http_80=" in script
    assert "/var/log/infra-portal.log" in script


def test_a_failed_upload_never_fails_the_boot():
    """A working machine that could not reach the bucket is a better outcome than
    one marked broken because object storage blinked."""
    _text, doc = _rendered()
    assert doc["runcmd"][-1].endswith("|| true")


# --- Reading the answer -------------------------------------------------------

def test_the_bucket_is_derived_from_the_par():
    """Configured twice, the two could point at different buckets and the portal
    would read an empty one while machines wrote to another."""
    assert boot_reports.bucket() == "shiftleft-boot-reports"


def test_no_par_means_no_bucket():
    os.environ.pop("OCI_BOOT_REPORT_PAR_URL", None)
    assert boot_reports.bucket() == ""


def test_a_missing_report_is_an_error_not_an_empty_answer():
    """"No report" and "a report saying it is broken" mean opposite things.
    Conflating them is how a silent failure reads as a healthy one."""
    os.environ.pop("OCI_BOOT_REPORT_PAR_URL", None)
    with pytest.raises(boot_reports.BootReportUnavailable):
        boot_reports.fetch("REQ-2026-0140", "oci-service-vm")


# --- The verdict --------------------------------------------------------------

HEALTHY = """os_family=debian
technologies=nginx
os=Ubuntu 24.04.1 LTS
--- packages ---
nginx 1.24.0-2ubuntu7
--- services ---
nginx=active
--- ports ---
http_80=200
LISTEN 0 511 0.0.0.0:80 0.0.0.0:*
--- first-boot log ---
PORTAL: first-boot configuration finished
"""


def test_a_healthy_machine_reads_as_ok():
    assert boot_reports.verdict(HEALTHY)["ok"] is True


def test_a_package_that_did_not_install_is_a_failure():
    bad = HEALTHY.replace("nginx 1.24.0-2ubuntu7", "nginx NOT INSTALLED")
    v = boot_reports.verdict(bad)
    assert v["ok"] is False and "NOT INSTALLED" in v["problems"][0]


def test_installed_but_not_running_is_a_failure_not_a_warning():
    """THE shape of every silent success here: the package is present, so a
    looser check would call it fine. REQ-2026-0130's Apache looked exactly like
    this."""
    bad = HEALTHY.replace("nginx=active", "nginx=inactive")
    v = boot_reports.verdict(bad)
    assert v["ok"] is False
    assert any("nginx is inactive" in p for p in v["problems"])


def test_a_service_answering_nothing_on_its_port_is_NOT_condemned_for_it():
    """CHANGED by REQ-2026-0195, and the change is a narrowing of what one check
    is allowed to claim.

    `000` means curl had no HTTP conversation. For nginx that would be alarming;
    for AMQP on 5672, epmd on 4369 or clustering on 25672 it is simply what
    happens when you speak HTTP at something that does not. A RabbitMQ with all
    four ports bound on the host and open in the firewall was failed for it.

    PRESENCE IS STILL CHECKED, by the line that is actually authoritative for
    it — see the test below. What `http_` can still condemn is a server that
    answered and said it could not serve.
    """
    bad = HEALTHY.replace("http_80=200", "http_80=000")
    assert boot_reports.verdict(bad)["ok"] is True


def test_a_port_with_nothing_listening_is_still_a_failure():
    """The check that is authoritative for presence. Relaxing `http_` must not
    quietly relax this: a service that never started has to be caught."""
    bad = HEALTHY.replace("http_80=200", "http_80=000") + "nothing listening on 80\n"
    assert boot_reports.verdict(bad)["ok"] is False


def test_a_server_that_answered_5xx_is_still_a_failure():
    """Running and unable to serve — which no socket check would notice."""
    for code in ("500", "502", "503"):
        bad = HEALTHY.replace("http_80=200", f"http_80={code}")
        assert boot_reports.verdict(bad)["ok"] is False, code


def test_a_redirect_or_forbidden_still_counts_as_answering():
    """403 is what an Apache with no index file returns — the server is up, which
    is what the port check is asking about."""
    for code in ("301", "302", "403"):
        assert boot_reports.verdict(HEALTHY.replace("http_80=200", f"http_80={code}"))["ok"]


def test_nothing_listening_is_a_failure():
    bad = HEALTHY.replace("LISTEN 0 511 0.0.0.0:80 0.0.0.0:*",
                          "nothing listening on 80")
    assert boot_reports.verdict(bad)["ok"] is False


def test_a_portal_failure_line_in_the_log_is_a_failure():
    bad = HEALTHY.replace("PORTAL: first-boot configuration finished",
                          "PORTAL: package install FAILED")
    assert boot_reports.verdict(bad)["ok"] is False


def test_the_verdict_lists_every_problem_not_just_the_first():
    bad = (HEALTHY.replace("nginx=active", "nginx=inactive")
                  .replace("http_80=200", "http_80=500"))
    assert len(boot_reports.verdict(bad)["problems"]) >= 2

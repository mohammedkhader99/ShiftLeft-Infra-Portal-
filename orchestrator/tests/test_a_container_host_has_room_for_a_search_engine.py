"""REQ-2026-0237: Elasticsearch started, checked itself, and shut down.

The machine's own log, captured because a declared port never bound:

    "using discovery type [multi-node] and seed hosts providers"
    "bound or publishing to a non-loopback address, enforcing bootstrap checks"
    ERROR "node validation exception ... bootstrap check failure [1] of [1]:
           max virtual memory areas vm.max_map_count [65530] is too low"
    ERROR: Elasticsearch did not exit normally
    "stopping ..."

TWO NIGHTS OF MY GUESSES WERE WRONG. I read `listening_inside=none` and said it
was slow; then I said the shape was too small. It was neither: Elasticsearch
started in two seconds, bound its transport port, ran its production bootstrap
checks, failed one, and stopped on purpose. The log capture settled what two
inferences from a port number could not.

TWO CAUSES, AND THIS FILE IS THE HOST ONE.

`vm.max_map_count` is a HOST sysctl — Oracle Linux ships 65530 and both
Elasticsearch and OpenSearch require 262144. No container setting can supply it.

APPLIED TO EVERY CONTAINER HOST, not to a list of technologies. It is the
vendors' own documented figure, inert for a container that does not use mmap
heavily, and the alternative is a table of which images need it — the shape this
project has spent a fortnight removing.

The other cause — `discovery.type=single-node` — is an operator decision and
belongs in secrets/container.env, which needed ENV_KEY widened to carry a dotted
name.

Nothing here reaches a machine.
"""

from __future__ import annotations

import json

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import configure

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")

ES = {
    "code": "elasticsearch", "builds_on": "oci/service-vm", "ports": [9200],
    "container": {
        "image": "docker.io/library/elasticsearch", "tag": "9.5.2",
        "digest": "sha256:" + "d" * 64,
        "data_dir": "/var/lib/elasticsearch",
        "data_mount": "/usr/share/elasticsearch/data",
    },
    "rhel": {"packages": [], "services": ["elasticsearch"]},
}

PLAIN = {
    "code": "nginx", "builds_on": "oci/service-vm", "ports": [80],
    "rhel": {"packages": ["nginx"], "services": ["nginx"]},
}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)


def render(tmp_path, monkeypatch, profile, code):
    store = tmp_path / "profiles"
    store.mkdir(exist_ok=True)
    (store / f"{code}.json").write_text(json.dumps(profile))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", store)
    url = configure.boot_report_url("REQ-2026-0237", "oci-service-vm")
    return configure.render([{"technology_code": code}], "rhel", url)


def test_a_container_host_raises_max_map_count(tmp_path, monkeypatch):
    """THE fix. Without it Elasticsearch refuses to run as a non-loopback node."""
    out = render(tmp_path, monkeypatch, ES, "elasticsearch")

    assert "vm.max_map_count=262144" in out
    assert "sysctl -w vm.max_map_count=262144" in out


def test_it_survives_a_reboot(tmp_path, monkeypatch):
    """The Quadlet [Install] section exists so the container comes back after a
    reboot. A sysctl that does not would let it come back and fail."""
    out = render(tmp_path, monkeypatch, ES, "elasticsearch")

    assert "/etc/sysctl.d/99-portal-containers.conf" in out


def test_it_is_set_before_anything_is_pulled(tmp_path, monkeypatch):
    """Order matters: the container starts moments after the pull, and a sysctl
    applied afterwards is applied too late for the first start."""
    out = render(tmp_path, monkeypatch, ES, "elasticsearch")

    assert out.index("vm.max_map_count") < out.index("podman pull"), (
        "the sysctl is applied after the image is pulled")


def test_an_ordinary_vm_is_untouched(tmp_path, monkeypatch):
    """THE REGRESSION GUARD. nginx, redis, java, python, node, dotnet, keycloak
    and vault are all certified against this renderer. A change to their first
    boot would invalidate evidence bought with real machines."""
    out = render(tmp_path, monkeypatch, PLAIN, "nginx")

    assert "vm.max_map_count" not in out
    assert "sysctl" not in out


def test_the_rendered_user_data_is_still_valid_yaml(tmp_path, monkeypatch):
    doc = yaml.safe_load(render(tmp_path, monkeypatch, ES, "elasticsearch"))

    assert isinstance(doc, dict) and doc.get("runcmd")


def test_it_does_not_abort_first_boot_if_the_sysctl_fails(tmp_path, monkeypatch):
    """A hardened image may refuse it. Everything else on the machine must still
    configure — a container that cannot start says so in its own log, which is
    more use than a machine that never finishes booting."""
    out = render(tmp_path, monkeypatch, ES, "elasticsearch")

    for line in out.splitlines():
        if "vm.max_map_count" in line:
            assert "|| true" in line, line

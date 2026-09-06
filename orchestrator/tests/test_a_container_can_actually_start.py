"""Three ways the renderer built a container that could never run (2026-09-06).

grafana, minio and gitea were each refused by a real proof on the same night,
and not one of them was broken software. All three were the portal assembling
the container wrongly, and each failure is a CLASS rather than a quirk of the
technology that happened to find it.

  * GRAFANA could not write its own data directory. `data_dir` is a HOST path
    created by cloud-init as root, and podman bind-mounts it verbatim -- it does
    not copy the image's ownership up (that is named volumes) and it does not
    chown. Grafana runs as uid 472, so it said

        GF_PATHS_DATA='/var/lib/grafana' is not writable.
        mkdir: can't create directory '/var/lib/grafana/plugins': Permission denied

    and died until systemd's start limit stopped it. Every image that drops
    privileges is the same case: elasticsearch, postgres, and most official
    images written since. `:U` is podman's answer and costs a root container
    nothing.

  * GITEA was handed the machine's own SSH port. It declares 22 and 3000, and
    the renderer published every declared port onto the host 1:1:

        Error: cannot listen on the TCP port: listen tcp4 :22: bind: address
        already in use

    The bind FAILING was the lucky outcome. Had sshd not held the port, podman
    would have taken it and cut the portal off from the machine it was building.

  * MINIO was never told what to do. Its CMD is the bare binary and the binary
    needs a subcommand, so the container started, printed its own help screen,
    and exited -- five times. There was no way to express a command at all.

WHAT MAKES THESE TESTS WORTH KEEPING is that all three were invisible for a
different reason than they were caused: the message every one of them produced
was "the verification result could not be read", because the reply carrying the
machine's own words was being truncated in transit. Two defects, and the second
hid the first three.
"""

from __future__ import annotations

import json

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import configure

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)


def _profile(code, *, ports, container_extra=None):
    return {
        "code": code, "builds_on": "oci/service-vm", "ports": list(ports),
        "container": {
            "image": f"docker.io/library/{code}", "tag": "latest",
            "digest": "sha256:" + "d" * 64,
            "data_dir": f"/var/lib/{code}", "data_mount": f"/var/lib/{code}",
            **(container_extra or {}),
        },
        "rhel": {"packages": [], "services": [code]},
    }


def render(tmp_path, monkeypatch, profile):
    code = profile["code"]
    (tmp_path / f"{code}.json").write_text(json.dumps(profile))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    return configure.render([{"technology_code": code}], "rhel", url)


def unit(tmp_path, monkeypatch, profile):
    doc = yaml.safe_load(render(tmp_path, monkeypatch, profile))
    return next(f["content"] for f in doc["write_files"]
                if f["path"].endswith(".container"))


# --- grafana: it must be able to write its own data ---------------------------

def test_the_data_volume_is_chowned_to_whoever_the_image_runs_as(
        tmp_path, monkeypatch):
    """THE DEFECT. `:Z` relabels for SELinux and says nothing about ownership,
    so an image that drops privileges gets a root-owned directory it cannot
    write. Grafana's first proof passed -- it mounted nothing -- and the
    narrowed recipe that added the volume is the one that broke."""
    text = unit(tmp_path, monkeypatch, _profile("grafana", ports=[3000]))

    assert "/var/lib/grafana:/var/lib/grafana:Z,U" in text


def test_the_selinux_relabel_is_not_lost_in_the_process(tmp_path, monkeypatch):
    """`:U` is added TO `:Z`, never instead of it. SELinux is enforcing on
    Oracle Linux, and dropping the relabel would swap one silent denial for
    another."""
    text = unit(tmp_path, monkeypatch, _profile("grafana", ports=[3000]))

    volume = next(l for l in text.splitlines() if l.strip().startswith("Volume="))
    assert volume.strip().endswith(":Z,U")


# --- gitea: the machine's own ports are not the container's to take -----------

def test_a_container_is_never_published_onto_the_machines_ssh_port(
        tmp_path, monkeypatch):
    """THE DEFECT. `PublishPort=22:22` either loses the race with sshd, as it
    did, or wins it and strands the machine."""
    text = unit(tmp_path, monkeypatch, _profile("gitea", ports=[22, 3000]))

    assert "PublishPort=22:22" not in text
    assert "PublishPort=20022:22" in text


def test_the_container_still_serves_the_port_it_expects_to(tmp_path, monkeypatch):
    """Only the HOST half moves. The software inside binds 22 exactly as its
    publisher intended, which is also what the health check reads -- it looks at
    /proc/net/tcp inside the container, so a remap cannot make a working service
    look broken."""
    text = unit(tmp_path, monkeypatch, _profile("gitea", ports=[22, 3000]))

    published = [l.split("=", 1)[1].strip()
                 for l in text.splitlines() if "PublishPort=" in l]
    assert sorted(published) == ["20022:22", "3000:3000"]


def test_an_ordinary_port_is_left_exactly_where_it_was(tmp_path, monkeypatch):
    """The rule is narrow on purpose. 80, 443 and 3000 are ports a requester
    expects to find a service on, and moving them would be a surprise with no
    cause. (A code with no shipped package table, so it renders as a container.)"""
    text = unit(tmp_path, monkeypatch, _profile("httpbin", ports=[80, 443]))

    assert "PublishPort=80:80" in text
    assert "PublishPort=443:443" in text


def test_the_firewall_opens_the_port_the_service_is_really_published_on(
        tmp_path, monkeypatch):
    """The firewall and the unit file are built from one declaration and must
    not disagree about it. Opening 22 while publishing 20022 would firewall off
    the only port anything listens on, and open one nothing serves."""
    body = render(tmp_path, monkeypatch, _profile("gitea", ports=[22, 3000]))

    assert "--add-port=20022/tcp" in body
    assert "--add-port=22/tcp" not in body


# --- minio: an image that must be told what to do -----------------------------

def test_a_command_reaches_the_unit_when_the_recipe_carries_one(
        tmp_path, monkeypatch):
    """THE DEFECT. There was no way to express this at all, so minio could not
    be offered however many machines were spent on it."""
    profile = _profile("minio", ports=[9000],
                       container_extra={"command": "server /var/lib/minio"})

    assert "Exec=server /var/lib/minio" in unit(tmp_path, monkeypatch, profile)


def test_a_container_that_needs_no_command_is_given_none(tmp_path, monkeypatch):
    """Most images run their own CMD perfectly well, and an empty `Exec=` would
    override it with nothing."""
    text = unit(tmp_path, monkeypatch, _profile("valkey", ports=[6379]))

    assert "Exec=" not in text


def test_the_whole_document_is_still_valid_cloud_config(tmp_path, monkeypatch):
    """The one that catches a rendering change breaking every machine at once:
    all three fixes write into a YAML block, and a stray newline anywhere in
    them produces a document cloud-init silently declines to run."""
    profile = _profile("minio", ports=[9000, 22],
                       container_extra={"command": "server /var/lib/minio"})

    doc = yaml.safe_load(render(tmp_path, monkeypatch, profile))

    assert isinstance(doc, dict) and doc.get("write_files")

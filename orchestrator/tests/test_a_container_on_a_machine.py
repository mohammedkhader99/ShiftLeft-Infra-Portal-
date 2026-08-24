"""Software delivered as an image, run on the VM the requester asked for (C8).

REQ-2026-0188 to 0190 established, over three real machines, that RabbitMQ is
not installable from Oracle Linux 9's repositories or from EPEL 9. Repology
confirms it independently: `rabbitmq-server` is packaged for EPEL 6 and EPEL 7
and for every recent Fedora, and for no EPEL 9 at all. The vendor's own answer,
in their documentation, is to add their repository — and the world's answer,
3.86 billion pulls deep, is to run their image.

WHAT THE REQUESTER GETS IS STILL A LINUX VM. They asked for RabbitMQ on Linux
and they receive an Oracle Linux machine they can log in to, with RabbitMQ
running on it. The container is how the software arrived, not a change to what
was delivered — which is exactly why this rung sits on `oci/service-vm` and not
on a Kubernetes cluster.

THREE THINGS THIS FILE HOLDS THE RENDERER TO.

  * The data volume is mounted BEFORE the container starts. A container whose
    mount failed creates its data directory on the boot volume and writes there
    perfectly happily; the machine looks healthy until somebody detaches the
    volume that was supposed to hold the data and finds it empty.
  * The image is pulled BY DIGEST, and the machine reports the digest it
    actually got. A tag can be repointed by its publisher after the proof passed.
  * A profile contributes DATA, never a podman flag. That rule lives in
    common/profile_rules and is exercised there; what is asserted here is that
    the renderer builds the command from validated fields only.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import configure

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")

RABBITMQ = {
    "code": "rabbitmq", "builds_on": "oci/service-vm", "ports": [5672, 15672],
    "version_command": "podman exec rabbitmq rabbitmqctl version",
    "container": {
        "image": "docker.io/library/rabbitmq", "tag": "4.1-management",
        "digest": "sha256:" + "b" * 64,
        "data_dir": "/var/lib/rabbitmq", "data_mount": "/var/lib/rabbitmq",
    },
    "rhel": {"packages": [], "services": ["rabbitmq"]},
}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)


def render(tmp_path, monkeypatch, profile=None, code="rabbitmq"):
    (tmp_path / f"{code}.json").write_text(json.dumps(profile or RABBITMQ))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    return configure.render([{"technology_code": code}], "rhel", url)


def doc(tmp_path, monkeypatch, **kw):
    return yaml.safe_load(render(tmp_path, monkeypatch, **kw))


def unit(tmp_path, monkeypatch, **kw):
    files = doc(tmp_path, monkeypatch, **kw)["write_files"]
    return next(f["content"] for f in files if f["path"].endswith(".container"))


def report(tmp_path, monkeypatch, **kw):
    files = doc(tmp_path, monkeypatch, **kw)["write_files"]
    return next(f["content"] for f in files if f["path"].endswith("report.sh"))


def runcmd(tmp_path, monkeypatch, **kw):
    out = render(tmp_path, monkeypatch, **kw)
    return out[out.index("runcmd:"):]


# --- the technology reaches the machine at all --------------------------------

def test_a_container_profile_is_not_dropped_for_installing_no_packages(
        tmp_path, monkeypatch):
    """`profile_for` asked whether a profile declared packages or an archive.
    A container profile legitimately declares neither, so it was discarded —
    and a discarded profile is silent: nothing installs and nothing says why."""
    assert "rabbitmq" in render(tmp_path, monkeypatch)


def test_the_runtime_is_supplied_by_the_renderer_not_the_profile(
        tmp_path, monkeypatch):
    """What runs a container is a property of this renderer's choice, not of the
    technology. A profile that had to remember `podman` would, when it forgot,
    produce a machine that pulls nothing and reports only that a service is
    inactive."""
    assert "dnf install -y podman" in runcmd(tmp_path, monkeypatch)


# --- the data volume ----------------------------------------------------------

def test_the_volume_is_mounted_before_the_container_starts(tmp_path, monkeypatch):
    """THE ordering that matters. A failed mount is invisible: the container
    creates its data directory on the boot volume and works perfectly, until the
    volume it was supposed to be using is detached and found empty."""
    run = runcmd(tmp_path, monkeypatch)
    assert run.index("mountpoint -q /var/lib/rabbitmq") < run.index("podman pull")
    assert run.index("podman pull") < run.index("systemctl start rabbitmq")


def test_an_existing_filesystem_is_never_reformatted(tmp_path, monkeypatch):
    """This script runs at every first boot. A machine rebuilt and reattached to
    its old volume must find its data, not lose it — so mkfs is guarded by
    whether a filesystem is already there."""
    run = runcmd(tmp_path, monkeypatch)
    assert "blkid" in run and "mkfs.xfs" in run
    mkfs_line = next(l for l in run.splitlines() if "mkfs.xfs" in l)
    assert "blkid" in mkfs_line, "mkfs runs unconditionally"


def test_the_device_is_found_rather_than_assumed(tmp_path, monkeypatch):
    """A paravirtualized volume appears under /dev/oracleoci where oci-utils is
    installed and as a plain /dev/sdX where it is not, and the kernel may order
    those differently between boots."""
    run = runcmd(tmp_path, monkeypatch)
    assert "/dev/oracleoci/oraclevdb" in run and "/dev/sdb" in run
    assert "[ -b " in run, "the candidates are used without testing they exist"


def test_fstab_names_the_volume_by_uuid(tmp_path, monkeypatch):
    """`/dev/sdb` is not a stable identity across reboots, and an fstab entry
    pointing at the wrong disk is worse than no entry at all."""
    run = runcmd(tmp_path, monkeypatch)
    assert "blkid -s UUID -o value" in run
    fstab = next(l for l in run.splitlines() if "/etc/fstab" in l and "echo" in l)
    assert "UUID=" in fstab


def test_the_mount_cannot_leave_the_machine_unbootable(tmp_path, monkeypatch):
    """A private machine that fails to boot cannot be logged in to and cannot
    report. `nofail` is what keeps a slow volume from being fatal."""
    run = runcmd(tmp_path, monkeypatch)
    assert "nofail" in run


def test_the_machine_says_whether_its_data_volume_actually_mounted(
        tmp_path, monkeypatch):
    assert "data_volume_rabbitmq=" in report(tmp_path, monkeypatch)
    assert "not-mounted" in report(tmp_path, monkeypatch)


def test_a_container_with_no_data_directory_asks_for_no_volume(
        tmp_path, monkeypatch):
    """Not every image keeps state. Mounting a volume for one that does not is a
    block volume billed for nothing."""
    stateless = {**RABBITMQ, "container": {
        k: v for k, v in RABBITMQ["container"].items()
        if k not in ("data_dir", "data_mount")}}
    run = runcmd(tmp_path, monkeypatch, profile=stateless)
    assert "mkfs.xfs" not in run and "/etc/fstab" not in run


# --- the image ----------------------------------------------------------------

def test_the_pull_is_by_digest_not_by_tag(tmp_path, monkeypatch):
    run = runcmd(tmp_path, monkeypatch)
    pull = next(l for l in run.splitlines() if "podman pull" in l)
    assert "@sha256:" in pull
    assert ":4.1-management" not in pull, "it pulled a tag that can be repointed"


def test_the_unit_runs_the_digest_too(tmp_path, monkeypatch):
    """Pulling by digest and then RUNNING by tag would defeat the whole point:
    podman would resolve the tag again at start."""
    assert "@sha256:" in unit(tmp_path, monkeypatch)
    lines = [l for l in unit(tmp_path, monkeypatch).splitlines()
             if l.startswith("Image=")]
    assert len(lines) == 1 and "@sha256:" in lines[0]


def test_the_machine_reports_the_digest_it_ACTUALLY_got(tmp_path, monkeypatch):
    """A pull that silently resolved elsewhere, or a stale image already on the
    host, is otherwise indistinguishable from the pinned one."""
    text = report(tmp_path, monkeypatch)
    assert "image_rabbitmq=" in text
    assert "RepoDigests" in text, "it asks podman nothing about what it holds"


def test_the_machine_reports_whether_the_container_is_running(
        tmp_path, monkeypatch):
    assert "container_rabbitmq=" in report(tmp_path, monkeypatch)


# --- the unit -----------------------------------------------------------------

def test_the_declared_ports_are_published_to_the_host(tmp_path, monkeypatch):
    """Published on the host is what makes every existing check work unchanged:
    the firewall rule, the `ss` line and the http probe all look at the host."""
    text = unit(tmp_path, monkeypatch)
    assert "PublishPort=5672:5672" in text
    assert "PublishPort=15672:15672" in text
    assert "firewall-cmd --permanent --add-port=5672/tcp" in runcmd(tmp_path, monkeypatch)


def test_the_volume_is_relabelled_for_selinux(tmp_path, monkeypatch):
    """SELinux is enforcing on Oracle Linux. Without `:Z` the container is denied
    access to its own data directory, and the service fails for a reason that
    looks nothing like a mount problem."""
    assert "/var/lib/rabbitmq:/var/lib/rabbitmq:Z" in unit(tmp_path, monkeypatch)


def test_the_unit_survives_a_reboot(tmp_path, monkeypatch):
    """A generated unit cannot be `systemctl enable`d, so the [Install] section
    is the only thing that brings the service back."""
    text = unit(tmp_path, monkeypatch)
    assert "[Install]" in text and "WantedBy=multi-user.target" in text


def test_a_quadlet_unit_is_started_not_enabled(tmp_path, monkeypatch):
    """`systemctl enable` on a Quadlet-generated unit fails. Using the ordinary
    enable path would leave every container profile reporting a service that
    never started."""
    run = runcmd(tmp_path, monkeypatch)
    assert "systemctl start rabbitmq" in run
    assert "systemctl enable --now rabbitmq" not in run
    assert run.index("daemon-reload") < run.index("systemctl start rabbitmq")


def test_a_container_waits_for_its_port_like_an_archive_does(tmp_path, monkeypatch):
    """An image has to be pulled, unpacked and started before anything listens —
    the slowest of the three install methods."""
    assert "seq 1 60" in render(tmp_path, monkeypatch)


# --- it has to actually parse -------------------------------------------------

def test_the_whole_document_is_valid_cloud_config(tmp_path, monkeypatch):
    assert isinstance(doc(tmp_path, monkeypatch), dict)


@pytest.mark.skipif(not shutil.which("sh"), reason="no POSIX shell available")
def test_the_report_script_still_parses_under_sh(tmp_path, monkeypatch):
    """The container evidence lines carry Go template braces and nested quotes,
    which is exactly the shape that has broken this script before."""
    path = tmp_path / "report.sh"
    path.write_text(report(tmp_path, monkeypatch), newline="\n")
    done = subprocess.run([shutil.which("sh"), "-n", str(path)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_nothing_changes_for_a_technology_with_no_container(tmp_path, monkeypatch):
    """nginx, redis7, java21, python312, nodejs20, keycloak and vault are all
    certified against this renderer. A container feature that altered their
    first boot would invalidate evidence bought with real machines."""
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    out = configure.render([{"technology_code": "nginx", "version": "1.24"}],
                           "rhel", url)
    for marker in ("podman", "Quadlet", ".container", "mkfs.xfs", "/etc/fstab"):
        assert marker not in out, f"{marker} leaked into an ordinary VM recipe"


# --- what is listening INSIDE, which is a different question -------------------
#
# `library/rabbitmq` declares six ports; a default container binds far fewer,
# and every declared port would be opened in a real machine's firewall.
#
# A plant that changed this probe to read the HOST's /proc instead of the
# container's passed every test in this file, so the premise of the whole
# narrowing design was unasserted.

def test_the_probe_asks_inside_the_container_not_the_host(tmp_path, monkeypatch):
    """A PUBLISHED PORT BINDS ON THE HOST WHETHER OR NOT THE CONTAINER LISTENS.
    podman's proxy answers either way, so the host's own socket table can never
    narrow anything — it would report exactly what was published and call that a
    measurement."""
    text = report(tmp_path, monkeypatch)
    probe = next(l for l in text.splitlines() if "/proc/net/tcp" in l)
    assert "podman exec rabbitmq cat" in probe, (
        f"the probe reads the host's own sockets: {probe.strip()}")


def test_it_reads_proc_rather_than_a_tool_the_image_may_not_have(
        tmp_path, monkeypatch):
    """/proc/net/tcp exists in every Linux container with nothing installed. A
    great many images carry no networking tools at all, and `ss: not found`
    would look identical to "nothing is listening"."""
    probe = next(l for l in report(tmp_path, monkeypatch).splitlines()
                 if "listening_inside" in l or "/proc/net/tcp" in l)
    assert "/proc/net/tcp" in probe or "LI=" in probe
    text = report(tmp_path, monkeypatch)
    assert "podman exec rabbitmq ss" not in text


def test_only_listening_sockets_count(tmp_path, monkeypatch):
    """State 0A is LISTEN. Counting established connections would report
    whatever the machine happened to be talking to as a service port."""
    assert '$4=="0A"' in report(tmp_path, monkeypatch)


def test_both_address_families_are_read(tmp_path, monkeypatch):
    """A container binding only on IPv6 would otherwise report nothing, and the
    narrowed profile would publish no ports at all."""
    text = report(tmp_path, monkeypatch)
    assert "/proc/net/tcp " in text or "/proc/net/tcp6" in text
    assert "/proc/net/tcp6" in text


def test_the_hex_port_is_converted_without_a_gawk_extension(tmp_path, monkeypatch):
    """`strtonum` is gawk-only, and the awk inside an arbitrary image is not
    guaranteed to be gawk. printf is POSIX."""
    text = report(tmp_path, monkeypatch)
    assert "strtonum" not in text
    assert "printf" in text


# --- the volume Terraform actually creates ------------------------------------
#
# The requester's `storage_gb` sized the BOOT disk, because until now that was
# the only disk there was. For a technology whose data lives on its own volume,
# someone asking for RabbitMQ with 100 GB means 100 GB for their messages — so
# the requested figure follows the data and the boot volume stays standard.
# Sizing both from one number would bill for the storage twice.

def _payload(code="rabbitmq", storage=100):
    return {"os_family": "rhel",
            "policy_input": {"components": [
                {"technology_code": code, "size": "small", "storage_gb": storage}]}}


def test_a_container_with_data_gets_its_own_volume(tmp_path, monkeypatch):
    from orchestrator import main as orch
    (tmp_path / "rabbitmq.json").write_text(json.dumps(RABBITMQ))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    assert orch._wants_a_data_volume(_payload()) is True
    assert orch._data_volume_gb(_payload(storage=100)) == 100


def test_the_requested_storage_follows_the_DATA_not_the_os_disk(
        tmp_path, monkeypatch):
    from orchestrator import main as orch
    (tmp_path / "rabbitmq.json").write_text(json.dumps(RABBITMQ))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    payload = _payload(storage=100)
    assert orch._data_volume_gb(payload) == 100
    assert orch._boot_volume_gb(payload) == 100, (
        "the boot figure itself is unchanged; the SIZING dict is what redirects")


def test_a_technology_with_no_container_asks_for_no_data_volume(
        tmp_path, monkeypatch):
    """nginx and every other certified technology must pass 0, so their plan is
    byte-for-byte what it was and evidence bought with real machines holds."""
    from orchestrator import main as orch
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    assert orch._wants_a_data_volume(_payload(code="nginx")) is False
    assert orch._data_volume_gb(_payload(code="nginx")) == 0


def test_a_stateless_container_asks_for_no_volume(tmp_path, monkeypatch):
    """Not every image keeps state; a volume for one that does not is billed for
    nothing."""
    from orchestrator import main as orch
    stateless = {**RABBITMQ, "container": {
        k: v for k, v in RABBITMQ["container"].items()
        if k not in ("data_dir", "data_mount")}}
    (tmp_path / "rabbitmq.json").write_text(json.dumps(stateless))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    assert orch._wants_a_data_volume(_payload()) is False


def test_the_terraform_variable_defaults_to_creating_nothing():
    """The module's own default. `count = var.data_volume_gb > 0 ? ... : 0`
    means an unset variable creates no volume and no attachment at all."""
    import pathlib
    tf = pathlib.Path(configure.__file__).parent / "terraform/oci/service-vm"
    variables = (tf / "variables.tf").read_text(encoding="utf-8")
    assert 'variable "data_volume_gb"' in variables
    block = variables[variables.index('variable "data_volume_gb"'):]
    assert "default     = 0" in block or "default = 0" in block
    main = (tf / "main.tf").read_text(encoding="utf-8")
    assert "var.data_volume_gb > 0 ?" in main, (
        "the volume is created unconditionally, so every VM gets one")
    assert 'attachment_type = "paravirtualized"' in main, (
        "an iSCSI attachment needs iscsiadm run at first boot on a machine "
        "nobody can log in to")


def test_the_requested_storage_is_not_billed_on_both_disks(tmp_path, monkeypatch):
    """A 100 GB request must produce ONE 100 GB volume, not a 100 GB boot disk
    AND a 100 GB data disk. Testing the helpers alone missed this: they are both
    correct in isolation and it is the dict assembled from them that decides
    what Terraform bills for."""
    from orchestrator import main as orch
    (tmp_path / "rabbitmq.json").write_text(json.dumps(RABBITMQ))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)

    spec = orch._compute_spec(_payload(storage=100), "oci-service-vm")
    assert spec["data_volume_gb"] == 100
    assert spec["boot_volume_gb"] == orch._DEFAULT_BOOT_VOLUME_GB, (
        f"the boot disk was also sized at {spec['boot_volume_gb']} GB, so the "
        f"requester pays twice for storage they asked for once")


def test_an_ordinary_vm_still_sizes_its_boot_disk_from_the_request(
        tmp_path, monkeypatch):
    """The other half. `storage_gb` sizing the boot volume is behaviour someone
    already had to fix once — a 'large' request paid for 500 GB and was built on
    the image default — and containers must not undo it."""
    from orchestrator import main as orch
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)

    spec = orch._compute_spec(_payload(code="nginx", storage=100), "oci-service-vm")
    assert spec["boot_volume_gb"] == 100
    assert spec["data_volume_gb"] == 0


# --- what REQ-2026-0192 taught, on real hardware ------------------------------
#
# The container rung worked: podman installed, the pinned digest pulled, the
# container running, the block volume mounted, the unit active, no PORTAL
# FAILURE lines. It failed on two questions asked badly.

PINNED = {
    "code": "rabbitmq", "builds_on": "oci/service-vm", "ports": [],
    "container": {"image": "docker.io/library/rabbitmq", "tag": "latest",
                  "digest": "sha256:" + "9d392587" * 8,
                  "data_dir": "/var/lib/rabbitmq", "data_mount": "/var/lib/rabbitmq"},
    "rhel": {"packages": [], "services": ["rabbitmq"]},
}


def test_a_pinned_container_is_not_asked_its_version(tmp_path, monkeypatch):
    """There is no general way to put the question. `podman exec rabbitmq
    rabbitmq --version` got "no such executable" from crun, and the obvious
    repair — the image's org.opencontainers.image.version label — reads "24.04"
    on library/rabbitmq, which is UBUNTU's version. A confidently wrong number
    is worse than none."""
    text = report(tmp_path, monkeypatch, profile=PINNED)
    assert "version_rabbitmq" not in text


def test_the_digest_is_COMPARED_on_the_machine_not_merely_printed(
        tmp_path, monkeypatch):
    """It was printed and nothing read it: verdict() judges failed/inactive,
    http_, version_ and firewall_ lines, and `image_x=docker.io/…@sha256:…`
    matched none of them — so a machine running an entirely different image
    would have passed."""
    text = report(tmp_path, monkeypatch, profile=PINNED)
    line = next(l for l in text.splitlines() if "image_rabbitmq=" in l)
    assert "test " in line and "match" in line and "MISMATCH" in line, (
        f"the digest is reported without being compared: {line.strip()}")


def test_a_mismatched_image_fails_the_machine():
    from orchestrator import boot_reports
    assert boot_reports.verdict("image_rabbitmq=match (sha256:9d39…)")["ok"] is True
    bad = boot_reports.verdict("image_rabbitmq=MISMATCH got nothing")
    assert bad["ok"] is False
    assert "not running the image that was pinned" in bad["problems"][0]


def test_the_listening_probe_waits_for_the_container_to_bind(
        tmp_path, monkeypatch):
    """THE structural defect. The first container pass publishes no ports by
    design, so the port-wait loop had nothing to wait for and this ran seconds
    after `systemctl start` — before RabbitMQ had bound anything. The wait was
    built to depend on the very thing it was meant to discover."""
    text = report(tmp_path, monkeypatch, profile=PINNED)
    probe = text[text.index("LI="):text.index("listening_inside_rabbitmq=")]
    assert "seq 1 36" in probe, "it samples once and calls that a measurement"
    assert "sleep 5" in probe
    assert '[ -n "$LI" ] && break' in probe, (
        "it waits the full three minutes even when the container binds at once")


def test_the_wait_is_bounded():
    """A container that never binds must still report, and report `none` — an
    unbounded wait would hang first boot and the machine would say nothing at
    all."""
    import re
    from orchestrator import configure as c
    source = pathlib.Path(c.__file__).read_text(encoding="utf-8")
    assert re.search(r"seq 1 36", source)


def test_the_port_wait_does_not_assume_http(tmp_path, monkeypatch):
    """REQ-2026-0193's narrowing proof timed out at 2202s — "still waiting for
    oci-service-vm to report" — because the wait curled each port and broke on
    success. AMQP on 5672, epmd on 4369 and clustering on 25672 do not speak
    HTTP, so curl never succeeded and it waited the full five minutes on each:
    twenty minutes before the report was even written.

    A LISTENING SOCKET is what "the service is up" means, and `ss` answers it
    for any protocol. A plant reverting this to curl passed all 730 orchestrator
    tests, because nothing asserted how the wait asks."""
    ported = {**PINNED, "ports": [4369, 5672, 25672]}
    run = runcmd(tmp_path, monkeypatch, profile=ported)
    waits = [l for l in run.splitlines() if "seq 1 60" in l]
    assert waits, "nothing waits for the service to come up"
    for line in waits:
        assert "ss -lnt" in line, f"the wait assumes HTTP: {line.strip()}"
        assert "curl" not in line
    assert "grep -q ':5672 '" in run

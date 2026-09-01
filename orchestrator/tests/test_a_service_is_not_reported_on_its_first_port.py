"""OpenSearch certified on a port nobody can query.

REQ-2026-0253 built OpenSearch, and the machine reported this:

    declares_opensearch=9200,9300,9600,9650
    listening_inside_opensearch=9300
    opened_ports=9300   LISTEN 0.0.0.0:9300   firewall_9300=open

9300 is the port OpenSearch nodes use to talk to each other. 9200 is the REST
API a client actually calls — it is the entire reason to run a search engine.
The readiness loop broke out of its wait at the FIRST declared port to appear,
so the report was written while 9200 was very likely still binding, and the
certification gate accepted the component on that evidence.

Nothing here proves 9200 never came up. That is the complaint: the check
stopped looking, so the report cannot say either way, and a proof that cannot
say is not a proof.

WAITING FOR EVERY PORT IS ALSO WRONG, which is why "any" was chosen in the
first place. `library/rabbitmq` declares six — AMQP, AMQPS, epmd, clustering
and two Prometheus endpoints — and a default container binds fewer by design.
Demanding all six would fail a healthy broker.

So the rule is split in two, because the verdict and the evidence are different
things:

    the verdict    unchanged. ANY declared port listening means don't fail.
    the evidence   keep watching until ALL of them are up, or a bounded grace
                   period runs out.

A healthy multi-port service now leaves EARLIER than before, because all its
ports are up on the first pass and it stops there. Only the partial case waits,
and only for a minute.

THESE TESTS RUN THE GENERATED SHELL. An earlier check in this suite asserted
that the script contained the right words, and a planted version that accepted
every image passed it. The loop is extracted, its port-reading line replaced by
a stub that answers a scripted sequence, and `sh` executes it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap

import pytest
import yaml

from orchestrator import configure

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")

SH = shutil.which("sh") or shutil.which("bash")
pytestmark = pytest.mark.skipif(SH is None, reason="no POSIX shell on this host")


def profile(code, ports):
    return {
        "code": code, "builds_on": "oci/service-vm", "ports": ports,
        "container": {
            "image": f"docker.io/library/{code}", "tag": "1",
            "digest": "sha256:" + "b" * 64,
            "data_dir": f"/var/lib/{code}", "data_mount": f"/var/lib/{code}",
        },
        "rhel": {"packages": [], "services": [code]},
    }


def loop_of(tmp_path, monkeypatch, code, ports):
    """The readiness loop as the machine would receive it.

    The code must not be one of `configure.TEMPLATES` — a shipped technology is
    installed as a package and has no container to wait on.
    """
    assert code not in configure.TEMPLATES, f"{code} is a package, not a container"
    (tmp_path / f"{code}.json").write_text(json.dumps(profile(code, ports)))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)

    doc = yaml.safe_load(configure.render(
        [{"technology_code": code}], "rhel",
        configure.boot_report_url("REQ-2026-0253", "oci-service-vm")))
    report = next(f["content"] for f in doc["write_files"]
                  if f["path"].endswith("report.sh"))

    start = report.index("  LI=; READY=; GRACE=0")
    end = report.index("\n  done", start) + len("\n  done")
    return report[start:end]


def run(tmp_path, loop, declares, passes):
    """Drive the loop with a scripted sequence of what is listening.

    `passes` is a list, one entry per five-second pass: a comma-separated port
    list, or None for nothing listening yet. The last entry repeats for as long
    as the loop keeps asking, so a service that never binds the rest is
    modelled by a list that stops changing.

    THE STUB SETS A VARIABLE rather than being called in `$(...)`. Command
    substitution runs in a subshell, so a counter incremented inside one is
    lost and every pass answers the same thing. The first version of this
    harness did exactly that, and reported a defect in correct code.
    """
    arms = "\n".join(
        f"    {i + 1}) PORTS='{'' if p is None else p}';;"
        for i, p in enumerate(passes))
    last = "" if passes[-1] is None else passes[-1]

    # Only the line that READS the ports is replaced. Everything that decides
    # when to stop is the generated script, untouched.
    reader = next(ln for ln in loop.splitlines() if ln.strip().startswith("LI=$("))
    loop = loop.replace(reader, "    next_ports\n    LI=$PORTS")
    # And the wait itself, so a test is not the length of a real proof.
    loop = loop.replace("    sleep 5", "    :")

    head = textwrap.dedent("""\
        STEP=0
        next_ports() {
          STEP=$((STEP+1))
          case $STEP in
        """)
    head += arms + "\n"
    head += textwrap.dedent(f"""\
            *) PORTS='{last}';;
          esac
        }}
        DECL="{declares}"
        """)
    tail = textwrap.dedent("""
        echo "FINAL_LI=${LI:-none}"
        echo "FINAL_READY=${READY:-no}"
        echo "FINAL_PASSES=$STEP"
        """)

    (tmp_path / "drive.sh").write_text(head + loop + tail, newline="\n")
    out = subprocess.run([SH, (tmp_path / "drive.sh").as_posix()],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr

    return {
        "listening": re.search(r"FINAL_LI=(\S+)", out.stdout).group(1),
        "ready": re.search(r"FINAL_READY=(\S+)", out.stdout).group(1) == "1",
        "passes": int(re.search(r"FINAL_PASSES=(\d+)", out.stdout).group(1)),
    }


# --- the harness has to be able to fail ------------------------------------------

def test_the_stub_actually_advances(tmp_path, monkeypatch):
    """If every pass answered the same thing, the tests below would still pass
    for the wrong reason — a fixed answer of "all ports up" exits on pass one
    and looks exactly like correct behaviour."""
    loop = loop_of(tmp_path, monkeypatch, "somesoftware", [])
    out = run(tmp_path, loop, "9200", [None, None, "9200"])

    assert out["passes"] == 3, "the scripted sequence is not being consumed"


# --- the defect ------------------------------------------------------------------

def test_the_api_port_is_waited_for_when_the_transport_port_comes_up_first(
        tmp_path, monkeypatch):
    """REQ-2026-0253, exactly. 9300 binds, then 9200 a moment later.

    Before this change the report was written on the first pass that saw
    anything and said `listening_inside_opensearch=9300` — no mention of the
    REST API at all.
    """
    loop = loop_of(tmp_path, monkeypatch, "opensearch", [9200, 9300])
    out = run(tmp_path, loop, "9200,9300",
              [None, "9300", "9300", "9200,9300"])

    assert out["listening"] == "9200,9300", (
        "the report still records only the port that happened to bind first")
    assert out["ready"]
    assert out["passes"] == 4, "it kept waiting after both were up"


def test_a_service_with_every_port_up_is_not_waited_for_at_all(
        tmp_path, monkeypatch):
    """The common case must not get slower. All declared ports up on the first
    look means the loop leaves on the first look."""
    loop = loop_of(tmp_path, monkeypatch, "caddy", [80, 443])
    out = run(tmp_path, loop, "80,443", ["80,443"])

    assert out["passes"] == 1
    assert out["listening"] == "80,443"
    assert out["ready"]


# --- and the case that made "any" the rule ----------------------------------------

def test_a_broker_that_binds_three_of_six_by_design_still_passes(
        tmp_path, monkeypatch):
    """RabbitMQ declares six ports. A default container binds AMQP, epmd and the
    management UI, and that is a healthy broker. `READY` must still be set, or
    every proof of it would fail."""
    loop = loop_of(tmp_path, monkeypatch, "rabbitmq", [5672, 15672])
    out = run(tmp_path, loop, "4369,5672,5671,15672,15691,15692",
              ["4369,5672,15672"])

    assert out["ready"], "a healthy broker would now fail its proof"
    assert out["listening"] == "4369,5672,15672"


def test_waiting_for_the_missing_ports_is_bounded(tmp_path, monkeypatch):
    """And it must not wait the full deadline for ports that are never coming.

    Thirty-six passes is three minutes on a real machine. A partial service is
    given a minute past its first port, not three."""
    loop = loop_of(tmp_path, monkeypatch, "rabbitmq", [5672, 15672])
    out = run(tmp_path, loop, "4369,5672,5671,15672,15691,15692",
              ["4369,5672,15672"])

    assert out["passes"] <= 14, (
        f"{out['passes']} five-second passes — the grace period is not bounded")
    assert out["passes"] > 1, (
        "it left on the first port, so one arriving seconds later is missed")


# --- nothing changes for the cases that were already right ------------------------

def test_a_service_that_binds_nothing_still_waits_the_full_deadline(
        tmp_path, monkeypatch):
    """This is how a dead container is caught: READY is never set, so the
    report captures `podman logs`. OpenShift died exactly this way."""
    loop = loop_of(tmp_path, monkeypatch, "openshift", [53, 8443])
    out = run(tmp_path, loop, "53,8443", [None])

    assert out["passes"] == 36, "a dead container is now given up on early"
    assert out["listening"] == "none"
    assert not out["ready"], "a container listening on nothing was called ready"


def test_an_image_declaring_no_ports_keeps_the_old_rule(tmp_path, monkeypatch):
    """No claim to wait for, so anything listening will do — and the grace
    period must not apply, because there is nothing it could be waiting for."""
    loop = loop_of(tmp_path, monkeypatch, "somesoftware", [])
    out = run(tmp_path, loop, "", ["7777"])

    assert out["passes"] == 1
    assert out["ready"]
    assert out["listening"] == "7777"


def test_an_image_declaring_no_ports_and_binding_none_is_still_not_ready(
        tmp_path, monkeypatch):
    loop = loop_of(tmp_path, monkeypatch, "somesoftware", [])
    out = run(tmp_path, loop, "", [None])

    assert out["passes"] == 36
    assert not out["ready"]


# --- the deadline itself ----------------------------------------------------------

def test_the_worst_case_wait_did_not_grow(tmp_path, monkeypatch):
    """The change may make a proof finish sooner; it must never make one take
    longer than the three minutes it already allowed."""
    loop = loop_of(tmp_path, monkeypatch, "opensearch", [9200])

    assert "seq 1 36" in loop and "sleep 5" in loop, (
        "the deadline is no longer 36 passes of five seconds")

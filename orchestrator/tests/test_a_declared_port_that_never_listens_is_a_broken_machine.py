"""REQ-2026-0256 was marked provisioned with nothing listening inside it.

The machine reported this, and every check the verdict made was satisfied:

    container_opensearch=running
    declares_opensearch=9200,9300,9600,9650
    listening_inside_opensearch=none
    opened_ports=9300
    LISTEN 0      4096         0.0.0.0:9300       0.0.0.0:*
    firewall_9300=open

THE CHECK THAT COULD BE FOOLED PASSED AND THE CHECK THAT KNEW THE TRUTH WAS
NEVER READ. `ss -lnt` on the host saw 0.0.0.0:9300 and reported a listener, but
for a container that is podman's published-port proxy -- it exists whether or
not anything inside ever binds. `listening_inside_` is read inside the
container's own network namespace, it said `none`, and no rule looked at it.

It is also why `answered_but_broken` can forgive an `http_ 000`: it reasons that
absence "is already caught -- `nothing listening on <port>` is a failure in its
own right". For a container it was not caught, because the host-side line the
report writes is about the proxy, not the service.

THE RULE IS THE ONE THE MACHINE ALREADY APPLIED. The first-boot script waits
until a declared port is listening and gives up after three minutes. That
verdict was computed on the machine and thrown away; deriving it here from the
two lines the report does carry judges old reports by it too, without asking
anything of machines that are already running.

Surveyed against every boot report on record before being enabled -- the lesson
from the certification gate that made bare VMs unprovisionable for twelve days.
Thirty reports, four of them container builds that declare ports, and exactly
one newly condemned: REQ-2026-0256, the machine already known to be broken.
"""

from __future__ import annotations

from orchestrator.boot_reports import verdict

# REQ-2026-0256, as the machine wrote it. Trimmed of the container log, which
# runs to twenty lines, and otherwise untouched.
REQ_0256 = """os_family=rhel
technologies=opensearch
os=Oracle Linux Server 9.8
--- packages ---
podman-5.8.2-6.0.1.el9_8.x86_64
image_opensearch=match (sha256:bcc179751972...)
container_opensearch=running
declares_opensearch=9200,9300,9600,9650
listening_inside_opensearch=none
  opensearch log: Enabling execution of performance-analyzer-agent-cli
data_volume_opensearch=mounted
--- services ---
opensearch=active
--- ports ---
opened_ports=9300
http_9300=000
LISTEN 0      4096         0.0.0.0:9300       0.0.0.0:*
firewall_9300=open
--- first-boot log ---
PORTAL: first-boot configuration finished
"""


def problems(report):
    return verdict(report)["problems"]


# --- the defect ------------------------------------------------------------------

def test_the_machine_that_was_marked_provisioned_is_now_condemned():
    out = verdict(REQ_0256)

    assert not out["ok"], "REQ-2026-0256 would be marked provisioned again"
    assert any("opensearch" in p and "9200,9300,9600,9650" in p
               for p in out["problems"]), out["problems"]


def test_the_host_side_listener_does_not_rescue_it():
    """THE WHOLE TRAP. The report carries `opened_ports=9300`, an `ss` line
    showing 0.0.0.0:9300 LISTEN, and `firewall_9300=open` -- three lines that
    all look like a working service and all describe podman's proxy."""
    assert "LISTEN" in REQ_0256 and "firewall_9300=open" in REQ_0256, (
        "this test no longer exercises the host-side lines it is about")

    assert not verdict(REQ_0256)["ok"]


def test_a_person_can_act_on_what_it_says():
    """A refusal must explain and guide. Naming the technology, what it claimed
    and what it actually did is the difference between a fixable report and
    'verification failed'."""
    said = " ".join(problems(REQ_0256))

    assert "opensearch" in said
    assert "9200,9300,9600,9650" in said, "it does not say what was claimed"
    assert "log" in said.lower(), "it does not point at the evidence"


# --- and the cases that must keep passing -----------------------------------------

def test_a_broker_serving_three_of_six_declared_ports_is_healthy():
    """RabbitMQ declares six ports -- AMQP, AMQPS, epmd, clustering and two
    Prometheus endpoints -- and a default container binds three by design. ONE
    declared port listening is enough, exactly as it is on the machine. A rule
    demanding all of them would fail every healthy broker."""
    out = verdict("declares_rabbitmq=4369,5671,5672,15691,15692,25672\n"
                  "listening_inside_rabbitmq=4369,5672,25672\n")

    assert out["ok"], out["problems"]


def test_an_image_that_declares_no_ports_makes_no_claim_to_break():
    """A batch or worker container is entitled to listen on nothing. Only a
    stated claim can be broken, so `declares=none` is not a failure."""
    out = verdict("container_worker=running\n"
                  "declares_worker=none\n"
                  "listening_inside_worker=none\n")

    assert out["ok"], out["problems"]


def test_a_package_install_is_untouched():
    """Most of the catalogue is installed with dnf and has no container, so no
    `declares_` line at all. The rule must be silent about those rather than
    inventing a claim they never made."""
    out = verdict("os_family=rhel\nnginx=active\nfirewall_80=open\n")

    assert out["ok"], out["problems"]


# --- the edges --------------------------------------------------------------------

def test_listening_on_a_port_it_never_declared_is_not_serving():
    """A red herring is not a service. The image said which ports it serves;
    something else being up says nothing about that claim."""
    out = verdict("declares_thing=9200,9300\nlistening_inside_thing=7777\n")

    assert not out["ok"]
    assert "7777" in " ".join(out["problems"])


def test_a_hyphenated_technology_code_is_still_caught():
    """CATALOGUE CODES CONTAIN HYPHENS, and `declares_oracle-db` is not a Python
    identifier -- the branch that reads every other `key=value` line guards on
    `key.isidentifier()` and skips it entirely. That is exactly how
    `archive_oracle-db=failed` was once read as healthy."""
    out = verdict("container_oracle-db=running\n"
                  "declares_oracle-db=1521,5500\n"
                  "listening_inside_oracle-db=none\n")

    assert not out["ok"], "a hyphenated code slips through the check again"
    assert "oracle-db" in " ".join(out["problems"])


def test_the_ports_it_does_serve_are_matched_exactly():
    """`920` must not satisfy a claim to serve `9200`, and `9200` must not be
    read out of `19200`. Substring matching on a comma-joined list would do
    both."""
    assert not verdict("declares_x=9200\nlistening_inside_x=920\n")["ok"]
    assert not verdict("declares_x=9200\nlistening_inside_x=19200\n")["ok"]
    assert verdict("declares_x=9200\nlistening_inside_x=19200,9200\n")["ok"]


def test_two_technologies_on_one_machine_are_judged_separately():
    """A machine can carry more than one. One healthy service must not vouch
    for a broken one sharing the report."""
    out = verdict("declares_good=8080\nlistening_inside_good=8080\n"
                  "declares_bad=9200\nlistening_inside_bad=none\n")

    assert not out["ok"]
    assert len(out["problems"]) == 1, out["problems"]
    assert "bad" in out["problems"][0] and "good" not in out["problems"][0]


def test_a_missing_listening_line_is_not_taken_as_success():
    """An older report, or one truncated before the line was written. Absence of
    the answer is not the answer."""
    out = verdict("declares_thing=9200,9300\n")

    assert not out["ok"], "a claim with no answer at all was read as healthy"

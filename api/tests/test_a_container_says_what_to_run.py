"""Reading a command out of a container's own help screen (C9b).

THE SIBLING OF the container-environment tests, and the same argument: an image
that will not start has usually said why, in the log the boot report already
captures. `container_env` reads "it wants a variable". This reads "it wants to
be told what to do".

`minio/minio` on a real machine, 2026-09-06:

    container_minio=running
    declares_minio=9000
    listening_inside_minio=none
    minio log: USAGE:
    minio log:   minio [FLAGS] COMMAND [ARGS...]
    minio log: COMMANDS:
    minio log:   server  start object storage server

Three machines were spent refusing minio before anything could express a
command at all.
"""

from __future__ import annotations

from api import container_command
from common import profile_rules

MINIO_LOG = """\
os_family=rhel
technologies=minio
image_minio=match (sha256:14cea493d9a3...)
container_minio=running
declares_minio=9000
listening_inside_minio=none
  minio log: NAME:
  minio log:   minio - High Performance Object Storage
  minio log: USAGE:
  minio log:   minio [FLAGS] COMMAND [ARGS...]
  minio log: COMMANDS:
  minio log:   server  start object storage server
  minio log:
  minio log: FLAGS:
  minio log:   --certs-dir value, -S value  path to certs directory
  minio log:   --quiet                      disable startup and info messages
  minio log: VERSION:
  minio log:   RELEASE.2025-09-07T16-13-09Z
"""


# --- what the machine actually said -------------------------------------------

def test_the_command_is_read_from_the_images_own_help(tmp_path=None):
    """THE POINT OF THE MODULE. Nothing here knows the word "minio"."""
    names, takes_args = container_command.offered(MINIO_LOG, "minio")

    assert names == ["server"]
    assert takes_args is True


def test_only_that_containers_lines_are_read():
    """A report carries every technology on the machine. One container's help
    text is not evidence about another -- the same anchoring `container_env`
    needed, and for the same reason."""
    mixed = MINIO_LOG + "  gitea log: USAGE:\n  gitea log: COMMANDS:\n  gitea log:   web  start\n"

    assert container_command.offered(mixed, "minio")[0] == ["server"]
    assert container_command.offered(mixed, "gitea")[0] == ["web"]


def test_ordinary_log_output_is_not_mistaken_for_a_help_screen():
    """Software prints the word "commands" all the time. Only a usage screen
    prints USAGE, and taking a command out of application output would hand
    root an argument nobody offered."""
    chatty = ("  redis log: Ready to accept connections\n"
              "  redis log: COMMANDS:\n"
              "  redis log:   flushall  executed by admin\n")

    assert container_command.offered(chatty, "redis")[0] == []


def test_the_list_stops_at_the_next_section():
    """FLAGS: ends COMMANDS:. Without that every flag description reads as
    another command and the image looks like it offers a choice it does not."""
    names, _ = container_command.offered(MINIO_LOG, "minio")

    assert "certs-dir" not in names and "quiet" not in names


def test_a_container_that_printed_nothing_offers_nothing():
    assert container_command.offered("", "minio")[0] == []


# --- what may be answered without a person ------------------------------------

def test_one_command_with_arguments_is_given_the_mount_the_recipe_already_has():
    """Not a path invented here: the one path this portal knows the container
    has, because the recipe mounts it."""
    out = container_command.answer(["server"], takes_args=True,
                                   data_mount="/var/lib/minio", code="minio")

    assert out.command == "server /var/lib/minio"
    assert out.answerable is True


def test_a_command_that_takes_no_arguments_is_given_none():
    """`[ARGS...]` in the usage line is the image saying it takes one. Without
    it, appending a path would be inventing an argument."""
    out = container_command.answer(["run"], takes_args=False,
                                   data_mount="/var/lib/x", code="x")

    assert out.command == "run"


def test_a_choice_between_commands_is_a_persons_to_make():
    """An image offering `server` and `gateway` has a decision in it. This
    portal does not make decisions -- and the refusal names both, so whoever
    does has what they need."""
    out = container_command.answer(["server", "gateway"], takes_args=True,
                                   data_mount="/var/lib/x", code="x")

    assert out.blocked is True
    assert not out.command
    assert "server" in out.detail and "gateway" in out.detail


def test_a_refusal_explains_and_guides_rather_than_saying_no():
    """The standing rule for every refusal this portal makes."""
    out = container_command.answer(["server", "gateway"], code="x")

    assert len(out.detail) > 60
    assert "operator" in out.detail.lower()


def test_the_answer_names_what_it_did_and_why():
    out = container_command.for_report(MINIO_LOG, "minio",
                                       data_mount="/var/lib/minio")

    assert out.command == "server /var/lib/minio"
    assert "usage" in out.detail.lower()
    assert "server /var/lib/minio" in out.detail


def test_nothing_is_offered_for_a_container_that_never_asked():
    """The overwhelmingly common case: an image with a working CMD. Proposing a
    command for it would override something that already works."""
    healthy = ("  valkey log: Ready to accept connections tcp\n"
               "  valkey log: Server initialized\n")

    assert container_command.for_report(healthy, "valkey").command == ""


# --- it can never write something the unit-file rules did not agree to --------

def test_what_it_produces_is_acceptable_to_the_profile_rules():
    """The seam that matters: this module proposes, `profile_rules` decides, and
    a proposal it would refuse must never be made."""
    out = container_command.for_report(MINIO_LOG, "minio",
                                       data_mount="/var/lib/minio")

    assert profile_rules.CONTAINER_COMMAND.match(out.command)
    assert profile_rules.container_problems({
        "image": "docker.io/minio/minio", "digest": "sha256:" + "a" * 64,
        "command": out.command}) == []


def test_a_command_that_would_not_pass_the_rules_is_refused_not_written():
    """Defensive, because the mount path is validated on its own way in -- but
    refusing beats rendering something the unit-file rules had not agreed to."""
    out = container_command.answer(["server"], takes_args=True,
                                   data_mount="/var/lib/x\nPodmanArgs=--privileged",
                                   code="x")

    assert out.command == ""
    assert out.blocked is True


# --- the capture has two shapes, and only one was tested ----------------------
#
# FOUND ON A REAL MACHINE, 2026-09-07, at the cost of a proof. minio was refused
# a SECOND time for a fault that had already been fixed, because `offered()`
# returned [] against a 12,101-character report with the usage screen plainly in
# it.
#
# configure.py captures a container's log with `podman logs` and falls back to
# `journalctl -u <code>` when the container is already gone -- which, for a
# container that died and was removed, is the usual case, and was the case here.
# `podman logs` emits the program's own output bare. journalctl prefixes every
# line with a timestamp, a host, a unit and a pid:
#
#   minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 minio[26377]: USAGE:
#
# Every match in this module is ANCHORED -- a heading has to BE a heading, which
# is what stops ordinary log output being read as a command list -- so the
# prefix defeated all of them. MINIO_LOG above was written from the FIRST
# failure, which happened to be captured the other way, so the whole file passed
# while the parser could not read the thing it exists to read.
#
# A fixture that does not resemble what the system produces tests the fixture.

JOURNALD_MINIO_LOG = """\
os_family=rhel
technologies=minio
container_minio=failed
declares_minio=9000
listening_inside_minio=none
  minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 minio[26377]: NAME:
  minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 minio[26377]:   minio - High Performance Object Storage
  minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 minio[26377]: USAGE:
  minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 minio[26377]:   minio [FLAGS] COMMAND [ARGS...]
  minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 minio[26377]: COMMANDS:
  minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 minio[26377]:   server  start object storage server
  minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 minio[26377]:
  minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 minio[26377]: FLAGS:
  minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 minio[26377]:   --quiet  disable startup messages
  minio log: Sep 07 01:54:12 proofminio20260907t0145359bd01 systemd[1]: minio.service: Failed with result 'start-limit-hit'.
"""


def test_a_journald_captured_help_screen_is_read_too():
    """THE DEFECT. This is the shape a dead container's log actually arrives in,
    and it is the common case: the container has to die for anyone to look."""
    names, takes_args = container_command.offered(JOURNALD_MINIO_LOG, "minio")

    assert names == ["server"]
    assert takes_args is True


def test_the_command_is_the_same_whichever_way_the_log_was_captured():
    """The two captures are the same log. If they disagree, one of them is
    lying about the machine."""
    bare = container_command.for_report(MINIO_LOG, "minio",
                                        data_mount="/var/lib/minio")
    journal = container_command.for_report(JOURNALD_MINIO_LOG, "minio",
                                           data_mount="/var/lib/minio")

    assert bare.command == journal.command == "server /var/lib/minio"


def test_a_journald_prefix_does_not_turn_log_output_into_a_help_screen():
    """Stripping the prefix must not weaken the anchoring it defeated. Without
    a USAGE line this is still ordinary output, prefix or no prefix."""
    chatty = ("  redis log: Sep 07 01:54:12 host redis[1]: Ready to accept connections\n"
              "  redis log: Sep 07 01:54:12 host redis[1]: COMMANDS:\n"
              "  redis log: Sep 07 01:54:12 host redis[1]:   flushall  executed by admin\n")

    assert container_command.offered(chatty, "redis")[0] == []


def test_the_unit_lines_around_it_are_not_read_as_commands():
    """systemd's own lines carry the same prefix and sit in the same capture."""
    names, _ = container_command.offered(JOURNALD_MINIO_LOG, "minio")

    assert names == ["server"]
    assert not any("service" in n for n in names)

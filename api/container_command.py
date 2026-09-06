"""What a container's own usage screen says it must be TOLD to do (C9b).

THE SIBLING OF container_env, AND THE SAME ARGUMENT. That module exists because
an image can refuse to start until it is given an environment variable, and the
only place that requirement is stated is the log of a machine that tried. This
one exists because an image can refuse to start until it is given a COMMAND, and
the only place THAT is stated is the same log.

`minio/minio` is the case that produced it. Its CMD is the bare binary, and the
binary does nothing without a subcommand, so the container starts, prints its own
help, and exits -- forever, until systemd's start limit stops it. Measured on a
real machine on 2026-09-06, the report read

    container_minio=running
    declares_minio=9000
    listening_inside_minio=none

with the answer sitting in the log underneath it:

    minio log: USAGE:
    minio log:   minio [FLAGS] COMMAND [ARGS...]
    minio log: COMMANDS:
    minio log:   server  start object storage server

No environment variable fixes that, so before this module the technology could
not be offered at all.

WHY THIS IS NOT A TABLE, and not a guess either. Nothing here knows the word
"minio". The command is read from the image's own help text, and it is taken
ONLY when that text names exactly one command -- an image offering a choice is
an image where somebody has to make it, and this portal does not. The argument,
when the usage line says the command takes one, is the data directory THE RECIPE
ALREADY MOUNTS: not a path invented here, but the one path the portal knows the
container has.

AND IT IS STILL ONLY A DRAFT. Nothing is certified because this module proposed
it; a machine builds it and either reports the ports listening or does not. A
wrong command costs one proof and says so in the next report, which is the whole
point of having a ladder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from common import profile_rules

#: A command name in a usage screen's COMMANDS block: `server`, `run`, `start`.
#: Lowercase, because that is what a subcommand is; bounded, because it reaches
#: a unit file.
_COMMAND = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

#: The heading that introduces the list. Matched on its own line, exactly.
_HEADING = re.compile(r"^COMMANDS:?$", re.IGNORECASE)

#: Any other section heading — FLAGS:, OPTIONS:, VERSION: — which ends the list.
_ANOTHER_HEADING = re.compile(r"^[A-Z][A-Z ]{2,29}:$")

#: The usage line itself. Its presence is what makes this a help screen rather
#: than an application log that happens to contain the word COMMANDS.
_USAGE = re.compile(r"^USAGE:?$", re.IGNORECASE)

#: `minio [FLAGS] COMMAND [ARGS...]` — the bracketed ARGS is the image saying the
#: command takes a positional argument. Without it, none is passed.
_TAKES_ARGS = re.compile(r"\bARGS?\b")

#: How far past the heading to keep reading. A help screen's command list is
#: short; a runaway log is not a help screen.
_MAX_ENTRIES = 24


@dataclass
class Command:
    """What may be given to a container, and what stops it."""

    #: Ready to go into the recipe's `container.command`.
    command: str = ""
    #: Every command the usage screen offered, in the order it named them.
    offered: list[str] = field(default_factory=list)
    #: Set when a person must choose, because the image offered more than one.
    needs_a_person: list[str] = field(default_factory=list)
    #: One sentence for a requester, an approver, or an audit entry.
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.needs_a_person)

    @property
    def answerable(self) -> bool:
        return bool(self.command) and not self.blocked


def offered(report: str, code: str) -> tuple[list[str], bool]:
    """The commands this technology's container listed, and whether it takes args.

    Only that container's lines are read, anchored the way `container_env` anchors
    them: a report carries every technology on the machine, and one container's
    help text is not evidence about another.
    """
    marker = f"{profile_rules.report_key(code)} log:"
    said: list[str] = []
    for line in (report or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(marker):
            said.append(stripped[len(marker):].strip())

    saw_usage = any(_USAGE.match(t) for t in said)
    takes_args = any(_TAKES_ARGS.search(t) for t in said
                     if "COMMAND" in t.upper() and not _HEADING.match(t))
    if not saw_usage:
        # NOT A HELP SCREEN. Plenty of software prints the word "commands"; only
        # a usage screen prints USAGE, and reading a command out of ordinary log
        # output would hand root an argument nobody offered.
        return [], False

    names: list[str] = []
    reading = False
    for text in said:
        if _HEADING.match(text):
            reading = True
            continue
        if not reading:
            continue
        if not text or _ANOTHER_HEADING.match(text):
            break
        first = text.split()[0] if text.split() else ""
        if _COMMAND.match(first) and first not in names:
            names.append(first)
        if len(names) >= _MAX_ENTRIES:
            break
    return names, takes_args


def answer(names, *, takes_args: bool = False, data_mount: str = "",
           code: str = "") -> Command:
    """What this portal may tell a container to do.

    ONE COMMAND ONLY. An image that offers `server` and nothing else has stated
    the only way it can be run, and running it is what the proof is for. An image
    offering several has a decision in it, and a decision is a person's.
    """
    names = [n for n in (names or []) if _COMMAND.match(n)]
    out = Command(offered=list(names))
    subject = code or "this container"
    if not names:
        return out

    if len(names) > 1:
        out.needs_a_person = list(names)
        out.detail = (
            f"{subject} will not start without a command, and its own help "
            f"offers {', '.join(names)}. Choosing between them is not this "
            f"portal's to do — an operator names the one this service should run "
            f"and it can be proved. Nothing was built.")
        return out

    chosen = names[0]
    argument = data_mount if (takes_args and data_mount) else ""
    candidate = f"{chosen} {argument}".strip()
    if not profile_rules.CONTAINER_COMMAND.match(candidate):
        # The mount path is validated on its own way in, so this should not
        # happen — and if it ever does, refusing beats rendering something the
        # unit-file rules had not agreed to.
        out.needs_a_person = [chosen]
        out.detail = (f"{subject} asks to be run as {candidate!r}, which this "
                      f"portal will not write into a unit file.")
        return out

    out.command = candidate
    out.detail = (
        f"{subject} printed its own usage screen and exited: its image runs a "
        f"binary that does nothing without a command, and the only one it offers "
        f"is `{chosen}`. It will be built again as `{candidate}`"
        + (f", where {argument} is the data directory this recipe already mounts"
           if argument else "")
        + ". A machine decides whether that was right.")
    return out


def for_report(report: str, code: str, *, data_mount: str = "") -> Command:
    """`answer` for what one machine's report says. The two steps, one call."""
    names, takes_args = offered(report, code)
    return answer(names, takes_args=takes_args, data_mount=data_mount, code=code)

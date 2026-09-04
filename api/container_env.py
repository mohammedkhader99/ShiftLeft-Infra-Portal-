"""What a container's own log says it needs before it will start (C9).

THE LADDER REACHES AN IMAGE AND THE IMAGE REFUSES TO RUN. `docker.io/library/
mysql` exits immediately unless it is told how to set its root password;
`postgres` wants POSTGRES_PASSWORD; `mssql/server` wants a password and a
licence acceptance. The container rung is the last one, so a technology that
stops here goes to manual fulfilment -- and the reason is one environment
variable the machine already printed in its own log.

WHERE THE ANSWER COMES FROM, AND WHY IT IS NOT A TABLE. The image's own config
does not declare these: measured on 2026-09-04, `library/mysql` declares
MYSQL_MAJOR, MYSQL_VERSION, MYSQL_SHELL_VERSION and nothing about a password.
The container SAYS what it needs, in the log the boot report already captures
when a service never comes up:

    mysql log: [Entrypoint]: Database is uninitialized and password option is
    mysql log:   not specified
    mysql log:   You need to specify one of the following as an environment variable:
    mysql log:   - MYSQL_ROOT_PASSWORD
    mysql log:   - MYSQL_ALLOW_EMPTY_PASSWORD
    mysql log:   - MYSQL_RANDOM_ROOT_PASSWORD

So this reads the machine's report, exactly as `discovery` reads it for
packages, and nothing here knows the word "mysql".

WHAT MAY BE ANSWERED AUTOMATICALLY, decided by the user on 2026-09-04, and the
whole safety argument of this module:

  * A RANDOM PASSWORD IS NOT A SECRET THIS PORTAL HOLDS. `*_RANDOM_ROOT_PASSWORD`
    tells the image to generate one on the machine and print it once. Nothing is
    chosen here, written to a recipe, stored in the database, committed, or
    logged anywhere this portal can read -- which is what CLAUDE.md's rule about
    secrets actually protects. The machine's administrator reads it with
    `podman logs <code>` and changes it.
  * AN EMPTY PASSWORD IS NEVER CHOSEN. `*_ALLOW_EMPTY_PASSWORD` also starts the
    container, and would put an unauthenticated database on a real network. It
    is refused even when it is the only option offered.
  * A NAMED PASSWORD IS A SECRET AND STOPS THE LADDER. POSTGRES_PASSWORD has no
    random alternative: there is nothing safe to choose, so the recipe is not
    drafted and the refusal NAMES what a person must supply and where
    (`secrets/container.env`, the operator channel that already exists).
    Inventing a password here would mean this portal generated a credential,
    put it in a recipe an approver reads, and stored it.
  * A LICENCE IS A CONTRACT AND IS NOT THE AGENT'S TO SIGN. ACCEPT_EULA comes
    from the Admin console setting an operator turned on, and from nowhere else
    -- `ai_blueprint.licence_accepted`, unchanged by this module.

A REFUSAL EXPLAINS AND GUIDES; it never merely says no.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from common import profile_rules

#: A shouted, underscored name in a log line: MYSQL_ROOT_PASSWORD, ACCEPT_EULA.
#:
#: An underscore is required. Without it every ERROR, WARNING, FATAL, INFO and
#: UTC in a stack trace reads as a demanded variable, and the drafter would
#: refuse a container for needing "ERROR".
_NAME = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,6})\b")

#: Shouted words that appear in logs and are not variables. Small on purpose:
#: the underscore rule does nearly all of the work, and a list that tries to
#: enumerate log vocabulary is a table by another name.
_NOT_A_VARIABLE = frozenset({
    "GENERATED_ROOT_PASSWORD",     # what MySQL prints AFTER it has one
    "NOT_SPECIFIED", "NOT_SET", "NOT_INSTALLED",
})

#: The image generates the password itself and prints it once. Chosen when
#: offered.
_RANDOM = re.compile(r"RANDOM.*PASSWORD$")
#: Starts the container with no password at all. Never chosen.
_EMPTY = re.compile(r"ALLOW_EMPTY")
#: A value only a person may supply.
_SECRET = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIALS?|_KEY|APIKEY)$")
#: A contract only a person may accept.
_LICENCE = re.compile(r"(EULA|LICEN[CS]E)")

#: What a random-password variable is set to. The images that offer one accept
#: any non-empty value; `yes` is what their documentation uses.
YES = "yes"


@dataclass
class Answer:
    """What can be supplied for a container, and what stops it."""

    #: Ready to go into the recipe's `container.environment`.
    environment: dict[str, str] = field(default_factory=dict)
    #: Names a person must supply before this technology can be built at all.
    needs_a_person: list[str] = field(default_factory=list)
    #: Every variable the container asked for, in the order it named them.
    demanded: list[str] = field(default_factory=list)
    #: One sentence for a requester, an approver, or an audit entry.
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.needs_a_person)

    @property
    def answerable(self) -> bool:
        return bool(self.environment) and not self.blocked


def demanded(report: str, code: str) -> list[str]:
    """Environment variable names this technology's container named in its log.

    Only that container's lines are read. A report carries every technology on
    the machine, and MongoDB's log is not evidence about MySQL.
    """
    marker = f"{profile_rules.report_key(code)} log:"
    names: list[str] = []
    for line in (report or "").splitlines():
        # ANCHORED, not `in`. configure.py writes each captured line as
        # `  <key> log: ...`, and a substring test would let a technology called
        # `sql` read `mysql`'s log -- answering one container from another's
        # requirements, which is worse than answering neither.
        stripped = line.lstrip()
        if not stripped.startswith(marker):
            continue
        for name in _NAME.findall(stripped[len(marker):]):
            if name not in _NOT_A_VARIABLE and name not in names:
                names.append(name)
    return names


def answer(names, *, licence_accepted: bool = False, code: str = "") -> Answer:
    """What this portal may supply for the variables a container demanded.

    `licence_accepted` is the Admin console setting, passed in rather than read
    here so this module has one job and the caller keeps the one that involves
    a contract.
    """
    names = [n for n in (names or []) if profile_rules.ENV_KEY.match(n)]
    out = Answer(demanded=list(names))
    if not names:
        return out

    random_ones = [n for n in names if _RANDOM.search(n)]
    empty_ones = [n for n in names if _EMPTY.search(n)]
    licence_ones = [n for n in names if _LICENCE.search(n)]
    # A secret that is not one of the two alternatives above.
    secret_ones = [n for n in names
                   if _SECRET.search(n) and n not in random_ones and n not in empty_ones]

    if random_ones:
        # ONE, not all of them: the log offers alternatives ("one of the
        # following"), and setting two ways of choosing a password is how an
        # image refuses to start for a new reason.
        chosen = sorted(random_ones)[0]
        out.environment[chosen] = YES
        secret_ones = []          # answered by the random one
    for name in licence_ones:
        if licence_accepted:
            out.environment[name] = "Y"
        else:
            out.needs_a_person.append(name)
    out.needs_a_person.extend(secret_ones)

    subject = code or "this container"
    if out.needs_a_person:
        wants = ", ".join(sorted(set(out.needs_a_person)))
        out.detail = (
            f"{subject} will not start until {wants} is supplied, and this "
            f"portal must not choose it: a password is a secret and a licence "
            f"acceptance is a contract. An operator adds it to "
            f"secrets/container.env as `{(code or '<technology>')}.<NAME>=<value>` "
            f"— a file only a person writes, which never reaches a recipe, the "
            f"database, an approver's screen or git — or accepts vendor licence "
            f"terms in the Admin console. Nothing was built.")
    elif out.environment:
        how = ", ".join(f"{k}={v}" for k, v in sorted(out.environment.items()))
        out.detail = (
            f"{subject} asked for {', '.join(out.demanded)} and was given "
            f"{how}. The machine generates its own password and prints it once "
            f"in its container log; this portal never sees it, stores it, or "
            f"writes it down. Read it on the machine with `podman logs "
            f"{code or '<technology>'}` and change it.")
    elif empty_ones and not random_ones:
        out.needs_a_person = sorted(set(names) - set(empty_ones)) or list(empty_ones)
        out.detail = (
            f"{subject} offers only {', '.join(empty_ones)} — a database with "
            f"no password on a real network. This portal will not choose that. "
            f"An operator supplies a password in secrets/container.env instead.")
    return out


def for_report(report: str, code: str, *, licence_accepted: bool = False) -> Answer:
    """`answer` for what one machine's report says. The two steps, one call."""
    return answer(demanded(report, code), licence_accepted=licence_accepted,
                  code=code)

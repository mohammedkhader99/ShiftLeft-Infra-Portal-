"""What artefact does this technology actually publish? (D1)

The defect this replaces was stated in the code it replaces:

    ORDER IS COST, NOT CONFIDENCE.

`install_methods` ordered the ladder by how cheap each attempt was to make, and
gated every rung but the first on a hand-written dictionary:

    methods = ["package"]
    if code in VENDOR_REPOS:      methods.append("repo")
    if code in ARCHIVE_KNOWLEDGE: methods.append("archive")

So a technology in neither dictionary had a ONE-RUNG ladder: guess the package
name. Every failure of 25-26 August lives in that sentence — a guessed package
that was a source RPM, a rotted repository URL, an invented daemon, an invented
binary. Meanwhile the container rung, which requires us to guess nothing at all,
was reachable only AFTER a machine had been spent and discovery came back empty.

ORDER IS NOW CONFIDENCE, measured as how much WE have to guess:

    repo       a person curated it, and C10 re-checks the URL before a machine
    container  the publisher declares the image, its digest and its ports
    package    SEARCHED in repository metadata we already downloaded — not
               guessed, which is the whole difference
    archive    a person curated it, but it fetches and executes a file as root,
               so it stays last however well curated

A dictionary entry is an OVERRIDE, never a prerequisite. That is the reviewer's
standing rule — a fix must catch the next contender, not the one in front of us
— applied to the ladder itself.

NOTHING HERE PROMISES ANYTHING. Every source is asked live and may answer "I
could not tell", and every rung is still proved on a real machine before it is
certified. This decides what to TRY, and in what order; it never decides what
works.
"""

from __future__ import annotations

import os

#: The order, best first. Named here rather than inline so the reasoning above
#: and the behaviour below cannot drift apart.
CONFIDENCE = ("repo", "container", "package", "archive")


def enabled() -> bool:
    """Confidence-first resolution, on unless someone turns it off.

    A switch because this reverses the ladder for every technology at once, and
    an increment that cannot be reverted from the Admin console is an increment
    that has to be reverted by a release.
    """
    # OFF BY DEFAULT, and the default lives HERE rather than only in compose and
    # the Admin console. Those describe a deployment; this describes the code,
    # and a test process picks up neither — which is how a switch that reads
    # "shipped off" everywhere else was on for the whole suite.
    return (os.getenv("RESOLVE_BY_CONFIDENCE", "false").strip().lower()
            in ("1", "true", "yes", "on"))


def package_name(code: str, family: str = "rhel", *, search=None) -> str:
    """The best installable package name for this technology, or "".

    SEARCHED, NOT GUESSED. `_guessed_profile` used the catalogue code verbatim —
    `dotnet8`, which is not a package — and spent a machine to be told so. The
    repository metadata that answers this is already downloaded for C10, so
    asking it costs nothing and removes the guess entirely.

    Returns "" when nothing matches, which is an ordinary answer: plenty of
    software is not packaged for Oracle Linux at all, and saying so is what
    lets the container rung take over instead.
    """
    if search is None:
        from api import repo_facts
        search = repo_facts.search_packages
    try:
        found = search(code, family)
    except Exception:  # noqa: BLE001 - a source that cannot answer is not a failure
        return ""
    return found[0] if found else ""


def image_for(code: str, *, find_image=None) -> dict | None:
    """The container image this technology publishes, or None.

    `find_image` is injected for the same reason every other cloud call in this
    codebase is: the interesting branches are the ones a real registry will not
    produce on demand.
    """
    if find_image is None:
        from api import registry
        find_image = registry.find
    try:
        return find_image(code)
    except Exception:  # noqa: BLE001
        return None


def methods_for(code: str, *, family: str = "rhel", curated_repo: bool = False,
                curated_archive: bool = False, search=None,
                find_image=None) -> list[str]:
    """Every way this technology could be installed, MOST CONFIDENT FIRST.

    The two `curated_*` flags are passed in rather than read here so this module
    never needs to know what is in anyone's dictionary — they are overrides on a
    resolution that works without them.
    """
    if not enabled():
        # The old order, exactly: cost first, dictionary-gated. Kept reachable
        # so the switch is a real revert and not a different code path pretending.
        methods = ["package"]
        if curated_repo:
            methods.append("repo")
        if curated_archive:
            methods.append("archive")
        return methods

    available = {
        "repo": curated_repo,
        "container": image_for(code, find_image=find_image) is not None,
        "package": bool(package_name(code, family, search=search)),
        "archive": curated_archive,
    }
    return [m for m in CONFIDENCE if available[m]]

"""Ask the repository before spending a machine (C10).

Two failures on 2026-08-25, five machines between them, nothing provisioned:

    dotnet8.0     four machines, four requests. dnf: "Unable to find a match".
                  The wildcard search had SEEN the name in ol9_appstream, and a
                  name a search lists is not a name dnf will install.
    mongodb       one machine. The recipe's vendor repository URL,
                  `.../yum/redhat/mongodb-org-7.0.repo`, is a 404. MongoDB's
                  repository is perfectly healthy; the URL in our dictionary was
                  the wrong shape and had rotted unnoticed.

Both were knowable in under a second, from here, without booting anything.

WHAT THIS MODULE MAY AND MAY NOT CONCLUDE — the design, not a caveat:

    a 404 or a dead name          IS conclusive. The URL does not exist
                                  anywhere; the package is not in the metadata
                                  the repository itself publishes.
    a 200, or a name we found     IS NOT a promise. We are not the machine. The
                                  build subnet has different egress, different
                                  DNS and different proxies, and a repository we
                                  can reach may be unreachable from there.

So this refuses, and never approves. Everything it cannot settle comes back
UNKNOWN, and UNKNOWN changes nothing: the ladder proceeds exactly as it did
before this file existed. A check that becomes a new way for provisioning to
fail has cost more than it saved.

It is also generic on purpose. The reviewer's standing instruction after the
Vault episode was to stop adding per-technology rows and make rules that catch
the next contender — so nothing here knows what dotnet or mongodb are.
"""

from __future__ import annotations

import gzip
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEAD = "dead"
ALIVE = "alive"
UNKNOWN = "unknown"

#: Seconds to wait on any single repository request. Short: this runs on the
#: path of a request somebody is waiting for, and a slow answer is UNKNOWN,
#: which costs nothing.
TIMEOUT = 20

#: How large a `primary.xml.gz` we will download to learn what a repository
#: offers. OL9's AppStream is 8 MB and worth it; its BaseOS is 130 MB and is
#: not, so that one falls through to the directory index below (3.7 MB).
PRIMARY_SIZE_CAP = 25 * 1024 * 1024

#: How long a repository's package list stays cached. Repositories change on
#: the order of days; this check runs on the order of minutes.
CACHE_SECONDS = 3600

#: THE BASE IMAGE'S OWN REPOSITORIES. One fact about the machine we boot, not a
#: row per technology — a recipe that names no repository is installing from
#: whatever the base image already has, and this is what that is.
BASE_REPOS = {
    "rhel": (
        "https://yum.oracle.com/repo/OracleLinux/OL9/appstream/x86_64/",
        "https://yum.oracle.com/repo/OracleLinux/OL9/baseos/latest/x86_64/",
    ),
}

#: A package's name TOGETHER WITH ITS ARCHITECTURE, because the architecture is
#: the whole point. `dotnet8.0` is in OL9's AppStream metadata — as `arch=src`,
#: a SOURCE package. `repoquery` lists it; `dnf install` cannot install it. That
#: one distinction is the entire .NET 8 story: four machines across four
#: requests, each told "Unable to find a match" for a name the search had just
#: shown it. In primary.xml `<name>` is always followed by `<arch>`.
_PKG = re.compile(
    rb"<name>([A-Za-z0-9][A-Za-z0-9._+-]*)</name>\s*<arch>([a-z0-9_]+)</arch>")

#: Oracle's directory listing links `getPackage/<file>.rpm`, so the filename is
#: not flush against the quote. Requiring it to be found nothing at all, which
#: read as "this repository publishes no packages".
#:
#: The whole FILENAME is captured and taken apart below rather than pattern-
#: matched in one go: a package name may itself contain dashes, so a regex that
#: stops at the first `-<digit>` reads `sqlite-libs-3.34.1` as the name.
_RPM = re.compile(rb'href="(?:[^"]*/)?([A-Za-z0-9][^"/]*\.rpm)"')

#: Architectures that cannot be installed. A source package is a recipe for
#: building software, not the software.
_NOT_INSTALLABLE = (b"src", b"nosrc")

_cache: dict[str, tuple[float, frozenset[str] | None]] = {}


@dataclass
class Offering:
    """What a repository says about the names a recipe asked for."""

    checked: tuple[str, ...] = ()          # repositories we actually read
    unchecked: tuple[str, ...] = ()        # repositories we could not read
    present: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    near: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def conclusive(self) -> bool:
        """Only when every repository the machine will have was readable.

        A name absent from the repositories we DID read, while one we could not
        read might carry it, is a suspicion. Refusing on a suspicion would make
        this check the thing that breaks provisioning.
        """
        return bool(self.checked) and not self.unchecked


def _get(url: str, *, fetch=None) -> bytes:
    if fetch is not None:
        return fetch(url)
    req = urllib.request.Request(url, headers={"User-Agent": "shiftleft-portal"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - repo URLs
        return r.read()


def url_verdict(url: str, *, fetch=None) -> str:
    """DEAD only when the server says the thing is not there.

    A timeout, a TLS complaint, a proxy, a 500 — none of those mean the URL is
    wrong, and treating them as wrong would refuse recipes on a bad afternoon.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return DEAD
    try:
        _get(url, fetch=fetch)
        return ALIVE
    except urllib.error.HTTPError as exc:
        return DEAD if exc.code in (404, 410) else UNKNOWN
    except Exception:  # noqa: BLE001 - anything else is "we could not tell"
        return UNKNOWN


def package_names(baseurl: str, *, fetch=None, now=None) -> frozenset[str] | None:
    """Every package name a repository publishes, or None if we could not read it.

    Standard repodata first, because every yum repository has it and a vendor's
    is usually tiny. The directory index is the fallback for the one case that
    defeats repodata — a repository whose accumulated metadata is larger than
    the answer is worth.
    """
    base = baseurl.rstrip("/") + "/"
    now = now if now is not None else time.time()
    hit = _cache.get(base)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]

    names = _from_repodata(base, fetch=fetch)
    if names is None:
        names = _from_index(base, fetch=fetch)
    _cache[base] = (now, names)
    return names


def _from_repodata(base: str, *, fetch=None) -> frozenset[str] | None:
    try:
        xml = _get(base + "repodata/repomd.xml", fetch=fetch).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None

    block = re.search(r'<data type="primary">(.*?)</data>', xml, re.S)
    if not block:
        return None
    href = re.search(r'<location href="([^"]+)"', block.group(1))
    size = re.search(r"<size>(\d+)</size>", block.group(1))
    if not href:
        return None
    if size and int(size.group(1)) > PRIMARY_SIZE_CAP:
        return None                      # too big to be worth it; try the index

    try:
        raw = _get(base + href.group(1).lstrip("/"), fetch=fetch)
        body = gzip.decompress(raw) if href.group(1).endswith(".gz") else raw
    except Exception:  # noqa: BLE001
        return None

    # Regex rather than an XML parse: 8 MB compressed is ~80 MB of XML, we want
    # two elements, and `<name>` is the package name in primary.xml (the other
    # names in that document are ATTRIBUTES, `<rpm:entry name="...">`).
    #
    # SOURCE PACKAGES ARE DROPPED HERE. Keeping them is not a harmless surplus:
    # it is precisely the mistake that let `dotnet8.0` look installable.
    found = {m.group(1).decode() for m in _PKG.finditer(body)
             if m.group(2) not in _NOT_INSTALLABLE}
    return frozenset(found) or None


def _from_index(base: str, *, fetch=None) -> frozenset[str] | None:
    """Scrape a directory listing. Oracle publishes one; not everyone does."""
    try:
        body = _get(base + "index.html", fetch=fetch)
    except Exception:  # noqa: BLE001
        return None
    found = set()
    for m in _RPM.finditer(body):
        stem = m.group(1).decode()
        if stem.endswith(".src.rpm") or stem.endswith(".nosrc.rpm"):
            continue                       # a source package is not installable
        # <name>-<version>-<release>.<arch>.rpm, and <name> may contain dashes.
        stem = stem[: -len(".rpm")].rsplit(".", 1)[0]      # drop .rpm, drop arch
        name = stem.rsplit("-", 2)[0]                      # drop release, version
        if name:
            found.add(name)
    return frozenset(found) or None


def _near(name: str, offered, code: str = "") -> tuple[str, ...]:
    """Names a person — or the ladder — would recognise as what was meant.

    BEST FIRST, ranked by `discovery.rank_matches`, which exists for exactly
    this question and whose docstring names this exact case: REQ-2026-0197 asked
    for .NET 8, and `dotnet-sdk-8.0` is the answer no name-shape rule would ever
    generate. Writing a second ranker here would be the two-functions-one-
    question mistake this project has paid for repeatedly.

    Ranked on the CATALOGUE CODE (`dotnet8`), not the failing package name:
    rank_matches splits a trailing version off the code, and `dotnet8.0` splits
    to a stem of `dotnet8.` and a version of `0`, which ranks nothing usefully.
    """
    from api import discovery

    stem = re.sub(r"[^a-z]", "", name.lower())[:12]
    if len(stem) < 3:
        return ()
    hits = [o for o in offered
            if re.sub(r"[^a-z]", "", o.lower()).startswith(stem[:6])]
    ranked = discovery.rank_matches([(h, "") for h in hits], code or name)
    return tuple(h for h, _repo in ranked[:4])


#: How long the whole check may take on the path of a request somebody is
#: waiting for. A cold read of OL9's AppStream and BaseOS is about two minutes;
#: an hour later it is instant. Rather than make the first requester of the hour
#: pay that, the check gives up and returns UNKNOWN — which changes nothing —
#: and `warm()` fills the cache off the request path.
BUDGET_SECONDS = 25


def warm(family: str = "rhel", *, fetch=None) -> list[str]:
    """Read the base repositories now, so a request never waits for them.

    Called from the poller's own sweep. Returns the repositories it managed to
    read, and never raises: a warm that fails leaves the check exactly as slow
    as it would have been anyway.
    """
    done = []
    for base in BASE_REPOS.get(family, ()):
        try:
            if package_names(base, fetch=fetch) is not None:
                done.append(base)
        except Exception:  # noqa: BLE001 - a warm is never load-bearing
            continue
    return done


def offers(baseurls, names, *, fetch=None, budget=None, code="") -> Offering:
    """Which of `names` the given repositories actually publish."""
    wanted = [n for n in dict.fromkeys(names) if n]
    checked, unchecked, offered = [], [], set()
    deadline = None if budget is None else time.monotonic() + budget
    for base in baseurls:
        if deadline is not None and time.monotonic() > deadline:
            # OUT OF TIME IS NOT AN ANSWER. Recorded as unchecked so the result
            # cannot be conclusive, so nothing is refused on a partial read.
            unchecked.append(base)
            continue
        got = package_names(base, fetch=fetch)
        if got is None:
            unchecked.append(base)
        else:
            checked.append(base)
            offered |= got

    present = tuple(n for n in wanted if n in offered)
    missing = tuple(n for n in wanted if n not in offered)
    return Offering(
        checked=tuple(checked), unchecked=tuple(unchecked),
        present=present, missing=missing,
        near={n: _near(n, offered, code) for n in missing} if checked else {})


@dataclass
class Refusal:
    """Why a recipe cannot work, in words a requester can act on.

    `suggestions` carries the same answer STRUCTURALLY. Prose is for people; the
    ladder needs the names in a form it can act on, and a refusal that only
    explains itself to a human is a refusal that ends in manual fulfilment.
    """

    reason: str
    detail: str = ""
    suggestions: dict = field(default_factory=dict)   # missing name -> better names

    def __str__(self) -> str:
        return f"{self.reason} {self.detail}".strip()


def _packages_of(recipe: dict, family: str) -> list[str]:
    block = recipe.get(family) or {}
    return [str(p) for p in (block.get("packages") or []) if p]


def repos_for(recipe: dict, family: str) -> list[str]:
    """Every repository the machine will be able to install from.

    The base image's own, plus the vendor repository the recipe adds — but only
    when that is a BASEURL. A `.repo` file is a pointer to a repository, not a
    repository, and asking it for `repodata/repomd.xml` would 404 and be read as
    "this vendor offers nothing".
    """
    bases = list(BASE_REPOS.get(family, ()))
    url = ((recipe.get("repo") or {}).get("url") or "").strip()
    if url and not url.lower().endswith(".repo"):
        bases.append(url)
    return bases


def check_recipe(recipe: dict, *, family: str = "rhel", fetch=None,
                 budget: float | None = BUDGET_SECONDS) -> Refusal | None:
    """What the repositories say about this recipe, before a machine is booted.

    None means "nothing conclusive against it" — which is NOT approval. It is
    the same answer this function gave before it existed.
    """
    if not isinstance(recipe, dict):
        return None

    repo = recipe.get("repo") or {}
    for field_name, label in (("url", "repository"), ("gpg_key", "signing key")):
        url = (repo.get(field_name) or "").strip()
        if url and url_verdict(url, fetch=fetch) == DEAD:
            return Refusal(
                f"The {label} this recipe adds does not exist.",
                f"{url} returns 404. The vendor may still publish this software — "
                f"a repository URL rots without anything noticing — but a machine "
                f"would add nothing and then fail to find the package. Nothing was "
                f"built.")

    wanted = _packages_of(recipe, family)
    if not wanted:
        return None

    found = offers(repos_for(recipe, family), wanted, fetch=fetch,
                   budget=budget, code=str(recipe.get("code") or ""))
    if not found.missing or not found.conclusive:
        return None

    lines = []
    for name in found.missing:
        near = found.near.get(name) or ()
        lines.append(f"{name} is not published by any repository this machine "
                     f"would have"
                     + (f" — did you mean {', '.join(near)}?" if near else "."))
    return Refusal(
        "This recipe names a package that does not exist.", " ".join(lines),
        suggestions={n: found.near.get(n, ()) for n in found.missing
                     if found.near.get(n)})

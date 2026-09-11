"""Ask a container registry what an image is, and pin it (C8).

REQ-2026-0188 to 0190 spent three real machines establishing that RabbitMQ is
not installable from Oracle Linux 9's repositories or from EPEL 9. Repology
confirms it independently — packaged for EPEL 6 and 7 and for every recent
Fedora, and for no EPEL 9 at all — and RabbitMQ's own documentation says to add
their repository instead. Meanwhile their official image has been pulled nearly
four billion times.

So when a SOUND search of the repositories comes back with nothing, the honest
next question is not "which vendor repository should I hard-code this time" but
"does this software publish an image", and that is a question a registry answers
for any technology without anyone here writing anything down.

THREE FACTS ARE TAKEN FROM THE REGISTRY, AND ALL THREE ARE MEASURED:

  * the image path, by trying the conventional shapes and seeing which exists;
  * the DIGEST the tag currently resolves to, which is what gets pinned — a tag
    can be repointed by its publisher after a proof passed, and then what was
    certified and what a later request installs are different things wearing the
    same name;
  * the ports the image itself declares. Not a guess and not a hard-coded row:
    the publisher's own statement of the interface, in the image config.

WHAT COMES BACK IS UNTRUSTED. It is JSON from a public host, and the image path
inside it is destined for a `podman pull` run by root. Every value is validated
against common/profile_rules before it leaves this module — the same allow-lists
that govern a drafted profile, because it is going to exactly the same place.

NO CREDENTIALS. Anonymous pull scope only, on public images. This module must
never grow a login: a registry that needs one is a registry whose images this
portal has no business pulling unattended.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from common import profile_rules

# Where an official image is conventionally found, in the order worth trying.
# `library/<code>` is Docker Hub's namespace for its official images; the
# vendor-named forms cover projects that publish under their own organisation
# (Keycloak is quay.io/keycloak/keycloak, not docker.io/library/keycloak).
#
# A SHAPE, NOT A TABLE. Adding a technology must never mean adding a row here.
_SHAPES = (
    "docker.io/library/{name}",
    "docker.io/{name}/{name}",
    "quay.io/{name}/{name}",
)

#: Where a vendor publishes an image that NO RULE COULD DERIVE.
#:
#: `_SHAPES` above says "a shape, not a table", and that is right: a technology
#: whose image can be guessed must never need a row anywhere. This is for the
#: case where guessing is not merely unlucky but impossible, and Oracle is the
#: clean example. Three independent things defeat discovery there, each verified
#: against the live registry:
#:
#:   the shapes      produce docker.io/library/oracle-db and quay.io/... ,
#:                   none of which exist
#:   the catalogue   container-registry.oracle.com/v2/_catalog answers 401, and
#:                   Oracle will not issue a catalog-scope token anonymously, so
#:                   the first-party sweep that found SQL Server learns nothing
#:   the match rule  `_first_party_match("database/free", "oracle-db")` is False.
#:                   Microsoft groups by PRODUCT (`mssql/server`); Oracle groups
#:                   by FAMILY (`database/free`), so the first path segment is
#:                   `database` and matches no sensible catalogue code
#:
#: There is precedent and it is deliberate: VENDOR_REPOS and ARCHIVE_KNOWLEDGE
#: are curated for the same reason, and the refusal ladder reports "none curated
#: for it" as a first-class answer rather than hiding the gap.
#:
#: CONSULTED LAST, NEVER FIRST. Discovery runs unchanged and wins; this is only
#: reached when the shapes and the vendor catalogues have all found nothing. A
#: curated entry that could override discovery would rot silently the day a
#: vendor moved an image, and nothing would notice.
#:
#: An entry here is a claim that a HUMAN checked where a vendor publishes. It is
#: not a licence to skip the proof: the image is still pinned by digest, still
#: built on a real machine, and still has to serve a port before anything is
#: certified.
PUBLISHED_IMAGES: dict[str, str] = {
    # Oracle's free editions pull anonymously; `database/enterprise` answers 401
    # without a credential, which is the licence boundary and not a bug.
    "oracle-free": "container-registry.oracle.com/database/free",
    "oracle-xe": "container-registry.oracle.com/database/express",
    "oracle-db": "container-registry.oracle.com/database/enterprise",
}


_MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))

_AGENT = "emaratech-infra-portal/1.0"


class RegistryOffline(RuntimeError):
    """Raised in place of a network call when REGISTRY_MODE is not `live`.

    Callers already treat an unreachable registry and an absent image as the
    same answer — "no" — so this needs no new handling anywhere. It exists so
    that the reason is READABLE when somebody goes looking, instead of a
    connection timeout that looks like a network fault.
    """


def mode() -> str:
    """`live` asks a real registry; anything else asks nothing at all.

    EVERY OTHER ADAPTER IN THIS PORTAL HAS ONE OF THESE and the test suite pins
    them all to mock in conftest.py — auth, Jira, both pricing adapters,
    provisioning, cloud state, roles. The registry never got one, so the suite
    has always reached the public internet without anything saying so.

    It showed up as a timing mystery rather than a failure.
    `test_asking_creates_nothing` makes four lookups and took 68.9 seconds of an
    18-minute run; the run before it took 1h44m and the one before that 9
    minutes, with an identical pass count each time. Nothing about the tests
    changed between them. What changed was the network.

    The convention this restores is written down in test_vault_live.py: "These
    tests mock the HTTP layer so the suite stays offline; the real proof is a
    separate, documented verification."

    LIVE BY DEFAULT, because production must ask a real registry — the whole
    point of the module is that nobody hard-codes what an image is.
    """
    return (os.getenv("REGISTRY_MODE", "live") or "live").strip().lower()


def timeout_seconds() -> float:
    try:
        return max(2.0, float(os.getenv("REGISTRY_TIMEOUT_SECONDS", "8")))
    except ValueError:
        return 8.0


def candidates(code: str) -> list[str]:
    """Fully-qualified image references worth asking about, best first.

    The same name-shape reasoning `configure.candidate_packages` uses for
    packages: the catalogue code, and the code with a trailing version label
    stripped, because `redis7` is a catalogue name and `redis` is what the
    publisher calls the image.
    """
    code = (code or "").strip().lower()
    if not profile_rules.CODE.match(code):
        return []
    names = [code]
    bare = code.rstrip("0123456789")
    if bare and bare != code:
        names.append(bare)

    out: list[str] = []
    for name in names:
        for shape in _SHAPES:
            ref = shape.format(name=name)
            registry, _, path = ref.partition("/")
            if (registry in profile_rules.REGISTRIES
                    and profile_rules.IMAGE_PATH.match(path) and ref not in out):
                out.append(ref)
    return out


def _get(url: str, headers: dict, want_headers: bool = False):
    if mode() != "live":
        raise RegistryOffline(f"REGISTRY_MODE={mode()}: not asking {url}")
    request = urllib.request.Request(url, headers={"User-Agent": _AGENT, **headers})
    with urllib.request.urlopen(request, timeout=timeout_seconds()) as response:
        if want_headers:
            # LOWERCASED KEYS. `response.headers` is case-insensitive, and
            # `dict()` of it is not — it keeps whatever case the server sent.
            # Docker Hub sends `docker-content-digest` and Quay sends
            # `Docker-Content-Digest`, so a plain dict lookup found the digest
            # on one registry and silently missed it on the other. Keycloak
            # resolved; rabbitmq and nginx came back as "no published image".
            return ({k.lower(): v for k, v in response.headers.items()},
                    response.read())
        return json.loads(response.read())


def _token(registry: str, path: str) -> str:
    """An anonymous pull token. Empty when the registry needs none."""
    services = {
        "docker.io": ("https://auth.docker.io/token"
                      "?service=registry.docker.io&scope=repository:{path}:pull"),
        "quay.io": "https://quay.io/v2/auth?service=quay.io&scope=repository:{path}:pull",
        "ghcr.io": "https://ghcr.io/token?service=ghcr.io&scope=repository:{path}:pull",
        # ORACLE'S REALM IS A DIFFERENT SHAPE, and its absence was not a policy
        # decision. `container-registry.oracle.com` has been on the allow-list
        # since the registries were broadened, and no image has ever been pulled
        # from it, because a registry with no entry here gets no token and every
        # manifest request comes back 401. REQ-2026-0265 asked for Oracle
        # Database and was told "no image on a registry this portal allows" --
        # true as written, and misleading: the registry was allowed and
        # unreachable.
        #
        # It is `/auth` rather than `/token`, and the service is "Oracle
        # Registry" with a space. The free editions -- database/free and
        # database/express -- answer anonymously; database/enterprise refuses,
        # which is the licence boundary and not something to work around here.
        "container-registry.oracle.com":
            "https://container-registry.oracle.com/auth"
            "?service=Oracle%20Registry&scope=repository:{path}:pull",
    }
    url = services.get(registry)
    if not url:
        return ""
    try:
        body = _get(url.format(path=path), {})
    except Exception:  # noqa: BLE001 - an unreachable registry is not an error here
        return ""
    return str(body.get("token") or body.get("access_token") or "")


def _host(registry: str) -> str:
    # Docker Hub's API lives on a different host from its canonical image prefix.
    return "registry-1.docker.io" if registry == "docker.io" else registry


def describe(image: str, tag: str = "latest") -> dict | None:
    """{digest, ports} for one image reference, or None if it is not there.

    None covers every negative alike — no such image, no such tag, a registry
    that would not answer. The caller treats them the same way: no container
    rung, and nothing claimed.
    """
    registry, _, path = (image or "").partition("/")
    if registry not in profile_rules.REGISTRIES or not profile_rules.IMAGE_PATH.match(path):
        return None
    if not profile_rules.IMAGE_TAG.match(tag or ""):
        return None

    token = _token(registry, path)
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    base = f"https://{_host(registry)}/v2/{path}"
    try:
        headers, raw = _get(f"{base}/manifests/{tag}",
                            {"Accept": _MANIFEST_TYPES, **auth}, want_headers=True)
    except Exception:  # noqa: BLE001 - absence and unreachability are both "no"
        return None

    # THE DIGEST THE REGISTRY ITSELF NAMES for this tag. Taken from the response
    # header rather than computed here: the registry is the authority on what
    # this tag currently points at, and re-deriving it would only introduce a way
    # to be subtly wrong.
    digest = str(headers.get("docker-content-digest") or "").strip()
    if not digest.startswith("sha256:") or not profile_rules.SHA256.match(
            digest[len("sha256:"):]):
        return None

    # BOTH, when the tag names an index. The recipe pins the index; the machine
    # will report the platform build. Neither is wrong and they are not equal.
    described = {"digest": digest, "ports": _ports(base, auth, raw)}
    platform = _platform_digest(raw)
    if platform and platform != digest:
        described["platform_digest"] = platform
    return described


def _platform_digest(manifest_raw: bytes) -> str:
    """The amd64/linux child of a multi-architecture index, or "".

    WHY BOTH DIGESTS ARE KEPT. `opensearchproject/opensearch` publishes an
    index — one digest naming a list of per-architecture manifests. The recipe
    pins the INDEX, which is right: it is what a person reads and what stays
    stable across architectures. But podman, having pulled it, reports the
    PLATFORM manifest it actually resolved:

        pinned              sha256:bcc179...   the index
        the machine's own   sha256:39a8f8...   its amd64 child

    The machine's check compared the two and reported MISMATCH — correctly, on
    the evidence it had, and wrongly about the world. Both digests name the same
    image. REQ-2026-0238 failed on this, and so would every multi-architecture
    image, which is most modern ones.
    """
    try:
        manifest = json.loads(manifest_raw)
    except (ValueError, TypeError):
        return ""
    children = manifest.get("manifests")
    if not isinstance(children, list):
        return ""
    for child in children:
        platform = child.get("platform") or {}
        if (platform.get("architecture") == "amd64"
                and platform.get("os") == "linux"):
            digest = str(child.get("digest") or "")
            if digest.startswith("sha256:") and profile_rules.SHA256.match(
                    digest[len("sha256:"):]):
                return digest
    return ""


def _ports(base: str, auth: dict, manifest_raw: bytes) -> list[int]:
    """TCP ports the image DECLARES it listens on, from its own config.

    The publisher's statement of the interface, which is the closest thing to an
    authority that exists — better than a guess, and better than a row typed
    here. Empty when the image says nothing, which is a real answer: a container
    that exposes nothing needs no firewall opened for it.
    """
    try:
        manifest = json.loads(manifest_raw)
        # A multi-architecture index points at per-architecture manifests; the
        # config lives one level down. amd64/linux is what these machines are.
        if isinstance(manifest.get("manifests"), list):
            child = next(
                (m for m in manifest["manifests"]
                 if (m.get("platform") or {}).get("architecture") == "amd64"
                 and (m.get("platform") or {}).get("os") == "linux"), None)
            if not child:
                return []
            manifest = _get(f"{base}/manifests/{child['digest']}",
                            {"Accept": _MANIFEST_TYPES, **auth})
        config_digest = (manifest.get("config") or {}).get("digest")
        if not config_digest:
            return []
        config = _get(f"{base}/blobs/{config_digest}", {"Accept": "*/*", **auth})
    except Exception:  # noqa: BLE001 - no ports declared is a valid outcome
        return []

    exposed = ((config.get("config") or {}).get("ExposedPorts") or {})
    ports: list[int] = []
    for spec in exposed:
        number, _, proto = str(spec).partition("/")
        if proto and proto != "tcp":
            continue
        if number.isdigit() and 0 < int(number) < 65536 and int(number) not in ports:
            ports.append(int(number))
    return sorted(ports)


#: A tag that names a RELEASE and nothing else: 9.5.2, v3, 1.24.0.
#:
#: Deliberately strict. `1-alpine`, `2017-CU1-ubuntu` and `8.15.0-arm64` are
#: variants — a different base image or architecture, not a different version of
#: the software — and `nightly`, `8.0.0-rc1` and `edge` are not releases at all.
#: Choosing one of those as THE image for a technology would be picking a
#: development build for production, which is the `mongodb-atlas-local` mistake
#: in another costume.
_RELEASE_TAG = re.compile(r"^v?\d+(?:\.\d+)*$")


def _version_of(tag: str) -> tuple:
    """A release tag as numbers, for ordering. Not a string sort: string order
    puts `9.5.2` before `10.0.0`, which would pin a major version behind."""
    return tuple(int(part) for part in tag.lstrip("vV").split("."))


def tags(image: str, *, fetch=None) -> list[str]:
    """Every tag a registry lists for this image, or [].

    Fail-soft like everything else here: a registry that will not list is one
    source of several, not a failure.
    """
    registry, _, path = (image or "").partition("/")
    if registry not in profile_rules.REGISTRIES or not profile_rules.IMAGE_PATH.match(path):
        return []
    try:
        if fetch is not None:
            body = fetch(image)
        else:
            token = _token(registry, path)
            auth = {"Authorization": f"Bearer {token}"} if token else {}
            body = _get(f"https://{_host(registry)}/v2/{path}/tags/list", auth)
    except Exception:  # noqa: BLE001 - a registry that will not list is not an error
        return []
    return [str(t) for t in ((body or {}).get("tags") or [])]


def newest_release(image: str, *, fetch=None) -> str:
    """The highest release tag this image publishes, or "".

    WHY THIS EXISTS. `find` asked only for `latest`, and Elasticsearch does not
    publish one: Docker Hub's official image was deprecated and its `latest` tag
    withdrawn, leaving 406 versioned tags and no default. So an image that is
    published, official, and on an allow-listed registry was invisible, the
    ladder came back EMPTY, and REQ-2026-0232 fell through to drafting Terraform
    and certified a guess that installed nothing.

    HIGHEST, not "the one somebody pinned". A pin is a table, and this project
    has spent a fortnight establishing that tables do not survive contact with
    the next technology. The choice is still measured — the tag must resolve to
    a real manifest — and still proved on a machine before anything is
    certified, so a wrong guess costs a proof rather than a provisioning.
    """
    releases = [t for t in tags(image, fetch=fetch) if _RELEASE_TAG.match(t)]
    if not releases:
        return ""
    return max(releases, key=_version_of)


def find(code: str, tag: str = "latest") -> dict | None:
    """The first published image for this technology, pinned, or None.

    None is the honest answer for software that publishes no image — internal or
    licensed software will always need a recipe someone writes — and the ladder
    treats it as the end of the road rather than as a failure.
    """
    # GUESSED NAMES FIRST, then what the registry itself offers (D1). The
    # guesses are free — no network call until `describe` — and they find the
    # common case (`rabbitmq`, `nginx`). The search is what finds `mongodb`,
    # whose image is published under the vendor's own namespace and which no
    # name-shape rule was ever going to produce.
    guesses = list(candidates(code))
    ordered = guesses + [ref for ref in search(code) if ref not in guesses]

    # LAST, so discovery always wins. A vendor that starts publishing somewhere
    # guessable is found by the guess, and the curated row becomes dead weight
    # rather than a wrong answer that outranks a right one.
    curated = PUBLISHED_IMAGES.get(code)
    if curated and curated not in ordered:
        ordered.append(curated)

    # AN IMAGE THAT DECLARES NO PORT IS PROBABLY NOT THE SERVICE.
    #
    # `mcr.microsoft.com/mssql/*` offers a dozen repositories. `mssql/server`
    # declares 1433; `mssql/ha` and every `mssql/bdc/*` controller declare
    # nothing. The same shape as the `mongodb-atlas-local` mistake, and the same
    # answer: rank on something MEASURED rather than on the order a registry
    # happened to reply in. A service we are being asked to install listens.
    #
    # Still only a preference. An image that declares nothing is returned when
    # nothing better resolves, because plenty of legitimate images say nothing
    # and refusing them all would be a worse error than ranking them last.
    silent = None
    for image in ordered[:FIND_LIMIT]:
        used = tag
        described = describe(image, used)
        if not described and tag == "latest":
            # NO `latest` IS NOT NO IMAGE. Elasticsearch publishes 406 tags and
            # no `latest`; asking only for the default made it invisible.
            newest = newest_release(image)
            if newest:
                used, described = newest, describe(image, newest)
        if not described:
            continue
        found = {"image": image, "tag": used, **described}
        if described.get("ports"):
            return found
        if silent is None:
            silent = found
    return silent


# --- D1: ask the registry, do not guess the image name -----------------------
#
# `candidates` builds image references from the catalogue code, which finds
# `rabbitmq` and misses `mongo` (the code is `mongodb`) and `hashicorp/vault`.
# Guessing an image name is the same defect as guessing a package name, one
# level up, and it is the reason container-first barely fired when D1 first ran.
#
# THE TRUST RULE IS THE WHOLE FEATURE. A registry search for "dotnet" returns
# `slacksec/dotnet` — zero stars, an unknown user — and pulling that as root is
# far worse than a failed guess. So a search result is a candidate ONLY if:
#
#   * the registry calls it OFFICIAL, or
#   * it is published under a namespace that IS the technology's own name
#     (`mongodb/mongodb-community-server` for mongodb), which is how a vendor
#     publishes under its own account.
#
# Everything else is discarded, however popular. Popularity is not provenance.

SEARCH_URL = "https://hub.docker.com/v2/search/repositories/"

#: How many results to consider. The trust rule discards nearly all of them;
#: this only bounds the response we parse.
SEARCH_LIMIT = 10


def _trusted(repo_name: str, official: bool, code: str) -> bool:
    """Would we let this image run as root in the tenancy?

    A VENDOR'S OWN ACCOUNT MAY CARRY A SUFFIX, and that is the norm rather than
    the exception. REQ-2026-0236 asked for OpenSearch, which publishes at
    `opensearchproject/opensearch` — 198 stars, manifestly the project's own
    account — and this rule refused it because the namespace is not EXACTLY
    `opensearch`. The request was refused with "no container image was found on
    a registry this portal trusts", which was true as written and misleading.

    So a namespace that STARTS with the technology's name is trusted too, but
    only when the repository name does as well. Surveyed against what Docker
    Hub actually returns before it was switched on:

        opensearchproject/opensearch  198  trusted   the project's own account
        onlyoffice/opensearch           0  refused   somebody else's product
        rancher/opensearch              0  refused
        bitnamicharts/opensearch        1  refused   a packager, not the vendor
        itzg/elasticsearch             72  refused   popular and still a stranger
        slacksec/dotnet                 0  refused   the image this rule exists for

    THIS IS A REAL WIDENING OF WHAT MAY RUN AS ROOT and it is not free: an
    account named `opensearchmalware` would satisfy it. What stands behind it is
    that BOTH halves must match, the image is pinned by digest, ranking prefers
    stars within what is already trusted, and nothing is certified until a
    machine has built it. Recorded here so the next person weighing it has the
    same facts.

    WHAT IT DOES NOT FIX, and no name rule can: `apache/kafka` is Kafka's real
    image, and nothing in the word "kafka" points at Apache. That needs a
    first-party catalogue or a curated entry, not a looser rule here.
    """
    if official:
        return True
    namespace, _, tail = (repo_name or "").partition("/")
    if not namespace or "/" not in (repo_name or ""):
        return False
    stem = re.sub(r"[^a-z0-9]", "", (code or "").lower()).rstrip("0123456789")
    ns = re.sub(r"[^a-z0-9]", "", namespace.lower())
    if not stem:
        return False
    if ns == stem:
        return True
    # The suffixed form. The repository has to name the technology too, so a
    # vendor's unrelated tooling under the same account is not swept in.
    repo = re.sub(r"[^a-z0-9]", "", (tail or "").lower())
    return ns.startswith(stem) and repo.startswith(stem)


def search(code: str, *, fetch=None, fetch_catalogue=None) -> list[str]:
    """Image references the registries themselves offer for this technology.

    Injected `fetch` for testability; returns [] on any problem, because a
    registry that cannot be searched is not a failure — it is one source of
    several, and the ladder has others.

    HERMETIC WHEN `fetch` IS INJECTED. A caller that scripts Hub's answer is a
    test, and a test that silently acquired a live catalogue sweep would make
    the trust rule's own assertions depend on what Microsoft published today.
    `image_for` in api/resolve.py carries the same rule for the same reason.
    """
    code = (code or "").strip().lower()
    if not profile_rules.CODE.match(code):
        return []

    try:
        if fetch is None:
            if mode() != "live":
                raise RegistryOffline(f"REGISTRY_MODE={mode()}: not searching for {code}")
            import json
            import urllib.request

            url = f"{SEARCH_URL}?query={code}&page_size={SEARCH_LIMIT}"
            with urllib.request.urlopen(  # noqa: S310 - fixed registry host
                    urllib.request.Request(url, headers={"User-Agent": "shiftleft"}),
                    timeout=timeout_seconds()) as response:
                body = json.loads(response.read())
        else:
            body = fetch(code)
    except Exception:  # noqa: BLE001
        return []

    # RANKED HERE, NOT BY THE REGISTRY.
    #
    # Docker Hub's own ordering is a relevance score that is not stable between
    # calls, and trusting it picked `mongodb/mongodb-atlas-local` — MongoDB's
    # LOCAL DEVELOPMENT EMULATOR — over `mongodb/mongodb-community-server`, on
    # one call and not the next. Provisioning a development emulator as a
    # production database because a search API reshuffled is not a defect anyone
    # would find by reading the code.
    #
    # official first, then most-used, then the shorter name. Stars are a fair
    # tiebreak HERE and nowhere else: these are already the vendor's own images,
    # so popularity separates the canonical one from its variants rather than
    # separating a stranger from a vendor.
    scored = []
    for row in (body or {}).get("results", [])[:SEARCH_LIMIT]:
        name = str(row.get("repo_name") or "")
        official = bool(row.get("is_official"))
        if not _trusted(name, official, code):
            continue
        path = name if "/" in name else f"library/{name}"
        ref = f"docker.io/{path}"
        if profile_rules.IMAGE_PATH.match(path):
            try:
                stars = int(row.get("star_count") or 0)
            except (TypeError, ValueError):
                stars = 0
            scored.append((0 if official else 1, -stars, len(name), name, ref))

    out: list[str] = []
    for _o, _s, _l, _n, ref in sorted(scored):
        if ref not in out:
            out.append(ref)

    # AND THE NINETEEN OTHERS. Hub's answer stays first — it is a search, ranked
    # by a registry that knows what is used — and the vendors' own catalogues
    # follow, which is where software Hub does not carry lives.
    if fetch is None or fetch_catalogue is not None:
        for ref in catalogue_search(code, fetch_catalogue=fetch_catalogue):
            if ref not in out:
                out.append(ref)
    return out


# --- D2: the nineteen registries we allow and never ask -----------------------
#
# `search` above asks Docker Hub, and Docker Hub only. Docker Hub is the registry
# a stranger can publish to, so it is the one that needed a trust rule — and
# having written the trust rule I never went back and asked the other nineteen.
#
# SQL SERVER IS WHAT THAT COST. Microsoft publishes it at
# `mcr.microsoft.com/mssql/server` and nowhere else. `candidates` builds
# `docker.io/library/mssql`, which does not exist; `search` asks Hub, which does
# not carry it. So the container rung — the only rung SQL Server could ever have
# used — was unreachable, and REQ-2026-0217 through 0224 each spent a real
# machine establishing that `dnf install mssql` installs nothing.
#
# A FIRST-PARTY REGISTRY IS ONE PUBLISHER BY CONSTRUCTION, and that is precisely
# why it is on the allow-list: everything on mcr.microsoft.com is Microsoft's,
# everything on registry.redhat.io is Red Hat's. There is no stranger to guard
# against, which is what makes the OCI catalogue endpoint usable here and
# unusable on Hub.
#
# STILL A SHAPE AND NOT A TABLE. Nothing below names a technology.

#: Registries where anyone may publish, so a namespace identifies a stranger and
#: `_trusted` has to do the work. Everything else on the allow-list is a vendor
#: publishing its own software.
_SHARED_REGISTRIES = frozenset({
    "docker.io", "quay.io", "ghcr.io", "registry.gitlab.com",
})

#: How much of a catalogue to parse. mcr.microsoft.com returns 3,728 entries in
#: a single request; this bounds a registry that would return far more.
CATALOGUE_LIMIT = 20000

#: How many resolved candidates `find` will measure before settling. Each one is
#: a manifest fetch, and a first-party catalogue can offer a dozen accessories
#: around one product.
FIND_LIMIT = 8

_catalogues: dict[str, tuple[str, ...]] = {}


def first_party_registries() -> list[str]:
    """Allow-listed registries that are one vendor's own, sorted for stability."""
    return sorted(r for r in profile_rules.REGISTRIES
                  if r not in _SHARED_REGISTRIES)


def catalogue(registry: str, *, fetch=None) -> tuple[str, ...]:
    """Every repository a registry admits to publishing, or ().

    CACHED FOR THE LIFE OF THE PROCESS. It is one request and the same answer
    for every technology asked about in that time; a registry re-asked once per
    candidate name would make the ladder's cost depend on how many names we
    guessed.

    () is the ordinary answer, not a failure: most registries require a token
    even to list, and the ladder simply learns nothing from them.
    """
    if registry in _catalogues:
        return _catalogues[registry]
    try:
        body = (fetch(registry) if fetch is not None
                else _get(f"https://{registry}/v2/_catalog?n={CATALOGUE_LIMIT}", {}))
        names = tuple(str(n) for n in ((body or {}).get("repositories") or ())
                      )[:CATALOGUE_LIMIT]
    except Exception:  # noqa: BLE001 - a registry that will not list is not an error
        names = ()
    _catalogues[registry] = names
    return names


def _first_party_match(path: str, code: str) -> bool:
    """Is this repository THIS technology's, on a registry that is one vendor's?

    The first path segment. On a shared registry that segment is an account name
    and proves nothing, which is why `_trusted` exists; here it is the vendor's
    own product grouping — `mssql/server`, `dotnet/core/sdk`.
    """
    stem = re.sub(r"[^a-z0-9]", "", (code or "").lower()).rstrip("0123456789")
    head = re.sub(r"[^a-z0-9]", "", (path or "").split("/")[0].lower())
    return bool(stem) and head == stem


def catalogue_search(code: str, *, fetch_catalogue=None) -> list[str]:
    """Image references first-party registries publish for this technology."""
    code = (code or "").strip().lower()
    if not profile_rules.CODE.match(code):
        return []

    out: list[str] = []
    for registry in first_party_registries():
        for path in catalogue(registry, fetch=fetch_catalogue):
            ref = f"{registry}/{path}"
            if (profile_rules.IMAGE_PATH.match(path)
                    and _first_party_match(path, code) and ref not in out):
                out.append(ref)

    # SHALLOWEST PATH FIRST, and it is not cosmetic: a vendor groups accessories
    # BELOW its product, so `mssql/server` outranks `mssql/bdc/mssql-controller`
    # on depth alone. Depth is a weak signal and it does not decide — `find`
    # measures the ports each image declares and prefers one that actually
    # listens — but it decides what gets measured first, and there are 3,700
    # repositories to get through.
    return sorted(out, key=lambda ref: (ref.count("/"), len(ref), ref))

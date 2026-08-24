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

_MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))

_AGENT = "emaratech-infra-portal/1.0"


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

    return {"digest": digest, "ports": _ports(base, auth, raw)}


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


def find(code: str, tag: str = "latest") -> dict | None:
    """The first published image for this technology, pinned, or None.

    None is the honest answer for software that publishes no image — internal or
    licensed software will always need a recipe someone writes — and the ladder
    treats it as the end of the road rather than as a failure.
    """
    for image in candidates(code):
        described = describe(image, tag)
        if described:
            return {"image": image, "tag": tag, **described}
    return None

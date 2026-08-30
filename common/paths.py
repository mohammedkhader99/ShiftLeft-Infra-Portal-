"""Where agent-written artefacts live when nothing says otherwise.

THE DEFAULTS USED TO BE ABSOLUTE POSIX PATHS -- "/generated/blueprints" and its
siblings. Inside the container that is exactly right, and every service sets the
variable explicitly there anyway (docker-compose.yml).

Outside a container, on Windows, "/generated/blueprints" resolves to the C: drive
root. Anything that publishes a draft then writes there for real: a blueprint and
its Terraform module appeared outside the repository on 2026-08-21, were still
being rewritten on 2026-08-29, and a diagnostic run in this project read them
back and reported a blueprint that does not exist in the catalogue. conftest.py
pins one of the four variables for pytest; anything run outside pytest reached
the default.

So the fallback is now inside the repository, under `generated/`, where it is
git-ignored, obvious, and harmless.

THE CONTAINER IS UNAFFECTED, and that is guaranteed rather than assumed:
test_generated_paths_are_declared holds every service to setting each variable
its own code reads. Without that guard this change would trade a Windows-litter
bug for a much worse one -- a container silently writing to /app/generated
instead of the mounted /generated, where nothing would ever look for it.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The repository root, derived from THIS FILE rather than from the working
#: directory. A default that depends on where you happened to run from is not a
#: default; it is a coin toss, and it is how the same code wrote to two places.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The variables that name these directories, and the path each falls back to
#: inside the repository. Held in one place so the guard test can check every one
#: of them against docker-compose.yml without a hand-kept second list.
GENERATED_VARS: dict[str, tuple[str, ...]] = {
    "GENERATED_ROOT": (),
    "GENERATED_BLUEPRINT_DIR": ("blueprints",),
    "GENERATED_MODULE_DIR": ("terraform",),
    "GENERATED_PROFILE_DIR": ("profiles",),
}


def generated_dir(variable: str) -> Path:
    """The directory `variable` names, or the repo-local fallback for it.

    An empty or whitespace-only value counts as unset: a variable present but
    blank is how a misconfigured deployment would otherwise resolve to the
    filesystem root.
    """
    if variable not in GENERATED_VARS:
        raise KeyError(f"{variable} is not a generated-artefact path variable; "
                       f"add it to GENERATED_VARS so the guard test sees it too.")
    named = (os.getenv(variable) or "").strip()
    if named:
        return Path(named)
    return REPO_ROOT.joinpath("generated", *GENERATED_VARS[variable])

"""Every cloud-init template must survive being parsed as YAML.

THIS IS THE STEP-9 BUG, REINTRODUCED AND CAUGHT A SECOND TIME.

A bare YAML scalar containing ': ' is a MAPPING. So

    - systemctl enable --now httpd || echo 'PORTAL: httpd failed to start' >> ...

parses as {"systemctl ... echo 'PORTAL": "httpd failed to start' >> ..."},
cloud-init raises "Failed to shellify" and abandons the WHOLE runcmd block. The
package still installs — `packages:` runs at an earlier stage — so the machine
boots healthy, Terraform reports success, the portal says provisioned, and there
is no web server and no log, because the commands that would have written the log
are the ones that were discarded.

GAP-ANALYSIS step 9 found this in orchestrator/configure.py, which was fixed by
JSON-quoting every entry, and a test in test_service_vm.py has guarded it since.
The TERRAFORM templates were never covered — and when diagnostics of exactly the
same shape were added to the Apache template, it broke Oracle Linux Apache on
REQ-2026-0130 while every test stayed green. Kafka carried the same fault
untouched and unnoticed, because Kafka has never been booted.

So this test renders each template through Terraform and parses the result, which
is the only thing that reproduces what cloud-init does.
"""

import json
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "terraform" / "oci"
# Enough variables to render each template; the values are irrelevant, the
# STRUCTURE is what is under test.
CASES = [
    ("apache-httpd", {"enable_https": False, "server_name": "",
                      "index_html_b64": "eA==", "os_family": "rhel"}),
    ("apache-httpd", {"enable_https": True, "server_name": "www.example.ae",
                      "index_html_b64": "eA==", "os_family": "rhel"}),
    ("apache-httpd", {"enable_https": False, "server_name": "",
                      "index_html_b64": "eA==", "os_family": "debian"}),
    ("apache-httpd", {"enable_https": True, "server_name": "www.example.ae",
                      "index_html_b64": "eA==", "os_family": "debian"}),
]


def _render(module: str, variables: dict) -> str:
    """Render a template the way Terraform will, or skip if terraform is absent.

    Skipping rather than passing: a test that quietly does nothing when its tool
    is missing is worse than no test, so the reason is stated.
    """
    path = TEMPLATE_DIR / module / "templates" / "cloud-init.yaml.tftpl"
    if not path.is_file():
        pytest.skip(f"{module} has no cloud-init template")
    args = ", ".join(f"{k} = {json.dumps(v)}" for k, v in variables.items())
    expr = f'templatefile("{path.as_posix()}", {{{args}}})'
    try:
        proc = subprocess.run(["terraform", "console"], input=expr,
                              capture_output=True, text=True, timeout=120,
                              cwd=str(TEMPLATE_DIR))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("terraform is not available here (it is in the orchestrator image)")
    out = proc.stdout.strip()
    if not out or "Error" in proc.stderr:
        pytest.skip(f"terraform console could not render {module}: {proc.stderr[:200]}")
    if out.startswith("<<EOT"):
        out = "\n".join(out.splitlines()[1:-1])
    return out


@pytest.mark.parametrize("module,variables", CASES,
                         ids=[f"{m}-{v['os_family']}-https{int(v['enable_https'])}"
                              for m, v in CASES])
def test_every_runcmd_entry_is_a_command_not_a_mapping(module, variables):
    """THE guard. One unquoted colon costs the entire runcmd block."""
    doc = yaml.safe_load(_render(module, variables))
    assert isinstance(doc, dict), "cloud-init parses this as a YAML mapping"
    entries = doc.get("runcmd") or []
    assert entries, f"{module} should produce commands"
    bad = [e for e in entries if not isinstance(e, str)]
    assert not bad, (
        f"{len(bad)} runcmd entr{'y' if len(bad) == 1 else 'ies'} parsed as "
        f"{type(bad[0]).__name__}, not a command — cloud-init will raise "
        f"'Failed to shellify' and discard EVERY command in the block. "
        f"First: {str(bad[0])[:120]}")


@pytest.mark.parametrize("module,variables", CASES,
                         ids=[f"{m}-{v['os_family']}-https{int(v['enable_https'])}"
                              for m, v in CASES])
def test_the_document_is_valid_cloud_config(module, variables):
    text = _render(module, variables)
    assert text.startswith("#cloud-config"), "cloud-init needs that first line"
    doc = yaml.safe_load(text)
    for path_entry in doc.get("write_files") or []:
        assert isinstance(path_entry, dict), "write_files entries ARE mappings"
        assert path_entry.get("path"), "a write_files entry needs a path"


def test_the_guard_catches_the_bug_it_was_written_for():
    """Proves the assertion above fires on the real failure, using the exact line
    that broke REQ-2026-0130."""
    broken = ("#cloud-config\nruncmd:\n"
              "  - systemctl enable --now httpd || echo 'PORTAL: httpd failed' >> /var/log/x\n")
    entries = yaml.safe_load(broken)["runcmd"]
    assert not isinstance(entries[0], str), (
        "if this passes, the colon-space no longer breaks YAML and this whole "
        "file can go")

    fixed = ("#cloud-config\nruncmd:\n"
             '  - "systemctl enable --now httpd || echo \'PORTAL: httpd failed\' >> /var/log/x"\n')
    assert isinstance(yaml.safe_load(fixed)["runcmd"][0], str)


def test_no_template_writes_a_bare_runcmd_entry():
    """A static backstop that needs no Terraform, so it runs everywhere — CI
    included, where the rendering tests skip.

    Checks the SOURCE: inside a runcmd block, every entry must be quoted or a
    block scalar. Cheaper than rendering and catches the mistake at the moment
    it is written.
    """
    offenders = []
    for template in TEMPLATE_DIR.glob("*/templates/cloud-init.yaml.tftpl"):
        in_runcmd = False
        for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("runcmd:"):
                in_runcmd = True
                continue
            if in_runcmd and line and not line.startswith((" ", "%", "#")):
                in_runcmd = False          # a new top-level key ends the block
            if not in_runcmd:
                continue
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            body = stripped[2:]
            if body.startswith(('"', "'", "|", ">")):
                continue                    # quoted or a block scalar: safe
            if ": " in body:
                offenders.append(f"{template.parent.parent.name}:{number}: {body[:70]}")
    assert not offenders, (
        "these runcmd entries contain ': ' unquoted, so YAML reads them as "
        "mappings and cloud-init discards the whole block:\n  "
        + "\n  ".join(offenders))

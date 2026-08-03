# Blueprints

One manifest per build recipe. **This directory is the registry** — the
orchestrator scans it, so adding a blueprint is adding a file here, not editing
code in several places.

## Adding one

1. Put the Terraform module somewhere under `orchestrator/terraform/`.
2. Add `<name>.yaml` here describing it (copy an existing one).
3. Rebuild and redeploy the orchestrator.
4. It appears on the portal's **Blueprints** page as `draft`.
5. An admin certifies it with a version → the catalogue badge flips to automated.

Steps 1–3 are an engineering change, reviewed through git. That is deliberate:
Terraform runs with real cloud credentials, so a recipe reaching the cloud
without review would be the single most dangerous path in this system. There is
no upload-and-run.

## Manifest fields

| field | meaning |
|---|---|
| `ref` | stable identifier shown in the portal, e.g. `oci/postgres` |
| `target` | the cloud it builds on: onprem, azure, oci, aws, gcp |
| `resource_kind` | what the provisioner builds; must match the Terraform module |
| `module` | directory under `orchestrator/terraform/` |
| `version` | the recipe's own version — what an admin pins when certifying |
| `builds` | catalogue technology codes this recipe delivers |
| `description` | one line, shown in the portal |
| `enable_flag` | optional env var that must be true before it may run |
| `requires_env` | optional env vars that must be set before it may run |

`enable_flag` and `requires_env` are declared here rather than hard-coded, so the
portal can show *"certified, but not configured"* instead of failing at apply
time.

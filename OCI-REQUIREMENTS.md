# OCI configuration required for portal-driven provisioning

**Purpose:** a working checklist for the meeting with the OCI administrator.
**Date:** 2 August 2026 · **Portal:** Shift-Left Infrastructure Provisioning Portal

Each section states **what the administrator must create or grant**, and **what to send back** for the portal's `.env`. No secrets or OCIDs are recorded in this file.

> **Key point for the discussion:** the portal already authenticates to OCI successfully and provisions object storage today. Everything below is about *authorising and pointing it at* additional services — it is mostly IAM policy plus a handful of identifiers, not a new integration.

---

## 0. What already works — no action needed

| Item | Status |
|---|---|
| Tenancy, user, API signing key, fingerprint, region (`me-dubai-1`) | ✅ Configured and working |
| Target compartment | ✅ Configured |
| **Object storage** — buckets created and destroyed by the portal | ✅ **Live and verified** |
| Compute subnet OCID and OS image OCID | ✅ Already supplied |

---

## 1. Compute (virtual machines) — **blocked on IAM only**

The subnet and image are already configured. The portal cannot create a VM because its user lacks permission. This is the single highest-value grant: it unblocks virtual machines *and* the first-boot configuration layer.

**Ask the administrator to add:**

```
Allow group <portal-group> to manage instance-family in compartment <compute-compartment>
Allow group <portal-group> to use subnets            in compartment <network-compartment>
Allow group <portal-group> to use vnics              in compartment <network-compartment>
Allow group <portal-group> to read instance-images   in tenancy
```

**Also confirm:**
- Which **compartment** VMs should be created in (if different from the current one).
- The **service limit** for the shape family in `me-dubai-1` — a zero limit fails the same way as a missing permission.

**Send back:** nothing new if the existing subnet/image are correct — otherwise the corrected subnet OCID and image OCID.

---

## 2. Network egress — **required for first-boot configuration**

The portal can install and start software on a new VM (nginx, Apache, Redis, Java, Python, Node.js) at first boot. That only works if the VM can reach package repositories.

**Ask the administrator to confirm one of:**
- the compute subnet has a **NAT gateway** (or service gateway) for outbound access, **or**
- an **internal package mirror** is reachable from that subnet — and its address.

**Why it matters:** without egress the install silently produces a bare VM. If neither is available, say so — the portal should then not advertise those technologies as automated.

---

## 3. Managed PostgreSQL (OCI Database with PostgreSQL)

Lets a requester get a real database instead of a ticket.

**Ask the administrator to create:**
1. A **private subnet** for database systems (may be the existing one if policy allows).
2. An **OCI Vault secret** containing the database administrator password.
   - The portal is **never given the password** — only the secret's OCID. It passes the reference to Terraform, so the password never reaches the portal, the plan file, or Terraform state.
3. IAM policy:

```
Allow group <portal-group> to manage postgres-db-systems in compartment <db-compartment>
Allow group <portal-group> to manage postgres-backups    in compartment <db-compartment>
Allow group <portal-group> to use subnets                in compartment <network-compartment>
Allow group <portal-group> to use vnics                  in compartment <network-compartment>
Allow group <portal-group> to read secret-family         in compartment <vault-compartment>
```

**Send back:**
| Value | Goes to |
|---|---|
| Private DB subnet OCID | `OCI_PSQL_SUBNET_OCID` |
| Vault **secret** OCID (admin password) | `OCI_PSQL_ADMIN_SECRET_OCID` |
| DB compartment OCID (if different) | `OCI_PSQL_COMPARTMENT_OCID` |
| Approved shape and PostgreSQL version | `OCI_PSQL_SHAPE`, `OCI_PSQL_VERSION` |

**Also confirm:** that managed PostgreSQL is **available in `me-dubai-1`** and which shapes are permitted. ⚠️ **Cost:** roughly **USD 200–400 per month per database system**.

---

## 4. DNS records

Naming an environment is one of the most frequent reasons to raise a ticket. The portal can create the record itself.

**Ask the administrator to create/confirm:**
1. A **DNS zone** the portal may write to — ideally a dedicated zone such as `envs.<internal-domain>` rather than the main corporate zone, so the blast radius is contained.
2. IAM policy:

```
Allow group <portal-group> to manage dns in compartment <dns-compartment>
```

**Send back:**
| Value | Goes to |
|---|---|
| Zone name (e.g. `envs.example.internal`) | `OCI_DNS_ZONE` |
| DNS compartment OCID (if different) | `OCI_DNS_COMPARTMENT_OCID` |

---

## 5. Encryption with a customer-managed key *(optional but recommended)*

Satisfies the security scanner for confidential and restricted data. Without it, Oracle-managed encryption is used.

```
Allow group <portal-group> to use keys in compartment <kms-compartment>
```

**Send back:** the KMS **key** OCID → `OCI_KMS_KEY_OCID`.

---

## 6. Not OCI — but needed for the same end-to-end flow

**Just-in-time credential delivery** uses **HashiCorp Vault**, not OCI Vault. It gives a requester a one-time link to a short-lived database login; the portal never sees the credential and can revoke it.

**Decide with the platform team:**
- Is there an existing HashiCorp Vault? If so: its address, and a portal token/AppRole limited to reading and wrapping the credential paths.
- If not, this stays in mock mode and access hand-over remains manual.

*(This has been proven working end to end against a real Vault.)*

---

## Priority order for the meeting

| # | Ask | Unblocks | Effort |
|---|---|---|---|
| **1** | **Compute IAM grant** (§1) | Real VMs — the biggest single unlock | Policy only |
| **2** | **Subnet egress** (§2) | Software installed automatically on VMs | Confirm or add NAT |
| 3 | DNS zone + policy (§4) | Self-service naming; cheap, low risk | Small |
| 4 | PostgreSQL subnet + vault secret + policy (§3) | Real databases | Larger; has cost |
| 5 | KMS key (§5) | Compliance for sensitive data | Small |

If only one thing comes out of the meeting, make it **§1**. If two, add **§2** — together they turn the portal from "creates buckets" into "delivers working servers".

---

## Questions the administrator will likely ask

**"What can the portal already do?"** — Create and destroy object storage buckets in the target compartment, using an existing least-privilege user. It has done so successfully.

**"Will it create things without approval?"** — No. Every request is approved in Jira first; the orchestrator re-verifies that approval independently before acting. Segregation of duties is enforced, and every privileged action is written to a tamper-evident audit log.

**"What stops it running away with cost?"** — Each capability is individually gated and off by default. Budgets and quotas are enforced at submission, and cost is shown and approved before anything is built.

**"Does it hold our secrets?"** — It holds the OCI API signing key it already has. Database passwords are referenced by vault OCID and never handled by the portal.

**"Can we limit the blast radius?"** — Yes, and it is recommended: a dedicated compartment, a dedicated DNS zone, and per-service policies scoped to those compartments.

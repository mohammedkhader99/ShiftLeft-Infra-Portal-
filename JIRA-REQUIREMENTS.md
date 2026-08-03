# Jira configuration required for portal-driven approvals

**Purpose:** a working checklist for the meeting with the Jira administrator.
**Date:** 2 August 2026 · **Instance:** `jira.emaratech.ae` (Jira Server / Data Center) · **Project:** `SDIMD`, issue type *Service Request*

Each section states **what the administrator must change or provide**, and **why it matters**. No credentials are recorded in this file.

> **Key point for the discussion:** the portal already creates real tickets in SDIMD and reads approvals back. Two things are missing, and both are governance rather than plumbing: **tickets are raised under the service account instead of the real requester**, and **portal roles are not yet driven by Jira groups**.

---

## 0. What already works — no action needed

| Item | Status |
|---|---|
| Service-account PAT authentication, REST API v2 | ✅ Working |
| Ticket creation in SDIMD (*Service Request*) | ✅ **Live — real tickets created** |
| Required custom fields on the create screen | ✅ Solved by replicating template issue `SDIMD-68275` |
| Reading approval status back | ✅ Working (`Assigned` = approved) |
| Transitions to *In Progress* and *Resolved*, with the fields Resolve requires | ✅ Working |
| Comments and attachments (cost PDF / Excel) | ✅ Working |
| Subsidiary dropdown synced from the create screen | ✅ Working |

---

## 1. Reporter on the create screen — **the main ask**

**Problem.** Setting `reporter` fails with *"Field 'reporter' cannot be set. It is not on the appropriate screen, or unknown."* So every ticket is raised **as the service account**, not as the person who submitted it.

**Why it matters.** Your approval routing sends requests to the **reporter's line manager**. With the service account as reporter, requests route to *that account's* manager — not the requester's. Approvals are therefore going to the wrong person, which undermines the whole approval chain. It also makes the audit trail read as though one account raised everything.

**Ask the administrator to:**
1. Add the **Reporter** field to the *Service Request* **create screen** for SDIMD, **and**
2. Grant the service account the **Modify Reporter** permission in that project's permission scheme.

**Then we set:** `JIRA_SET_REPORTER=true` (currently `false` as a workaround).

**If they cannot change SDIMD's screen**, the alternative is a **dedicated request type or project for portal requests** — see §7, which solves this and several other items at once.

---

## 2. Jira group names for portal roles — **RBAC is currently not enforced**

**Problem.** The portal's roles (who may request, approve, administer, audit) are designed to come from **Jira group membership**. That mapping is empty, so role enforcement is running in mock mode.

**Ask the administrator for the real group names** covering:

| Portal role | What it allows | Group name needed |
|---|---|---|
| `requester` | Raise requests | |
| `approver` | Approve/reject, grant access, waive policy | |
| `platform_admin` | Administer the portal, execute operations | |
| `auditor` | Read the estate and the audit trail | |
| `finops` | Cost and budget views | |
| `read_only` | View only (safe default) | |

**Also required:** the service account must be able to **read other users' group membership** (Browse Users permission). Without it the portal cannot resolve anyone's role.

**Note:** these are entered in the portal's own **Admin → Access control** panel — no `.env` change needed. A user who resolves to no group gets `read_only`, so the failure mode is safe.

---

## 3. Email → Jira username mapping

**Problem.** Portal identity comes from Microsoft Entra (email address); Jira usernames are different — e.g. `Mohammed.Khader` rather than the email.

**Ask the administrator:**
- Is there a **reliable convention** (e.g. `First.Last` from the email local part)?
- Or can the service account **look users up by email** via the REST API?

This affects both §1 (setting the correct reporter) and §2 (resolving a signed-in user's groups).

---

## 4. How approvals are recorded — affects four-eyes and quorum

The portal enforces that **the approver must not be the requester** (four-eyes) and can require **N distinct approvers** (quorum). It determines who approved by reading the **Jira Service Management approval API**, falling back to the **issue changelog**.

**Ask the administrator to confirm:**
- Do approvals go through **JSM approvals** (an Approve/Decline step), or are they plain **status transitions** to `Assigned`?
- Can the service account **read the issue changelog** (usually granted with Browse Projects)?
- Is **multi-approver** approval required for any request class? If so, how many?

**Why it matters:** four-eyes is currently **disabled** (`FOUR_EYES_ENFORCED=false`) because the approver cannot be reliably identified until this is confirmed. It is a governance control worth turning on.

---

## 5. Status names and transition rights

Currently configured:

| Meaning | Status |
|---|---|
| Approved (provision it) | `Assigned` |
| Rejected | `Rejected`, `Withdrawn` |
| Provisioning started | `In Progress` |
| Finished | `Resolved` |

**Ask the administrator to confirm:**
- These are the correct, **stable** status names — a workflow rename silently breaks approval detection.
- The service account may perform the *In Progress* and *Resolved* transitions from wherever the ticket sits after approval.
- ⚠️ Whether anything **else** can move a ticket to `Assigned`. If a status the portal reads as "approved" can be reached without a real approval, that is a bypass of the approval gate.

---

## 6. Approval SLA expectations

The portal tracks how long approvals take, warns before breach, and escalates by commenting on the ticket.

**Ask:** what is the agreed approval SLA (hours) for infrastructure requests? *(Default: 24.)*

---

## 7. Optional but recommended — a dedicated request type

Raising portal requests through a **dedicated request type** (or its own project) would resolve several items at once:

- Reporter can be on **its** create screen without touching SDIMD's shared screen (§1).
- A clean approval workflow with an explicit approval step (§4).
- Fewer mandatory custom fields, removing the dependence on replicating template issue `SDIMD-68275` — which breaks if that ticket is deleted or its fields change.
- Portal traffic is separable from human-raised tickets for reporting.

Worth asking whether this is easier for them than modifying the existing screen.

---

## Priority order for the meeting

| # | Ask | Unblocks | Effort |
|---|---|---|---|
| **1** | **Reporter on create screen + Modify Reporter** (§1) | Approvals routing to the *right* manager | Screen + permission |
| **2** | **Jira group names + Browse Users** (§2) | Turning on real access control | Names + permission |
| 3 | Email → username convention (§3) | Prerequisite for 1 and 2 | Answer only |
| 4 | Confirm approval mechanism (§4) | Enabling four-eyes | Answer only |
| 5 | Confirm statuses can't be reached without approval (§5) | Closing a possible gate bypass | Review |
| 6 | Dedicated request type (§7) | Solves 1 and 4 cleanly | Larger |

**#1 and #2 are the ones that matter.** Without #1 approvals go to the wrong manager; without #2 the portal cannot enforce who is allowed to do what. Items 3–5 are mostly questions, not work.

---

## Questions the administrator will likely ask

**"What does the portal do in Jira today?"** — Creates a *Service Request* in SDIMD with the configuration, cost estimate and a plan preview, attaches the costing PDF and spreadsheet, reads the approval status back, then transitions the ticket to *In Progress* and *Resolved* as it provisions.

**"Will it spam the project?"** — One ticket per infrastructure request, plus comments on state changes (SLA breach, failures, completion). A dedicated request type (§7) would separate this traffic entirely.

**"Can it approve its own tickets?"** — No, and it is designed so it cannot. Jira holds the approval; the portal only reads it. The orchestrator independently re-verifies the approval before acting, and segregation of duties prevents a requester acting on their own request.

**"What permissions does the service account need?"** — Create Issues, Modify Reporter *(new — §1)*, Transition Issues, Add Comments, Create Attachments, Browse Projects (including changelog), and Browse Users *(new — §2)*.

**"What happens if Jira is unavailable?"** — Nothing provisions. The portal holds and retries; approval cannot be inferred locally.

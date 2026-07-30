"""Governance evidence pack PDF for a request (increment F-GOV-10).

One auditor-ready document per request: the request + governance metadata, the
cost breakdown, the approval, the full audit trail (including any SoD / four-eyes
/ SLA governance events), and a tamper-evidence attestation from verify_chain.
Reuses the FPDF helpers from api.attachment; no new dependency.
"""

from datetime import datetime, timezone

from fpdf import FPDF

from api.attachment import AMBER, GREY, TEAL, _heading, _kv

GREEN = (0, 120, 60)
RED = (170, 30, 30)


def build_evidence_pdf(req, breakdown: dict, sizing: dict, audit: list, integrity: dict) -> bytes:
    """Render the governance evidence pack as a PDF (bytes)."""
    currency = breakdown.get("currency", "AED")
    by_cat = breakdown.get("by_category", {})
    totals = breakdown.get("totals", {})

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # --- Title ---
    pdf.set_text_color(*TEAL)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Governance Evidence Pack", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 6, f"Request {req.reference}  -  status: {req.status}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # --- Request ---
    _heading(pdf, "Request", TEAL)
    _kv(pdf, "Requester", req.requester)
    _kv(pdf, "Type", req.request_type)
    _kv(pdf, "Project", req.project_code)
    _kv(pdf, "Cost centre", req.cost_centre_code)
    _kv(pdf, "Deployment target", req.deployment_target)
    _kv(pdf, "Environment", req.environment_name or req.target_environment)
    _kv(pdf, "Environment tier", getattr(req, "environment_tier", None))
    _kv(pdf, "Data classification", req.data_classification)
    pdf.ln(2)

    # --- Governance & ownership ---
    if getattr(req, "business_justification", None) or getattr(req, "priority", None):
        _heading(pdf, "Governance & ownership", TEAL)
        _kv(pdf, "Priority", getattr(req, "priority", None))
        _kv(pdf, "Business criticality", getattr(req, "business_criticality", None))
        _kv(pdf, "Required delivery", getattr(req, "required_delivery_date", None))
        for label, attr in (("Application owner", "application_owner"),
                            ("Business owner", "business_owner"),
                            ("Technical owner", "technical_owner"),
                            ("Environment owner", "environment_owner")):
            value = getattr(req, attr, None)
            if value:
                _kv(pdf, label, value)
        _kv(pdf, "Justification", getattr(req, "business_justification", None))
        pdf.ln(2)

    # --- Advanced options ---
    adv = getattr(req, "advanced_options", None) or {}
    shown = {k: v for k, v in adv.items() if v not in (None, "", "none", False)}
    if shown:
        _heading(pdf, "Advanced options", TEAL)
        for key, value in shown.items():
            _kv(pdf, key.replace("_", " ").title(), "yes" if value is True else str(value))
        pdf.ln(2)

    # --- Policy waiver (F-GOV-02) ---
    waiver = getattr(req, "waiver", None)
    if waiver:
        _heading(pdf, "Policy waiver", AMBER)
        _kv(pdf, "Granted by", waiver.get("granted_by"))
        _kv(pdf, "Granted at", (waiver.get("granted_at") or "")[:19].replace("T", " "))
        _kv(pdf, "Expires", waiver.get("expires_at") or "open-ended")
        _kv(pdf, "Reason", waiver.get("reason"))
        pdf.ln(2)

    # --- Cost ---
    _heading(pdf, "Cost", AMBER)
    for label, key in (("Compute", "compute"), ("Storage", "storage"), ("Licence", "licence"),
                       ("Backup", "backup"), ("Monitoring", "monitoring"), ("Support", "support")):
        value = by_cat.get(key, 0)
        if value:
            _kv(pdf, f"{label} (monthly)", f"{currency} {value:,.2f}")
    for label, key in (("One-time", "one_time"), ("Monthly", "monthly"), ("Annual", "annual")):
        _kv(pdf, f"{label} total", f"{currency} {totals.get(key, 0):,.2f}")
    pdf.ln(2)

    # --- Approval ---
    _heading(pdf, "Approval", TEAL)
    appr = getattr(req, "approval", None)
    if appr is not None:
        _kv(pdf, "Jira ticket", appr.jira_key)
        _kv(pdf, "Status", appr.status)
        if getattr(appr, "ticket_url", None):
            _kv(pdf, "Link", appr.ticket_url)
    else:
        _kv(pdf, "Approval", "none")
    pdf.ln(2)

    # --- Audit trail ---
    _heading(pdf, "Audit trail", TEAL)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*TEAL)
    pdf.set_text_color(255, 255, 255)
    cols = [("When (UTC)", 34), ("Event", 42), ("Actor", 28), ("Detail", 86)]
    for label, width in cols:
        pdf.cell(width, 6, label, border=1, align="C", fill=True)
    pdf.ln()
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(20, 20, 20)
    for entry in audit:
        when = (entry.get("when") or "")[:16].replace("T", " ")
        detail = ", ".join(f"{k}={v}" for k, v in (entry.get("detail") or {}).items())[:62]
        cells = [(when, 34, "L"), ((entry.get("event") or "")[:27], 42, "L"),
                 ((entry.get("actor") or "-")[:17], 28, "L"), (detail, 86, "L")]
        for text, width, align in cells:
            pdf.cell(width, 6, text, border=1, align=align)
        pdf.ln()
    pdf.ln(3)

    # --- Tamper-evidence attestation (F-SEC-01) ---
    ok = integrity.get("ok")
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*(GREEN if ok else RED))
    message = (f"Audit chain: VERIFIED - {integrity.get('total', 0)} entries intact."
               if ok else f"Audit chain: TAMPERING DETECTED at {integrity.get('broken_at')}.")
    pdf.multi_cell(0, 6, message)
    pdf.ln(1)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(
        0, 5,
        f"Generated by the Infrastructure Provisioning Portal on "
        f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}. The audit trail is hash-chained "
        f"and tamper-evident (F-SEC-01); the line above re-verifies it at generation time.",
    )
    return bytes(pdf.output())

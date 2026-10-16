"""Zero-touch reminder engine.

plan()  -> decides, per customer, one of: REMIND (stage n) | AUTO_DEBIT | SKIP(reason).  Pure read, safe to preview.
run()   -> executes the plan: auto-debit first (so paid customers are never mailed), then sends/logs reminders.
Stages come from settings ("0:1,7:2,15:3,30:4" = days past the oldest due date). After the last stage we repeat
every `repeat_every_days`. A stage is never re-sent, and nobody is mailed twice within `min_gap_days`.
"""
import datetime, json, os, smtplib, ssl
from email.message import EmailMessage
from jinja2 import Environment
from .db import get_settings, audit
from . import ledger

SUBJECTS = {1: "Payment reminder - outstanding Rs {amount} | {customer}",
            2: "Second reminder - overdue Rs {amount} | {customer}",
            3: "Urgent - overdue Rs {amount} pending for {days} days | {customer}",
            4: "Final notice - Rs {amount} overdue {days} days | {customer}"}
INTRO = {1: "This is a gentle reminder that the following amount is pending against your account.",
         2: "We have not yet received payment for the dues below, which are now past their due date. Please arrange payment at the earliest.",
         3: "Despite earlier reminders, the dues below remain unpaid. Kindly clear them immediately to avoid disruption of services.",
         4: "This is a final reminder. The dues below are significantly overdue; please settle them or contact us today to avoid service suspension."}
BODY = Environment(autoescape=True).from_string("""
<p>Dear {{ customer }} team,</p><p>{{ intro }}</p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:Arial;font-size:13px">
<tr style="background:#f1f5f9"><th align="left">Period</th><th align="left">Invoice / Description</th><th align="left">Due date</th><th align="right">Amount (Rs)</th></tr>
{% for i in rows %}<tr><td>{{ i.period }}</td><td>{{ i.invoice_no }} {{ i.description }}</td><td>{{ i.due_date }}</td><td align="right">{{ '{:,.2f}'.format(i.balance) }}</td></tr>{% endfor %}
<tr><td colspan="3" align="right"><b>Total outstanding</b></td><td align="right"><b>{{ '{:,.2f}'.format(total) }}</b></td></tr></table>
{% if wallet %}<p>Your prepaid balance on our platform is low. Please top up so that these dues can be adjusted and services continue uninterrupted.</p>{% endif %}
<p>If you have already paid, please share the payment reference (UTR) and we will reconcile it immediately.</p>
<p>Regards,<br>{{ sender }}</p>""")

def _stages(s):
    return sorted((int(a), int(b)) for a, b in (x.split(":") for x in s["stages"].split(",")))

def stage_for(days_past_due, stages):
    st = 0
    for d, n in stages:
        if days_past_due >= d:
            st = n
    return st

def plan(conn, today=None):
    today = today or datetime.date.today()
    s = get_settings(conn); stages = _stages(s); last_stage = stages[-1][1]
    out = []
    for c in conn.execute("SELECT c.*, r.name reseller_name, r.contact_emails reseller_emails, r.remind_reseller FROM customers c "
                          "LEFT JOIN resellers r ON r.id=c.reseller_id"):
        invs = [i for i in ledger.open_invoices(conn, c["id"])]
        total = sum(i["balance"] for i in invs)
        if total < float(s["min_amount"]):
            continue
        overdue = [i for i in invs if i["due_date"] < today.isoformat()]
        item = {"customer_id": c["id"], "customer": c["name"], "kam": c["kam"], "total": round(total, 2)}
        if not overdue:
            out.append({**item, "action": "SKIP", "reason": "nothing overdue yet"}); continue
        dpd = (today - datetime.date.fromisoformat(overdue[0]["due_date"])).days
        item.update(days_past_due=dpd, overdue=round(sum(i["balance"] for i in overdue), 2))
        if c["status"] != "ACTIVE" or not c["remind"]:
            out.append({**item, "action": "SKIP", "reason": f"customer {c['status'].lower()} / reminders off"}); continue
        if s["auto_debit_enabled"] == "1" and c["auto_debit_allowed"] and c["wallet_balance"] >= 1:
            out.append({**item, "action": "AUTO_DEBIT", "amount": round(min(c["wallet_balance"], total), 2)}); continue
        if not (c["email_to"] or "").strip():
            out.append({**item, "action": "SKIP", "reason": "no e-mail on file"}); continue
        last = conn.execute("SELECT stage, sent_at FROM reminder_log WHERE customer_id=? AND status='SENT' ORDER BY id DESC LIMIT 1", (c["id"],)).fetchone()
        stage = stage_for(dpd, stages)
        if stage == 0:
            out.append({**item, "action": "SKIP", "reason": "not due for stage 1"}); continue
        if last:
            since = (today - datetime.date.fromisoformat(last["sent_at"][:10])).days
            if since < int(s["min_gap_days"]):
                out.append({**item, "action": "SKIP", "reason": f"mailed {since}d ago"}); continue
            if stage <= last["stage"] and not (stage == last_stage and since >= int(s["repeat_every_days"])):
                out.append({**item, "action": "SKIP", "reason": f"stage {stage} already sent"}); continue
        out.append({**item, "action": "REMIND", "stage": stage, "wallet_mode": c["collection_mode"] == "WALLET"})
    return out

def compose(conn, customer_id, stage):
    s = get_settings(conn)
    c = conn.execute("SELECT c.*, r.contact_emails rem, r.remind_reseller rr FROM customers c LEFT JOIN resellers r ON r.id=c.reseller_id WHERE c.id=?", (customer_id,)).fetchone()
    rows = ledger.open_invoices(conn, customer_id); total = sum(r["balance"] for r in rows)
    dpd = (datetime.date.today() - datetime.date.fromisoformat(rows[0]["due_date"])).days
    ctx = dict(amount=f"{total:,.0f}", customer=c["name"], days=dpd)
    cc = [x for x in (c["email_cc"] or "").replace(",", ";").split(";") + s["default_cc"].replace(",", ";").split(";") if x.strip()]
    if c["rr"] and c["rem"]:
        cc += c["rem"].split(";")
    if stage >= int(s["escalate_stage"]):
        kam = json.loads(s.get("kam_emails", "{}") or "{}").get((c["kam"] or "").lower())
        if kam: cc.append(kam)
    html = BODY.render(customer=c["name"], intro=INTRO[min(stage, 4)], rows=rows, total=total, wallet=c["collection_mode"] == "WALLET", sender=s["sender_name"])
    to = [x.strip() for x in (c["email_to"] or "").replace(",", ";").split(";") if x.strip()]
    return {"to": to, "cc": sorted({x.strip() for x in cc if x.strip()} - set(to)), "subject": SUBJECTS[min(stage, 4)].format(**ctx), "html": html, "total": total}

def _send(msg, s):
    host = os.environ.get("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP_HOST not configured")
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=30) as sm:
        sm.starttls(context=ssl.create_default_context())
        sm.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        sm.send_message(msg)

def run(conn, today=None, send_mode=None, limit=None):
    s = get_settings(conn); mode = send_mode or s["send_mode"]
    result = {"auto_debited": [], "sent": 0, "dry_run": 0, "failed": 0, "skipped": 0}
    if s["auto_debit_enabled"] == "1":
        result["auto_debited"] = ledger.run_auto_debit(conn)
    done, limit = 0, limit or int(s.get("max_per_run", 0) or 0)
    for p in sorted(plan(conn, today), key=lambda x: -x["total"]):
        if p["action"] != "REMIND":
            result["skipped"] += 1; continue
        if limit and done >= limit: break
        m = compose(conn, p["customer_id"], p["stage"]); done += 1
        msg = EmailMessage(); msg["Subject"] = m["subject"]; msg["From"] = f'{s["sender_name"]} <{s["sender_email"]}>'
        msg["To"] = ", ".join(m["to"]); msg["Cc"] = ", ".join(m["cc"]); msg.set_content("Please view this e-mail in HTML."); msg.add_alternative(m["html"], subtype="html")
        status, err = "SENT", ""
        if mode == "DRY_RUN":
            status = "DRY_RUN"
        else:
            try: _send(msg, s)
            except Exception as e: status, err = "FAILED", str(e)[:300]
        conn.execute("INSERT INTO reminder_log(customer_id,stage,sent_at,to_addr,cc_addr,outstanding,status,error,subject) VALUES(?,?,?,?,?,?,?,?,?)",
                     (p["customer_id"], p["stage"], datetime.datetime.now().isoformat(timespec="seconds"), msg["To"], msg["Cc"], m["total"], status, err, m["subject"]))
        result[{"SENT": "sent", "DRY_RUN": "dry_run", "FAILED": "failed"}[status]] += 1
    audit(conn, "reminder_run", json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in result.items()}))
    conn.commit()
    return result

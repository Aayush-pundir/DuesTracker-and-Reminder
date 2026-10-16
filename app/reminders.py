"""Zero-touch reminder engine (e-mail + WhatsApp).

A *party* is either a customer (chased for the customer-payer invoices) or a reseller (chased for ITS share of its customers' invoices,
in one consolidated statement). plan() decides per party: REMIND(stage, channels) | AUTO_DEBIT | SKIP(reason); run() executes it.
Rules: stages by days past the oldest due date ("0:1,7:2,15:3,30:4"), repeat after the last stage, min gap between mails, dust ignored,
disputed invoices excluded, promise-to-pay pauses chasing until the promised date, dry-run never advances stages.
"""
import datetime, json, time
from jinja2 import Environment
from jinja2.sandbox import SandboxedEnvironment
from markupsafe import Markup, escape
from .db import get_settings, audit, now
from . import ledger, channels

HTML_ENV = SandboxedEnvironment(autoescape=True)
TEXT_ENV = SandboxedEnvironment(autoescape=False)

def inr(n):
    n = round(float(n)); s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        while len(head) > 2:
            tail = head[-2:] + "," + tail; head = head[:-2]
        s = head + "," + tail
    return ("-" if n < 0 else "") + s

def _stages(s):
    return sorted((int(a), int(b)) for a, b in (x.split(":") for x in s["stages"].split(",")))

def stage_for(dpd, stages):
    st = 0
    for d, n in stages:
        if dpd >= d:
            st = n
    return st

def _emails(*xs):
    out = []
    for x in xs:
        out += [e.strip() for e in (x or "").replace(",", ";").split(";") if e.strip()]
    return list(dict.fromkeys(out))

# ------------------------------------------------------------------ plan
def _party_rows(conn):
    for c in conn.execute("SELECT * FROM customers"):
        yield "C", c
    for r in conn.execute("SELECT * FROM resellers"):
        yield "R", r

def plan(conn, today=None):
    today = today or datetime.date.today(); iso = today.isoformat()
    s = get_settings(conn); stages = _stages(s); last_stage = stages[-1][1]
    ch_email, ch_wa = s["channel_email"] == "1", s["channel_whatsapp"] == "1"
    out = []
    for kind, p in _party_rows(conn):
        if kind == "C":
            invs = ledger.open_invoices(conn, p["id"], "CUSTOMER", include_disputed=False)
            addr, cc, wa = _emails(p["email_to"]), _emails(p["email_cc"]), p["whatsapp"]
            allow = p["status"] == "ACTIVE" and p["remind"]
            want_email, want_wa = p["channel_email"], p["channel_whatsapp"]
        else:
            invs = ledger.open_invoices_reseller(conn, p["id"], include_disputed=False)
            addr, cc, wa = _emails(p["contact_emails"]), [], p["whatsapp"]
            allow, want_email, want_wa = p["remind_reseller"], 1, 1
        total = sum(i["balance"] for i in invs)
        if total < float(s["min_amount"]):
            continue
        item = {"party": f"{kind}:{p['id']}", "kind": "CUSTOMER" if kind == "C" else "RESELLER", "id": p["id"], "name": p["name"],
                "kam": p["kam"] if kind == "C" else "", "total": round(total, 2)}
        overdue = [i for i in invs if i["due_date"] < iso]
        if not overdue:
            out.append({**item, "action": "SKIP", "reason": "nothing overdue yet"}); continue
        dpd = (today - datetime.date.fromisoformat(overdue[0]["due_date"])).days
        item.update(days_past_due=dpd, overdue=round(sum(i["balance"] for i in overdue), 2))
        if not allow:
            out.append({**item, "action": "SKIP", "reason": "reminders off / customer not active"}); continue
        if kind == "C" and p["promise_date"] and p["promise_date"] >= iso:
            out.append({**item, "action": "SKIP", "reason": f"promised to pay by {p['promise_date']}"}); continue
        if kind == "C" and s["auto_apply_wallet"] == "1" and p["auto_debit_allowed"] and p["wallet_balance"] >= 1:
            out.append({**item, "action": "AUTO_DEBIT", "amount": round(min(p["wallet_balance"], total), 2)}); continue
        chans = []
        if ch_email and want_email and addr: chans.append("EMAIL")
        if ch_wa and want_wa and channels.normalize_phone(wa): chans.append("WHATSAPP")
        if not chans:
            out.append({**item, "action": "SKIP", "reason": "no enabled channel with contact details"}); continue
        col = "customer_id" if kind == "C" else "reseller_id"
        last = conn.execute(f"SELECT stage, sent_at FROM reminder_log WHERE {col}=? AND status='SENT' ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
        stage = stage_for(dpd, stages)
        if stage == 0:
            out.append({**item, "action": "SKIP", "reason": "below stage-1 threshold"}); continue
        if last:
            since = (today - datetime.date.fromisoformat(last["sent_at"][:10])).days
            if since < int(s["min_gap_days"]):
                out.append({**item, "action": "SKIP", "reason": f"contacted {since}d ago"}); continue
            if stage <= last["stage"] and not (stage == last_stage and since >= int(s["repeat_every_days"])):
                out.append({**item, "action": "SKIP", "reason": f"stage {stage} already sent"}); continue
        out.append({**item, "action": "REMIND", "stage": stage, "channels": chans})
    return out

# ------------------------------------------------------------------ compose
def build_context(conn, party, today=None):
    today = today or datetime.date.today(); s = get_settings(conn)
    kind, pid = party.split(":"); pid = int(pid)
    if kind == "C":
        p = conn.execute("SELECT * FROM customers WHERE id=?", (pid,)).fetchone()
        invs = ledger.open_invoices(conn, pid, "CUSTOMER", include_disputed=False)
        terms = f"Net {p['credit_days']}"; contact = p["contact_person"] or f"{p['name']} team"
    else:
        p = conn.execute("SELECT * FROM resellers WHERE id=?", (pid,)).fetchone()
        invs = ledger.open_invoices_reseller(conn, pid, include_disputed=False)
        terms = "as agreed"; contact = f"{p['name']} team"
    total = sum(i["balance"] for i in invs)
    overdue = [i for i in invs if i["due_date"] < today.isoformat()]
    oldest = invs[0]["due_date"] if invs else ""
    days = (today - datetime.date.fromisoformat(overdue[0]["due_date"])).days if overdue else 0
    rows = ""
    for i in invs[:40]:
        d = (today - datetime.date.fromisoformat(i["due_date"])).days
        rows += (f"<tr>{'<td>'+str(escape(i['customer_name']))+'</td>' if kind=='R' else ''}"
                 f"<td>{escape(i['invoice_no'] or i['description'] or i['kind'])}</td><td>{i['period'][:7]}</td><td>{i['due_date']}</td>"
                 f"<td align='right'>{max(d,0)}</td><td align='right'>{inr(i['balance'])}</td></tr>")
    more = f"<tr><td colspan='6'>+ {len(invs)-40} more invoices</td></tr>" if len(invs) > 40 else ""
    table = Markup("<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-family:Arial;font-size:13px'>"
                   "<tr style='background:#f1f5f9'>" + ("<th>Customer</th>" if kind == "R" else "") +
                   "<th>Invoice</th><th>Period</th><th>Due date</th><th>Days overdue</th><th>Amount (Rs)</th></tr>" + rows + more + "</table>")
    return dict(customer=p["name"], contact=contact, total=inr(total), total_raw=round(total, 2), overdue=inr(sum(i["balance"] for i in overdue)),
                days=days, oldest_due=oldest, invoice_count=len(invs), credit_terms=terms, table=table, sender=s["sender_name"],
                bank_html=Markup(str(escape(s["bank_details"])).replace("\n", "<br>")), payment_link=s["payment_link"], is_reseller=kind == "R"), p, invs

def compose(conn, party, stage, channel, today=None):
    ctx, p, invs = build_context(conn, party, today)
    t = conn.execute("SELECT * FROM templates WHERE key=?", (f"{channel}:{min(stage,4)}",)).fetchone()
    kind = party[0]
    s = get_settings(conn)
    if channel == "EMAIL":
        to = _emails(p["email_to"] if kind == "C" else p["contact_emails"])
        cc = _emails(p["email_cc"] if kind == "C" else "", s["default_cc"])
        if stage >= int(s["escalate_stage"]) and kind == "C":
            k = json.loads(s.get("kam_emails") or "{}").get((p["kam"] or "").lower())
            if k: cc.append(k)
        return {"channel": "EMAIL", "to": to, "cc": [c for c in dict.fromkeys(cc) if c not in to], "subject": TEXT_ENV.from_string(t["subject"]).render(**ctx),
                "body": HTML_ENV.from_string(t["body"]).render(**ctx), "total": ctx["total_raw"]}
    ctx_t = {**ctx, "customer": ctx["customer"][:60]}
    return {"channel": "WHATSAPP", "to": [p["whatsapp"]], "cc": [], "subject": t["subject"],   # subject = approved template name
            "body": TEXT_ENV.from_string(t["body"]).render(**ctx_t), "total": ctx["total_raw"],
            "params": [ctx_t["customer"], ctx["total"], str(ctx["days"]), ctx["oldest_due"]]}

# ------------------------------------------------------------------ run
def _deliver(conn, s, mode, kind, pid, party, stage, ch, today=None):
    """Compose + send (or dry-run) one message on one channel and log it. Returns status SENT | DRY_RUN | FAILED."""
    m = compose(conn, party, stage, ch, today); status, err = "SENT", ""
    if mode != "LIVE":
        status = "DRY_RUN"
    else:
        try:
            if ch == "EMAIL":
                channels.send_email(s["sender_name"], s["sender_email"], m["to"], m["cc"], m["subject"], m["body"])
                time.sleep(float(s.get("send_delay_sec") or 0))
            else:
                channels.send_whatsapp(m["to"][0], m["subject"], m["params"])
        except Exception as e:
            status, err = "FAILED", str(e)[:300]
    col = "customer_id" if kind == "CUSTOMER" else "reseller_id"
    conn.execute(f"INSERT INTO reminder_log({col},stage,channel,sent_at,to_addr,cc_addr,outstanding,status,error,subject,body) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, stage, ch, now(), "; ".join(m["to"]), "; ".join(m["cc"]), m["total"], status, err, m["subject"], m["body"][:4000]))
    return status

def _mode(s, send_mode=None):
    return "DRY_RUN" if s["is_demo"] == "1" else (send_mode or s["send_mode"])    # demo data can never reach a real inbox

_KEY = {"SENT": "sent", "DRY_RUN": "dry_run", "FAILED": "failed"}

def run(conn, today=None, send_mode=None, limit=None):
    s = get_settings(conn); mode = _mode(s, send_mode)
    res = {"auto_applied": [], "sent": 0, "dry_run": 0, "failed": 0, "skipped": 0}
    if s["auto_apply_wallet"] == "1":
        res["auto_applied"] = ledger.run_auto_apply(conn)
    cap = limit or int(s.get("max_per_run") or 0) or 10**9; done = 0
    for p in sorted(plan(conn, today), key=lambda x: -x["total"]):
        if p["action"] != "REMIND":
            res["skipped"] += 1; continue
        if done >= cap:
            break
        done += 1
        for ch in p["channels"]:
            res[_KEY[_deliver(conn, s, mode, p["kind"], p["id"], p["party"], p["stage"], ch, today)]] += 1
        conn.commit()
    audit(conn, "reminder_run", json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}) + f" mode={mode}")
    conn.commit()
    return res

def send_now(conn, party, channels_wanted=None, today=None, send_mode=None):
    """Ad-hoc reminder for ONE party right now. Ignores the gap/stage rules (a human decided) but still respects holds, disputes and
    DRY_RUN / demo safety. Stage = what the ageing says (at least 1)."""
    s = get_settings(conn); mode = _mode(s, send_mode); kind_c, pid = party.split(":"); pid = int(pid)
    ctx, p, invs = build_context(conn, party, today)
    if not invs:
        raise ValueError("nothing chargeable to remind (no open invoices, or all are on hold / disputed)")
    stage = max(1, stage_for(ctx["days"], _stages(s)))
    have = {"EMAIL": bool(_emails(p["email_to"] if kind_c == "C" else p["contact_emails"])), "WHATSAPP": bool(channels.normalize_phone(p["whatsapp"]))}
    chans = [c for c in (channels_wanted or ["EMAIL", "WHATSAPP"]) if have.get(c)]
    if not chans:
        raise ValueError("no usable contact detail for the requested channel(s)")
    out = {}
    for ch in chans:
        out[ch] = _deliver(conn, s, mode, "CUSTOMER" if kind_c == "C" else "RESELLER", pid, party, stage, ch, today)
    audit(conn, "reminder_now", f"{party} stage={stage} {out}"); conn.commit()
    return {"stage": stage, "results": out}

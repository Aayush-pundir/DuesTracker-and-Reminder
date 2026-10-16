import datetime, io, os, tempfile, threading, time
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import openpyxl
from . import db, ledger, importers, reminders, zoho, channels, demo

app = FastAPI(title="Dues Control Room")
_lock = threading.RLock()
conn = db.connect(); db.init(conn)
STATIC = os.path.join(os.path.dirname(__file__), "static")
TODAY = lambda: datetime.date.today()

def rows(sql, *a):
    return [dict(r) for r in conn.execute(sql, a)]

def _save(f: UploadFile):
    suffix = os.path.splitext(f.filename or "")[1] or ".xlsx"
    t = tempfile.NamedTemporaryFile(delete=False, suffix=suffix); t.write(f.file.read()); t.close(); return t.name

def _err(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))

BAL = ledger.BAL

# ================================================================== dashboard
@app.get("/api/summary")
def summary():
    with _lock:
        t = TODAY(); iso = t.isoformat()
        inv = rows(f"""SELECT i.customer_id, c.name, c.kam, c.collection_mode, c.reseller_id, i.payer, i.due_date, i.kind, {BAL} bal
                      FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.status IN ('OPEN','PARTIAL')""")
        dpd = lambda r: (t - datetime.date.fromisoformat(r["due_date"])).days
        buckets = {"Not yet due": 0, "1-30 d": 0, "31-60 d": 0, "61-90 d": 0, "90+ d": 0}
        for r in inv:
            d = dpd(r); k = "Not yet due" if d <= 0 else "1-30 d" if d <= 30 else "31-60 d" if d <= 60 else "61-90 d" if d <= 90 else "90+ d"
            buckets[k] += r["bal"]
        def group(key):
            g = {}
            for r in inv: g[r[key] or "-"] = g.get(r[key] or "-", 0) + r["bal"]
            return sorted(({"name": k, "amount": round(v)} for k, v in g.items()), key=lambda x: -x["amount"])
        top = {}
        for r in inv: top[(r["customer_id"], r["name"])] = top.get((r["customer_id"], r["name"]), 0) + r["bal"]
        top = sorted(({"id": k[0], "name": k[1], "amount": round(v)} for k, v in top.items()), key=lambda x: -x["amount"])[:10]
        total = sum(r["bal"] for r in inv)
        billed90 = conn.execute("SELECT COALESCE(SUM(amount),0) FROM invoices WHERE status!='CANCELLED' AND invoice_date>=?", ((t - datetime.timedelta(days=90)).isoformat(),)).fetchone()[0]
        month = t.replace(day=1).isoformat()
        coll = rows("SELECT mode, ROUND(SUM(amount+tds_amount)) amount FROM payments WHERE received_on>=? AND status='ALLOCATED' AND mode NOT IN ('SNAPSHOT') GROUP BY mode", month)
        pl = reminders.plan(conn); s = db.get_settings(conn)
        return {"total": round(total), "overdue": round(sum(r["bal"] for r in inv if dpd(r) > 0)), "customers_with_dues": len({r["customer_id"] for r in inv}),
                "by_payer": [{"name": "Customers", "amount": round(sum(r["bal"] for r in inv if r["payer"] == "CUSTOMER"))},
                             {"name": "Resellers", "amount": round(sum(r["bal"] for r in inv if r["payer"] == "RESELLER"))}],
                "dso": round(total / billed90 * 90) if billed90 else None, "aging": [{"name": k, "amount": round(v)} for k, v in buckets.items()],
                "by_kam": group("kam"), "by_mode": group("collection_mode"), "by_kind": group("kind"), "top": top,
                "collected_this_month": coll, "wallet_held": round(conn.execute("SELECT COALESCE(SUM(wallet_balance),0) FROM customers").fetchone()[0]),
                "bank_to_review": conn.execute("SELECT COUNT(*) FROM payments WHERE status IN ('UNMATCHED','SUGGESTED')").fetchone()[0],
                "plan": {"remind": sum(p["action"] == "REMIND" for p in pl), "auto_debit": sum(p["action"] == "AUTO_DEBIT" for p in pl), "skip": sum(p["action"] == "SKIP" for p in pl)},
                "send_mode": "DRY_RUN" if s["is_demo"] == "1" else s["send_mode"], "is_demo": s["is_demo"] == "1", "migration_done": s["migration_done"] == "1",
                "empty": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0}

@app.get("/api/today")
def today_worklist():
    """What a human actually has to look at - everything else is automatic."""
    with _lock:
        iso = TODAY().isoformat(); soon = (TODAY() + datetime.timedelta(days=3)).isoformat()
        base = f"""SELECT c.id, c.name, c.kam, c.promise_date, c.promise_amount, c.promise_note,
                   (SELECT COALESCE(SUM({BAL}),0) FROM invoices i WHERE i.customer_id=c.id AND i.status IN ('OPEN','PARTIAL') AND i.payer='CUSTOMER') AS due FROM customers c"""
        broken = rows(base + " WHERE c.promise_date IS NOT NULL AND c.promise_date<? AND due>1 ORDER BY due DESC", iso)
        upcoming = rows(base + " WHERE c.promise_date>=? AND c.promise_date<=? ORDER BY c.promise_date", iso, soon)
        no_contact = rows(base + " WHERE c.status='ACTIVE' AND c.remind=1 AND TRIM(c.email_to)='' AND TRIM(c.whatsapp)='' AND due>=100 ORDER BY due DESC LIMIT 30")
        no_split = rows(f"""SELECT c.id,c.name,r.name reseller FROM customers c JOIN resellers r ON r.id=c.reseller_id
                            WHERE c.reseller_share_pct=0 AND EXISTS (SELECT 1 FROM invoices i WHERE i.customer_id=c.id AND i.source!='MIGRATION') ORDER BY c.name LIMIT 50""")
        disputes = rows(f"""SELECT i.id, c.id customer_id, c.name, i.invoice_no, i.dispute_note, {BAL} bal FROM invoices i JOIN customers c ON c.id=i.customer_id
                            WHERE i.disputed=1 AND i.status IN ('OPEN','PARTIAL')""")
        plan = reminders.plan(conn)
        held = rows(f"""SELECT COUNT(*) n, COALESCE(SUM({BAL}),0) amt FROM invoices i WHERE i.hold=1 AND i.status IN ('OPEN','PARTIAL')""")[0]
        ld = conn.execute("SELECT MAX(received_on) FROM payments WHERE mode IN ('WALLET','AUTO_DEBIT') AND status='ALLOCATED'").fetchone()[0]
        return {"held": held, "last_deduction": ld, "deduction_age_days": (TODAY() - datetime.date.fromisoformat(ld)).days if ld else None,
                "broken_promises": broken, "promises_soon": upcoming, "bank_review": rows("SELECT id,received_on,amount,narration FROM payments WHERE status IN ('UNMATCHED','SUGGESTED') ORDER BY received_on DESC LIMIT 20"),
                "to_remind": [p for p in plan if p["action"] == "REMIND"][:50], "n_remind": sum(p["action"] == "REMIND" for p in plan),
                "no_contact": no_contact, "reseller_no_split": no_split, "disputes": disputes}

# ================================================================== customers
@app.get("/api/lookup")
def lookup(q: str = ""):
    with _lock:
        c = rows("SELECT 'C:'||id party, name, 'Customer' kind FROM customers WHERE name LIKE ? ORDER BY name LIMIT 12", f"%{q}%")
        r = rows("SELECT 'R:'||id party, name, 'Reseller' kind FROM resellers WHERE name LIKE ? LIMIT 4", f"%{q}%")
        return r + c

@app.get("/api/customers")
def customers(q: str = "", kam: str = "", reseller_id: int = 0, status: str = "dues", limit: int = 400):
    with _lock:
        t = TODAY().isoformat(); out = []
        for c in rows("""SELECT c.*, r.name reseller FROM customers c LEFT JOIN resellers r ON r.id=c.reseller_id
                         WHERE c.name LIKE ? AND (?='' OR c.kam=?) AND (?=0 OR c.reseller_id=?)""", f"%{q}%", kam, kam, reseller_id, reseller_id):
            invs = ledger.open_invoices(conn, c["id"], None)
            cust = [i for i in invs if i["payer"] == "CUSTOMER"]; res = [i for i in invs if i["payer"] == "RESELLER"]
            c["due_customer"] = round(sum(i["balance"] for i in cust)); c["due_reseller"] = round(sum(i["balance"] for i in res))
            c["outstanding"] = c["due_customer"] + c["due_reseller"]
            c["overdue"] = round(sum(i["balance"] for i in invs if i["due_date"] < t))
            c["oldest_due"] = min((i["due_date"] for i in invs), default=None)
            c["last_reminder"] = (conn.execute("SELECT stage||' · '||channel||' · '||substr(sent_at,1,10) FROM reminder_log WHERE customer_id=? AND status='SENT' ORDER BY id DESC LIMIT 1", (c["id"],)).fetchone() or [None])[0]
            if status == "all" or c["outstanding"] or (status == "dues" and False):
                out.append(c)
        return sorted(out, key=lambda x: -x["outstanding"])[:limit]

class CustomerIn(BaseModel):
    name: str | None = None; kam: str | None = None; contact_person: str | None = None; email_to: str | None = None; email_cc: str | None = None
    whatsapp: str | None = None; reseller_id: int | None = None; reseller_share_pct: float | None = None; credit_days: int | None = None
    collection_mode: str | None = None; auto_debit_allowed: int | None = None; remind: int | None = None; channel_email: int | None = None
    channel_whatsapp: int | None = None; status: str | None = None; notes: str | None = None; wallet_balance: float | None = None

def _check(p: CustomerIn):
    d = p.model_dump(exclude_none=True)
    if "reseller_share_pct" in d and not 0 <= d["reseller_share_pct"] <= 100: raise HTTPException(400, "reseller share must be 0-100%")
    if "credit_days" in d and not 0 <= d["credit_days"] <= 365: raise HTTPException(400, "credit days must be 0-365")
    if d.get("status") and d["status"] not in ("ACTIVE", "FROZEN", "STOPPED"): raise HTTPException(400, "bad status")
    if d.get("collection_mode") and d["collection_mode"] not in ("BANK", "WALLET", "AUTO_DEBIT"): raise HTTPException(400, "bad collection mode")
    return d

@app.post("/api/customers")
def create_customer(p: CustomerIn):
    with _lock:
        d = _check(p)
        if not d.get("name"): raise HTTPException(400, "name required")
        d.setdefault("credit_days", int(db.get_settings(conn)["default_credit_days"]))
        try:
            cid = conn.execute(f"INSERT INTO customers({','.join(d)}) VALUES({','.join('?'*len(d))})", list(d.values())).lastrowid
        except Exception as e:
            raise HTTPException(400, "customer already exists" if "UNIQUE" in str(e) else str(e))
        db.audit(conn, "customer_create", d["name"]); conn.commit(); return {"id": cid}

@app.patch("/api/customers/{cid}")
def patch_customer(cid: int, p: CustomerIn):
    with _lock:
        d = _check(p)
        if d:
            conn.execute("UPDATE customers SET " + ",".join(f"{k}=?" for k in d) + " WHERE id=?", (*d.values(), cid))   # keys come from the model, not the client
            if "credit_days" in d: ledger.recompute_due_dates(conn, cid)
            db.audit(conn, "customer_update", f"{cid} {d}")
        conn.commit(); return {"ok": True}

@app.get("/api/customers/{cid}")
def customer(cid: int):
    with _lock:
        c = conn.execute("SELECT c.*, r.name reseller FROM customers c LEFT JOIN resellers r ON r.id=c.reseller_id WHERE c.id=?", (cid,)).fetchone()
        if not c: raise HTTPException(404)
        pays = rows("SELECT p.* FROM payments p WHERE p.customer_id=? ORDER BY received_on DESC, id DESC LIMIT 100", cid)
        for p in pays: p["unapplied"] = ledger.unapplied(conn, p["id"])
        return {"customer": dict(c), "unapplied_credit": round(sum(p["unapplied"] for p in pays if p["unapplied"] > 0)),
                "invoices": rows(f"SELECT i.*, {BAL} balance FROM invoices i WHERE customer_id=? ORDER BY invoice_date DESC, id DESC LIMIT 300", cid),
                "payments": pays, "notes": rows("SELECT * FROM notes WHERE customer_id=? ORDER BY id DESC LIMIT 50", cid),
                "reminders": rows("SELECT id,stage,channel,sent_at,status,outstanding,error FROM reminder_log WHERE customer_id=? ORDER BY id DESC LIMIT 30", cid)}

class NoteIn(BaseModel):
    text: str; kind: str = "NOTE"; author: str = ""

@app.post("/api/customers/{cid}/notes")
def add_note(cid: int, n: NoteIn):
    with _lock:
        conn.execute("INSERT INTO notes(customer_id,at,author,kind,text) VALUES(?,?,?,?,?)", (cid, db.now(), n.author, n.kind, n.text)); conn.commit(); return {"ok": True}

class PromiseIn(BaseModel):
    date: str | None = None; amount: float | None = None; note: str = ""

@app.post("/api/customers/{cid}/promise")
def promise(cid: int, p: PromiseIn):
    """Promise-to-pay: chasing pauses until the date; if it passes unpaid it appears in 'broken promises' on the Today screen."""
    with _lock:
        conn.execute("UPDATE customers SET promise_date=?, promise_amount=?, promise_note=? WHERE id=?", (p.date or None, p.amount, p.note, cid))
        if p.date:
            conn.execute("INSERT INTO notes(customer_id,at,kind,text) VALUES(?,?,?,?)", (cid, db.now(), "PROMISE", f"Promised {p.amount or ''} by {p.date}. {p.note}"))
        conn.commit(); return {"ok": True}

@app.get("/api/customers/{cid}/statement.xlsx")
def statement(cid: int):
    with _lock:
        c = conn.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        if not c: raise HTTPException(404)
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Statement"
        ws.append([f"Statement of account - {c['name']}"]); ws.append([f"As of {TODAY().isoformat()} | Payment terms: Net {c['credit_days']}"]); ws.append([])
        ws.append(["Invoice date", "Invoice no", "Description", "Due date", "Payer", "Invoice amount", "Settled", "Balance", "Status", "Days overdue"])
        for i in rows(f"SELECT i.*, {BAL} bal FROM invoices i WHERE customer_id=? AND status!='CANCELLED' ORDER BY invoice_date", cid):
            od = max((TODAY() - datetime.date.fromisoformat(i["due_date"])).days, 0) if i["bal"] > 0.5 else 0
            ws.append([i["invoice_date"], i["invoice_no"], i["description"], i["due_date"], i["payer"].title(), i["amount"], round(i["amount"] - i["bal"], 2), round(i["bal"], 2), i["status"], od])
        ws.append([]); ws.append(["Receipts"]); ws.append(["Date", "Mode", "Amount", "TDS", "Reference"])
        for p in rows("SELECT * FROM payments WHERE customer_id=? ORDER BY received_on", cid):
            ws.append([p["received_on"], p["mode"], p["amount"], p["tds_amount"], p["reference"]])
        b = io.BytesIO(); wb.save(b)
        return Response(b.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="statement_{cid}.xlsx"'})

# ================================================================== invoices (the source of truth)
@app.get("/api/invoices")
def invoices(q: str = "", status: str = "", payer: str = "", kind: str = "", date_from: str = "", date_to: str = "", limit: int = 300):
    with _lock:
        iso = TODAY().isoformat(); w, a = ["1=1"], []
        if q: w.append("(c.name LIKE ? OR i.invoice_no LIKE ? OR i.description LIKE ?)"); a += [f"%{q}%"] * 3
        if status == "overdue": w.append("i.status IN ('OPEN','PARTIAL') AND i.due_date<?"); a.append(iso)
        elif status == "held": w.append("i.hold=1 AND i.status IN ('OPEN','PARTIAL')")
        elif status: w.append("i.status=?"); a.append(status)
        if payer: w.append("i.payer=?"); a.append(payer)
        if kind: w.append("i.kind=?"); a.append(kind)
        if date_from: w.append("i.invoice_date>=?"); a.append(date_from)
        if date_to: w.append("i.invoice_date<=?"); a.append(date_to)
        return rows(f"""SELECT i.*, c.name customer, c.credit_days, {BAL} balance FROM invoices i JOIN customers c ON c.id=i.customer_id
                        WHERE {' AND '.join(w)} ORDER BY i.invoice_date DESC, i.id DESC LIMIT ?""", *a, limit)

class InvoiceIn(BaseModel):
    customer_id: int; amount: float; invoice_date: str | None = None; invoice_no: str = ""; description: str = ""; kind: str = "OTHER"

@app.post("/api/invoices")
def add_invoice(b: InvoiceIn):
    with _lock:
        ids = _err(lambda: ledger.create_invoice(conn, b.customer_id, b.amount, b.invoice_date, b.kind, b.invoice_no, b.description))
        return {"ids": ids, "split": len(ids) > 1}

class InvoicePatch(BaseModel):
    amount: float | None = None; invoice_date: str | None = None; description: str | None = None; invoice_no: str | None = None; due_date: str | None = None

@app.patch("/api/invoices/{iid}")
def edit_invoice(iid: int, b: InvoicePatch):
    with _lock:
        _err(lambda: ledger.update_invoice(conn, iid, **b.model_dump(exclude_none=True))); return {"ok": True}

class Reason(BaseModel):
    reason: str = ""; disputed: bool = True

@app.post("/api/invoices/{iid}/cancel")
def cancel(iid: int, b: Reason):
    with _lock:
        _err(lambda: ledger.cancel_invoice(conn, iid, b.reason)); return {"ok": True}

@app.post("/api/invoices/{iid}/restore")
def restore(iid: int):
    with _lock:
        ledger.restore_invoice(conn, iid); return {"ok": True}

class ReleaseIn(BaseModel):
    source: str | None = None; date_from: str | None = None; date_to: str | None = None; invoice_ids: list[int] | None = None

@app.post("/api/invoices/release")
def release(b: ReleaseIn):
    with _lock: return {"released": ledger.release_hold(conn, b.source, b.date_from, b.date_to, b.invoice_ids)}

@app.post("/api/invoices/{iid}/dispute")
def dispute(iid: int, b: Reason):
    with _lock:
        ledger.set_dispute(conn, iid, b.disputed, b.reason); return {"ok": True}

# ================================================================== collections
@app.get("/api/payments")
def payments(q: str = "", mode: str = "", limit: int = 300):
    with _lock:
        return rows("""SELECT p.*, COALESCE(c.name, r.name) party_name, CASE WHEN p.reseller_id IS NOT NULL THEN 'Reseller' ELSE 'Customer' END party_kind
                       FROM payments p LEFT JOIN customers c ON c.id=p.customer_id LEFT JOIN resellers r ON r.id=p.reseller_id
                       WHERE p.status='ALLOCATED' AND (?='' OR p.mode=?) AND (COALESCE(c.name,r.name,'') LIKE ? OR p.reference LIKE ?)
                       ORDER BY p.received_on DESC, p.id DESC LIMIT ?""", mode, mode, f"%{q}%", f"%{q}%", limit)

class PaymentIn(BaseModel):
    party: str                      # "C:12" or "R:3"
    amount: float; mode: str = "BANK"; received_on: str | None = None; reference: str = ""; tds: float = 0; invoice_ids: list[int] | None = None
    narration: str = ""

@app.post("/api/payments")
def add_payment(p: PaymentIn):
    with _lock:
        if p.mode not in ("BANK", "WALLET", "AUTO_DEBIT", "CREDIT_NOTE"): raise HTTPException(400, "bad mode")
        kind, pid = p.party.split(":")
        r = _err(lambda: ledger.record_payment(conn, int(pid) if kind == "C" else None, p.amount, p.mode, p.received_on, p.reference, p.narration,
                                                p.invoice_ids, int(pid) if kind == "R" else None, p.tds))
        return {"payment_id": r[0], "allocated": r[1], "unapplied": r[2]}

@app.post("/api/payments/{pid}/reverse")
def reverse(pid: int):
    with _lock: ledger.reverse_payment(conn, pid); return {"ok": True}

@app.get("/api/bank/review")
def bank_review():
    with _lock:
        out = rows("SELECT * FROM payments WHERE status IN ('UNMATCHED','SUGGESTED') ORDER BY received_on DESC")
        for p in out:
            sp = p["suggested_party"]
            if sp:
                k, i = sp.split(":"); p["suggested"] = (conn.execute(f"SELECT name FROM {'customers' if k=='C' else 'resellers'} WHERE id=?", (i,)).fetchone() or ["?"])[0]
        return out

class Confirm(BaseModel):
    party: str; tds: float = 0

@app.post("/api/payments/{pid}/confirm")
def confirm(pid: int, b: Confirm):
    with _lock:
        d, l = ledger.confirm_bank_payment(conn, pid, b.party, b.tds); return {"allocated": d, "advance": l}

@app.delete("/api/payments/{pid}")
def discard_unmatched(pid: int):
    with _lock:
        conn.execute("DELETE FROM payments WHERE id=? AND status IN ('UNMATCHED','SUGGESTED')", (pid,)); conn.commit(); return {"ok": True}

class Amt(BaseModel):
    amount: float | None = None

@app.post("/api/customers/{cid}/wallet/set")
def wallet_set(cid: int, b: Amt):
    with _lock: ledger.wallet_set(conn, cid, b.amount or 0); return {"ok": True}

@app.post("/api/customers/{cid}/wallet/apply")
def wallet_apply(cid: int, b: Amt):
    with _lock: return {"applied": ledger.apply_wallet(conn, cid, b.amount)}

# ================================================================== uploads (bulk) - validate first, then apply
def _bulk(kind, file, apply, **kw):
    with _lock:
        rows_ = _err(lambda: importers.read_table(_save(file)))
        fn = {"invoices": importers.bulk_invoices, "collections": importers.bulk_collections, "customers": importers.bulk_customers}[kind]
        return fn(conn, rows_, apply=apply, **kw)

@app.post("/api/upload/invoices")
def up_invoices(file: UploadFile = File(...), apply: bool = False): return _bulk("invoices", file, apply)

@app.post("/api/upload/collections")
def up_collections(file: UploadFile = File(...), apply: bool = False, reduce_wallet: bool = False): return _bulk("collections", file, apply, reduce_wallet=reduce_wallet)

@app.post("/api/upload/deductions")
def up_deductions(file: UploadFile = File(...), apply: bool = False, reduce_wallet: bool = False):
    """Fortnightly/monthly file of balances deducted on the ops platform (the platform is not connected to this app)."""
    return _bulk("collections", file, apply, reduce_wallet=reduce_wallet, default_mode="WALLET")

@app.post("/api/upload/customers")
def up_customers(file: UploadFile = File(...), apply: bool = False): return _bulk("customers", file, apply)

@app.post("/api/upload/bank")
def up_bank(file: UploadFile = File(...)):
    with _lock:
        return ledger.import_bank_rows(conn, _err(lambda: importers.read_bank_statement(_save(file))))

@app.get("/api/templates/{kind}.xlsx")
def template(kind: str):
    if kind not in importers.TEMPLATES: raise HTTPException(404)
    return Response(importers.template_workbook(kind), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{kind}_template.xlsx"'})

# ================================================================== resellers
@app.get("/api/resellers")
def resellers():
    with _lock:
        out = rows("SELECT * FROM resellers ORDER BY name"); t = TODAY().isoformat()
        for r in out:
            inv = ledger.open_invoices_reseller(conn, r["id"])
            r["due"] = round(sum(i["balance"] for i in inv)); r["overdue"] = round(sum(i["balance"] for i in inv if i["due_date"] < t))
            r["customers"] = conn.execute("SELECT COUNT(*) FROM customers WHERE reseller_id=?", (r["id"],)).fetchone()[0]
            r["lines"] = [{"customer": i["customer_name"], "invoice": i["invoice_no"], "due_date": i["due_date"], "balance": round(i["balance"])} for i in inv[:60]]
            r["avg_share"] = (conn.execute("SELECT AVG(reseller_share_pct) FROM customers WHERE reseller_id=?", (r["id"],)).fetchone()[0] or 0)
        return out

class ResellerPatch(BaseModel):
    contact_emails: str | None = None; whatsapp: str | None = None; remind_reseller: int | None = None; notes: str | None = None

@app.patch("/api/resellers/{rid}")
def patch_reseller(rid: int, p: ResellerPatch):
    with _lock:
        d = p.model_dump(exclude_none=True)
        if d: conn.execute("UPDATE resellers SET " + ",".join(f"{k}=?" for k in d) + " WHERE id=?", (*d.values(), rid)); conn.commit()
        return {"ok": True}

# ================================================================== reminders
@app.get("/api/reminders/plan")
def rplan():
    with _lock: return [p for p in reminders.plan(conn) if p["action"] != "SKIP"]

class RunIn(BaseModel):
    mode: str | None = None; limit: int | None = None

@app.post("/api/reminders/run")
def rrun(b: RunIn):
    with _lock: return reminders.run(conn, send_mode=b.mode, limit=b.limit)

@app.get("/api/reminders/preview")
def rpreview(party: str, channel: str = "EMAIL", stage: int = 0):
    with _lock:
        if not stage:
            p = next((x for x in reminders.plan(conn) if x["party"] == party and x["action"] == "REMIND"), None); stage = p["stage"] if p else 1
        return _err(lambda: {**reminders.compose(conn, party, stage, channel), "stage": stage})

@app.get("/api/reminders/log")
def rlog(limit: int = 200):
    with _lock:
        return rows("""SELECT l.id,l.stage,l.channel,l.sent_at,l.to_addr,l.outstanding,l.status,l.error,l.subject, COALESCE(c.name,r.name) party
                       FROM reminder_log l LEFT JOIN customers c ON c.id=l.customer_id LEFT JOIN resellers r ON r.id=l.reseller_id ORDER BY l.id DESC LIMIT ?""", limit)

@app.get("/api/reminders/templates")
def get_templates():
    with _lock: return rows("SELECT * FROM templates ORDER BY channel, stage")

class TemplateIn(BaseModel):
    subject: str; body: str

@app.put("/api/reminders/templates/{key}")
def put_template(key: str, b: TemplateIn):
    with _lock:
        t = conn.execute("SELECT * FROM templates WHERE key=?", (key,)).fetchone()
        if not t: raise HTTPException(404)
        party = conn.execute("SELECT 'C:'||id FROM customers LIMIT 1").fetchone()
        if party:                                         # refuse templates that do not render
            old = (t["subject"], t["body"])
            conn.execute("UPDATE templates SET subject=?, body=? WHERE key=?", (b.subject, b.body, key))
            try: reminders.compose(conn, party[0], t["stage"], t["channel"])
            except Exception as e:
                conn.execute("UPDATE templates SET subject=?, body=? WHERE key=?", (*old, key)); conn.commit(); raise HTTPException(400, f"template error: {e}")
        conn.execute("UPDATE templates SET subject=?, body=?, updated_at=? WHERE key=?", (b.subject, b.body, db.now(), key)); conn.commit(); return {"ok": True}

@app.get("/api/channels/status")
def channel_status(): return channels.status()

class TestSend(BaseModel):
    channel: str = "EMAIL"; to: str

@app.post("/api/channels/test")
def test_send(b: TestSend):
    s = db.get_settings(conn)
    if s["is_demo"] == "1": raise HTTPException(400, "disabled while demo data is loaded")
    try:
        if b.channel == "EMAIL":
            channels.send_email(s["sender_name"], s["sender_email"], [b.to], [], "Test from Dues Control Room", "<p>This is a test message. Your mailbox integration works.</p>")
        else:
            channels.send_whatsapp(b.to, "dues_reminder_1", ["Test Customer", "1,000", "7", "2026-01-01"])
    except Exception as e:
        raise HTTPException(400, str(e)[:300])
    return {"ok": True}

# ================================================================== zoho
class ZohoPeriod(BaseModel):
    start: str; end: str; create_customers: bool = True; include_paid: bool = True; invoice_ids: list[str] | None = None; hold_open: bool = True

@app.post("/api/zoho/preview")
def zoho_preview(b: ZohoPeriod):
    try:
        z = zoho.Zoho()
    except zoho.ZohoNotConfigured as e:
        raise HTTPException(400, f"Zoho not configured: {e}")
    with _lock:
        try: return zoho.preview(conn, z.invoices(b.start, b.end))
        except Exception as e: raise HTTPException(502, f"Zoho error: {str(e)[:200]}")

@app.post("/api/zoho/import")
def zoho_import(b: ZohoPeriod):
    try:
        z = zoho.Zoho()
    except zoho.ZohoNotConfigured as e:
        raise HTTPException(400, f"Zoho not configured: {e}")
    with _lock:
        try: invs = z.invoices(b.start, b.end)
        except Exception as e: raise HTTPException(502, f"Zoho error: {str(e)[:200]}")
        return zoho.import_invoices(conn, invs, b.create_customers, b.include_paid, set(b.invoice_ids) if b.invoice_ids else None, b.hold_open)

@app.post("/api/zoho/file")
def zoho_file(file: UploadFile = File(...), apply: bool = False, create_customers: bool = True, include_paid: bool = True, hold_open: bool = True):
    """Import from Zoho Books' own export (Invoices -> Export as CSV/XLSX). No API credentials needed."""
    with _lock:
        invs = zoho.invoices_from_rows(_err(lambda: importers.read_table(_save(file))))
        if not invs: raise HTTPException(400, "no invoices found - export Invoices from Zoho Books (needs Invoice Number, Invoice Date, Customer Name, Total)")
        if not apply:
            pv = zoho.preview(conn, invs); c = {}
            for r in pv: c[r["import_status"]] = c.get(r["import_status"], 0) + 1
            return {"total": len(pv), "by_status": c, "sample": pv[:8]}
        return zoho.import_invoices(conn, invs, create_customers, include_paid, None, hold_open)

# ================================================================== migration (one time) / demo / settings
@app.get("/api/migration/status")
def mig_status():
    s = db.get_settings(conn)
    return {"done": s["migration_done"] == "1", "is_demo": s["is_demo"] == "1", "customers": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]}

def _mig_guard():
    s = db.get_settings(conn)
    if s["migration_done"] == "1":
        raise HTTPException(409, "Initial migration is finished and locked - the app is now the source of truth. Use the bulk upload tools instead.")
    if s["is_demo"] == "1":
        demo.reset(conn)

@app.post("/api/migration/consol")
def mig_consol(file: UploadFile = File(...)):
    with _lock:
        _mig_guard(); return _err(lambda: importers.import_consol(conn, _save(file)))

@app.post("/api/migration/contacts")
def mig_contacts(file: UploadFile = File(...)):
    with _lock:
        _mig_guard(); return _err(lambda: importers.import_contacts(conn, _save(file)))

@app.post("/api/migration/finish")
def mig_finish():
    with _lock:
        db.set_setting(conn, "migration_done", "1"); db.audit(conn, "migration_finished"); conn.commit(); return {"ok": True}

@app.post("/api/demo/load")
def demo_load():
    with _lock:
        if db.get_settings(conn)["migration_done"] == "1": raise HTTPException(409, "real data is live - demo disabled")
        if conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] and db.get_settings(conn)["is_demo"] != "1":
            raise HTTPException(409, "database already holds real data")
        return demo.load(conn)

@app.post("/api/demo/reset")
def demo_reset():
    with _lock:
        if db.get_settings(conn)["is_demo"] != "1": raise HTTPException(409, "not in demo mode")
        demo.reset(conn); return {"ok": True}

@app.get("/api/demo/bank_sample.csv")
def demo_bank():
    with _lock: return Response(demo.sample_bank_csv(conn), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="sample_bank_statement.csv"'})

@app.get("/api/settings")
def get_settings(): return db.get_settings(conn)

@app.put("/api/settings")
def put_settings(b: dict):
    with _lock:
        for k, v in b.items():
            if k in db.DEFAULT_SETTINGS and k not in ("migration_done", "is_demo"): db.set_setting(conn, k, v)
        conn.commit(); db.audit(conn, "settings", ",".join(b)); return db.get_settings(conn)

@app.get("/api/audit")
def audit_log(): return rows("SELECT * FROM audit ORDER BY id DESC LIMIT 200")

def _daily_loop():
    """Built-in scheduler: once a day after 09:30 - reminders (and optional wallet auto-apply). Or run `python -m app.jobs` from cron."""
    c = db.connect()
    while True:
        try:
            n = datetime.datetime.now()
            if n.hour * 60 + n.minute >= 9 * 60 + 30 and db.get_settings(c).get("last_auto_run") != n.date().isoformat():
                with _lock:
                    reminders.run(c); db.set_setting(c, "last_auto_run", n.date().isoformat()); c.commit()
        except Exception as e:
            print("scheduler error", e)
        time.sleep(300)

if os.environ.get("DUES_SCHEDULER", "1") == "1":
    threading.Thread(target=_daily_loop, daemon=True).start()

@app.get("/")
def index(): return FileResponse(os.path.join(STATIC, "index.html"))
app.mount("/static", StaticFiles(directory=STATIC), name="static")

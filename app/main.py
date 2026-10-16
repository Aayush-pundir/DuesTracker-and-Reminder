import datetime, os, tempfile, threading, time
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import db, ledger, importers, reminders, zoho

app = FastAPI(title="Dues Tracker & Reminder")
_lock = threading.RLock()
conn = db.connect(); db.init(conn)
STATIC = os.path.join(os.path.dirname(__file__), "static")

def rows(sql, *a):
    return [dict(r) for r in conn.execute(sql, a)]

def _upload(f: UploadFile, suffix):
    t = tempfile.NamedTemporaryFile(delete=False, suffix=suffix); t.write(f.file.read()); t.close(); return t.name

# ---------------------------------------------------------------- dashboard
@app.get("/api/summary")
def summary():
    with _lock:
        today = datetime.date.today().isoformat()
        inv = rows("""SELECT i.customer_id, c.name, c.kam, c.collection_mode, c.reseller_id, i.due_date, i.kind,
                      i.amount - COALESCE((SELECT SUM(amount) FROM allocations WHERE invoice_id=i.id),0) bal
                      FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.status IN ('OPEN','PARTIAL')""")
        def dpd(r): return (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(r["due_date"])).days
        buckets = {"Not yet due": 0, "1-30 d": 0, "31-90 d": 0, "91-180 d": 0, "180+ d": 0}
        for r in inv:
            d = dpd(r); k = "Not yet due" if d <= 0 else "1-30 d" if d <= 30 else "31-90 d" if d <= 90 else "91-180 d" if d <= 180 else "180+ d"
            buckets[k] += r["bal"]
        def group(key):
            g = {}
            for r in inv: g[r[key] or "-"] = g.get(r[key] or "-", 0) + r["bal"]
            return sorted(({"name": k, "amount": round(v)} for k, v in g.items()), key=lambda x: -x["amount"])
        top = {}
        for r in inv: top[(r["customer_id"], r["name"])] = top.get((r["customer_id"], r["name"]), 0) + r["bal"]
        top = sorted(({"id": k[0], "name": k[1], "amount": round(v)} for k, v in top.items()), key=lambda x: -x["amount"])[:10]
        resellers = rows("SELECT id,name FROM resellers")
        for rs in resellers:
            rs["amount"] = round(sum(r["bal"] for r in inv if r["reseller_id"] == rs["id"])); rs["customers"] = len({r["customer_id"] for r in inv if r["reseller_id"] == rs["id"]})
        month = datetime.date.today().replace(day=1).isoformat()
        coll = rows("SELECT mode, ROUND(SUM(amount)) amount FROM payments WHERE received_on>=? AND status='ALLOCATED' AND mode!='SNAPSHOT' GROUP BY mode", month)
        pl = reminders.plan(conn)
        return {"total": round(sum(r["bal"] for r in inv)), "overdue": round(sum(r["bal"] for r in inv if dpd(r) > 0)),
                "customers_with_dues": len({r["customer_id"] for r in inv}), "aging": [{"name": k, "amount": round(v)} for k, v in buckets.items()],
                "by_kam": group("kam"), "by_mode": group("collection_mode"), "by_kind": group("kind"), "top": top, "resellers": resellers,
                "collected_this_month": coll, "bank_to_review": conn.execute("SELECT COUNT(*) FROM payments WHERE status IN ('UNMATCHED','SUGGESTED')").fetchone()[0],
                "wallet_held": round(conn.execute("SELECT COALESCE(SUM(wallet_balance),0) FROM customers").fetchone()[0]),
                "plan": {"remind": sum(p["action"] == "REMIND" for p in pl), "auto_debit": sum(p["action"] == "AUTO_DEBIT" for p in pl),
                         "skip": sum(p["action"] == "SKIP" for p in pl)}, "send_mode": db.get_settings(conn)["send_mode"]}

@app.get("/api/customers")
def customers(q: str = "", kam: str = "", reseller_id: int = 0, only_dues: bool = True):
    with _lock:
        out = []
        for c in rows("SELECT c.*, r.name reseller FROM customers c LEFT JOIN resellers r ON r.id=c.reseller_id WHERE c.name LIKE ? AND (?='' OR c.kam=?) AND (?=0 OR c.reseller_id=?)",
                      f"%{q}%", kam, kam, reseller_id, reseller_id):
            invs = ledger.open_invoices(conn, c["id"]); c["outstanding"] = round(sum(i["balance"] for i in invs))
            c["oldest_due"] = invs[0]["due_date"] if invs else None
            c["last_reminder"] = (conn.execute("SELECT stage||' @ '||substr(sent_at,1,10) FROM reminder_log WHERE customer_id=? AND status='SENT' ORDER BY id DESC LIMIT 1", (c["id"],)).fetchone() or [None])[0]
            if c["outstanding"] or not only_dues: out.append(c)
        return sorted(out, key=lambda x: -x["outstanding"])[:500]

@app.get("/api/customers/{cid}")
def customer(cid: int):
    with _lock:
        c = conn.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        if not c: raise HTTPException(404)
        return {"customer": dict(c), "invoices": [dict(r) for r in conn.execute(
                    "SELECT i.*, i.amount-COALESCE((SELECT SUM(amount) FROM allocations WHERE invoice_id=i.id),0) balance FROM invoices i WHERE customer_id=? ORDER BY period DESC LIMIT 200", (cid,))],
                "payments": rows("SELECT * FROM payments WHERE customer_id=? ORDER BY id DESC LIMIT 100", cid),
                "reminders": rows("SELECT * FROM reminder_log WHERE customer_id=? ORDER BY id DESC LIMIT 50", cid)}

class CustomerPatch(BaseModel):
    kam: str | None = None; email_to: str | None = None; email_cc: str | None = None; reseller_id: int | None = None
    collection_mode: str | None = None; auto_debit_allowed: int | None = None; remind: int | None = None; status: str | None = None; notes: str | None = None

@app.patch("/api/customers/{cid}")
def patch_customer(cid: int, p: CustomerPatch):
    with _lock:
        for k, v in p.model_dump(exclude_none=True).items():
            conn.execute(f"UPDATE customers SET {k}=? WHERE id=?", (v, cid))   # keys come from the pydantic model, not the client
        db.audit(conn, "customer_update", f"{cid} {p.model_dump(exclude_none=True)}"); conn.commit(); return {"ok": True}

# ---------------------------------------------------------------- data in: bulk + one-on-one
@app.post("/api/upload/consol")
def up_consol(file: UploadFile = File(...)):
    with _lock: return importers.import_consol(conn, _upload(file, ".xlsx"))

@app.post("/api/upload/contacts")
def up_contacts(file: UploadFile = File(...)):
    with _lock: return importers.import_contacts(conn, _upload(file, ".xlsm"))

@app.post("/api/upload/bank")
def up_bank(file: UploadFile = File(...)):
    with _lock:
        suffix = ".csv" if (file.filename or "").lower().endswith(".csv") else ".xlsx"
        return ledger.import_bank_rows(conn, importers.read_bank_statement(_upload(file, suffix)))

class PaymentIn(BaseModel):
    customer_id: int; amount: float; mode: str = "BANK"; received_on: str | None = None; reference: str = ""; invoice_ids: list[int] | None = None

@app.post("/api/payments")
def add_payment(p: PaymentIn):
    with _lock:
        if p.mode not in ("BANK", "CREDIT_NOTE"): raise HTTPException(400, "use wallet endpoints for WALLET / AUTO_DEBIT")
        pid, done, left = ledger.record_payment(conn, p.customer_id, p.amount, p.mode, p.received_on, p.reference, invoice_ids=p.invoice_ids)
        return {"payment_id": pid, "allocated": done, "unapplied": left}

@app.get("/api/bank/review")
def bank_review():
    with _lock:
        return rows("SELECT p.*, c.name suggested FROM payments p LEFT JOIN customers c ON c.id=p.suggested_customer_id WHERE p.status IN ('UNMATCHED','SUGGESTED') ORDER BY p.received_on DESC")

class Confirm(BaseModel):
    customer_id: int

@app.post("/api/payments/{pid}/confirm")
def confirm(pid: int, b: Confirm):
    with _lock:
        d, l = ledger.confirm_bank_payment(conn, pid, b.customer_id); return {"allocated": d, "advance": l}

@app.post("/api/payments/{pid}/reverse")
def reverse(pid: int):
    with _lock: ledger.reverse_payment(conn, pid); return {"ok": True}

class Amt(BaseModel):
    amount: float | None = None

@app.post("/api/customers/{cid}/wallet/topup")
def topup(cid: int, b: Amt):
    with _lock: ledger.wallet_topup(conn, cid, b.amount or 0); return {"ok": True}

@app.post("/api/customers/{cid}/wallet/deduct")
def deduct(cid: int, b: Amt):
    with _lock: return {"deducted": ledger.wallet_deduct(conn, cid, b.amount, actor="user")}

@app.post("/api/autodebit/run")
def autodebit():
    with _lock: return {"debited": ledger.run_auto_debit(conn)}

# ---------------------------------------------------------------- reminders
@app.get("/api/reminders/plan")
def rplan():
    with _lock: return [p for p in reminders.plan(conn) if p["action"] != "SKIP"]

class RunIn(BaseModel):
    mode: str | None = None; limit: int | None = None

@app.post("/api/reminders/run")
def rrun(b: RunIn):
    with _lock: return reminders.run(conn, send_mode=b.mode, limit=b.limit)

@app.get("/api/reminders/preview/{cid}")
def rpreview(cid: int):
    with _lock:
        p = next((x for x in reminders.plan(conn) if x["customer_id"] == cid and x["action"] == "REMIND"), None)
        return reminders.compose(conn, cid, p["stage"] if p else 1)

@app.get("/api/reminders/log")
def rlog():
    return rows("SELECT l.*, c.name customer FROM reminder_log l JOIN customers c ON c.id=l.customer_id ORDER BY l.id DESC LIMIT 200")

# ---------------------------------------------------------------- settings / zoho / health
@app.get("/api/settings")
def get_settings(): return db.get_settings(conn)

@app.put("/api/settings")
def put_settings(b: dict):
    with _lock:
        for k, v in b.items(): conn.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        conn.commit(); return db.get_settings(conn)

@app.post("/api/zoho/pull")
def zoho_pull():
    try:
        with _lock: return {"invoices": zoho.pull_open_invoices(conn)}
    except zoho.ZohoNotConfigured as e:
        raise HTTPException(400, f"Zoho not configured: {e}")

@app.get("/api/audit")
def audit_log(): return rows("SELECT * FROM audit ORDER BY id DESC LIMIT 200")

def _daily_loop():
    """Built-in scheduler: once a day after 09:30 local - auto-debit, then reminders. (Or run `python -m app.jobs` from cron.)"""
    c = db.connect()
    while True:
        try:
            now = datetime.datetime.now(); s = db.get_settings(c)
            if now.hour * 60 + now.minute >= 9 * 60 + 30 and s.get("last_auto_run") != now.date().isoformat():
                with _lock: reminders.run(c); c.execute("INSERT OR REPLACE INTO settings VALUES('last_auto_run',?)", (now.date().isoformat(),)); c.commit()
        except Exception as e:
            print("scheduler error", e)
        time.sleep(300)

if os.environ.get("DUES_SCHEDULER", "1") == "1":
    threading.Thread(target=_daily_loop, daemon=True).start()

@app.get("/")
def index(): return FileResponse(os.path.join(STATIC, "index.html"))
app.mount("/static", StaticFiles(directory=STATIC), name="static")

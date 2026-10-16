"""Money logic: invoices (incl. reseller split), payment allocation, wallet, bank matching.

Rule: every rupee that settles an invoice is an `allocation` row from a `payment`
(BANK / WALLET / AUTO_DEBIT / CREDIT_NOTE / ZOHO / SNAPSHOT). Invoice status is derived from allocations, never typed by hand.
Reseller model: an invoice of Rs 100 for a customer whose reseller bears 50% becomes TWO invoice rows sharing a split_group:
payer=CUSTOMER (50) and payer=RESELLER (50). Each is chased, aged and settled independently.
"""
import datetime, calendar, hashlib, re
from .db import audit, get_settings, now

EPS = 0.5

def today():
    return datetime.date.today().isoformat()

def month_end(period):
    y, m = int(period[:4]), int(period[5:7])
    return datetime.date(y, m, calendar.monthrange(y, m)[1]).isoformat()

def due_date(invoice_date, credit_days):
    return (datetime.date.fromisoformat(invoice_date) + datetime.timedelta(days=int(credit_days))).isoformat()

# ------------------------------------------------------------------ queries
BAL = "i.amount - COALESCE((SELECT SUM(a.amount) FROM allocations a WHERE a.invoice_id=i.id),0)"

def open_invoices(conn, customer_id, payer="CUSTOMER", include_disputed=True):
    """FIFO order. payer=None -> both parts."""
    q = f"""SELECT i.*, {BAL} AS balance FROM invoices i WHERE i.customer_id=? AND i.status IN ('OPEN','PARTIAL')
            AND (? IS NULL OR i.payer=?) AND (?=1 OR (i.disputed=0 AND i.hold=0)) ORDER BY i.due_date, i.period, i.id"""
    return conn.execute(q, (customer_id, payer, payer, 1 if include_disputed else 0)).fetchall()

def open_invoices_reseller(conn, reseller_id, include_disputed=True):
    q = f"""SELECT i.*, c.name AS customer_name, {BAL} AS balance FROM invoices i JOIN customers c ON c.id=i.customer_id
            WHERE c.reseller_id=? AND i.payer='RESELLER' AND i.status IN ('OPEN','PARTIAL') AND (?=1 OR (i.disputed=0 AND i.hold=0))
            ORDER BY i.due_date, i.period, i.id"""
    return conn.execute(q, (reseller_id, 1 if include_disputed else 0)).fetchall()

def outstanding(conn, customer_id, payer=None):
    return sum(r["balance"] for r in open_invoices(conn, customer_id, payer))

def reseller_outstanding(conn, reseller_id):
    return sum(r["balance"] for r in open_invoices_reseller(conn, reseller_id))

def refresh_invoice(conn, invoice_id):
    r = conn.execute("""SELECT i.amount, i.status, COALESCE(SUM(a.amount),0) paid FROM invoices i
        LEFT JOIN allocations a ON a.invoice_id=i.id WHERE i.id=? GROUP BY i.id""", (invoice_id,)).fetchone()
    if r["status"] == "CANCELLED":
        return
    st = "CLOSED" if r["paid"] >= r["amount"] - EPS else ("PARTIAL" if r["paid"] > 0 else "OPEN")
    conn.execute("UPDATE invoices SET status=? WHERE id=?", (st, invoice_id))

# ------------------------------------------------------------------ invoices (source of truth lives here)
def create_invoice(conn, customer_id, amount, invoice_date=None, kind="OTHER", invoice_no="", description="",
                   source="MANUAL", period=None, zoho_invoice_id=None, credit_days=None, split=True, commit=True, hold=0, hold_reason=""):
    """Create an invoice for a customer; if the customer has a reseller share, create the reseller part too.
    Returns list of invoice ids. Unapplied advances of the customer/reseller are applied automatically."""
    c = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not c:
        raise ValueError("unknown customer")
    amount = round(float(amount), 2)
    if amount <= 0:
        raise ValueError("amount must be positive")
    invoice_date = invoice_date or today()
    period = period or invoice_date[:7] + "-01"
    cd = c["credit_days"] if credit_days is None else credit_days
    dd = due_date(invoice_date, cd if cd is not None else get_settings(conn)["default_credit_days"])
    pct = float(c["reseller_share_pct"] or 0) if (split and c["reseller_id"]) else 0.0
    r_amt = round(amount * min(pct, 100) / 100, 2)
    parts = [("CUSTOMER", round(amount - r_amt, 2)), ("RESELLER", r_amt)]
    parts = [(p, a) for p, a in parts if a > 0]
    gid = None; ids = []
    for payer, a in parts:
        try:
            cur = conn.execute("""INSERT INTO invoices(customer_id,payer,kind,period,invoice_no,description,invoice_date,due_date,amount,
                split_group,source,zoho_invoice_id,created_at,hold,hold_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (customer_id, payer, kind, period, invoice_no, description, invoice_date, dd, a, gid, source, zoho_invoice_id, now(), hold, hold_reason))
        except Exception as e:
            if "UNIQUE" in str(e):
                raise ValueError(f"invoice already exists ({kind} {period} {invoice_no or '-'})")
            raise
        ids.append(cur.lastrowid)
        if gid is None:
            gid = cur.lastrowid
            conn.execute("UPDATE invoices SET split_group=? WHERE id=?", (gid, gid))
    audit(conn, "invoice_create", f"cust={customer_id} {kind} {invoice_no} {amount} parts={len(ids)} src={source}")
    apply_unapplied(conn, ("C", customer_id))
    if c["reseller_id"]:
        apply_unapplied(conn, ("R", c["reseller_id"]))
    if commit:
        conn.commit()
    return ids

def update_invoice(conn, invoice_id, **f):
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        raise ValueError("unknown invoice")
    paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM allocations WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
    if "amount" in f and f["amount"] is not None and float(f["amount"]) < paid - EPS:
        raise ValueError(f"amount cannot be below already-settled {paid:.2f}; reverse a payment first")
    cols = {k: f[k] for k in ("amount", "invoice_date", "description", "invoice_no", "kind", "period", "due_date") if f.get(k) not in (None, "")}
    if "invoice_date" in cols and "due_date" not in cols:
        cd = conn.execute("SELECT credit_days FROM customers WHERE id=?", (inv["customer_id"],)).fetchone()[0]
        cols["due_date"] = due_date(cols["invoice_date"], cd)
    if cols:
        conn.execute("UPDATE invoices SET " + ",".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), invoice_id))
    refresh_invoice(conn, invoice_id)
    audit(conn, "invoice_update", f"{invoice_id} {cols}"); conn.commit()

def cancel_invoice(conn, invoice_id, reason=""):
    paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM allocations WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
    if paid > EPS:
        raise ValueError("invoice has payments allocated; reverse them first (or raise a credit note instead)")
    conn.execute("UPDATE invoices SET status='CANCELLED', dispute_note=? WHERE id=?", (reason, invoice_id))
    audit(conn, "invoice_cancel", f"{invoice_id} {reason}"); conn.commit()

def restore_invoice(conn, invoice_id):
    conn.execute("UPDATE invoices SET status='OPEN' WHERE id=? AND status='CANCELLED'", (invoice_id,))
    refresh_invoice(conn, invoice_id); audit(conn, "invoice_restore", str(invoice_id)); conn.commit()

def set_dispute(conn, invoice_id, disputed, note=""):
    conn.execute("UPDATE invoices SET disputed=?, dispute_note=? WHERE id=?", (1 if disputed else 0, note, invoice_id))
    audit(conn, "invoice_dispute", f"{invoice_id} {disputed} {note}"); conn.commit()

def release_hold(conn, source=None, date_from=None, date_to=None, invoice_ids=None):
    """Put held invoices into normal chasing. Filters are optional; returns number released."""
    w, a = ["hold=1"], []
    if source: w.append("source=?"); a.append(source)
    if date_from: w.append("invoice_date>=?"); a.append(date_from)
    if date_to: w.append("invoice_date<=?"); a.append(date_to)
    if invoice_ids: w.append("id IN (%s)" % ",".join("?" * len(invoice_ids))); a += list(invoice_ids)
    n = conn.execute("UPDATE invoices SET hold=0, hold_reason='' WHERE " + " AND ".join(w), a).rowcount
    audit(conn, "release_hold", f"{n} invoices {source or ''} {date_from or ''}..{date_to or ''}"); conn.commit()
    return n

def recompute_due_dates(conn, customer_id):
    c = conn.execute("SELECT credit_days FROM customers WHERE id=?", (customer_id,)).fetchone()
    for i in conn.execute("SELECT id,invoice_date FROM invoices WHERE customer_id=? AND status IN ('OPEN','PARTIAL')", (customer_id,)).fetchall():
        conn.execute("UPDATE invoices SET due_date=? WHERE id=?", (due_date(i["invoice_date"], c["credit_days"]), i["id"]))

# ------------------------------------------------------------------ payments
def _party_of(p):
    return ("R", p["reseller_id"]) if p["reseller_id"] else ("C", p["customer_id"])

def _party_open(conn, party):
    if party[0] == "R":
        return open_invoices_reseller(conn, party[1])
    return open_invoices(conn, party[1], "CUSTOMER")

def unapplied(conn, payment_id):
    p = conn.execute("SELECT amount, tds_amount FROM payments WHERE id=?", (payment_id,)).fetchone()
    used = conn.execute("SELECT COALESCE(SUM(amount),0) FROM allocations WHERE payment_id=?", (payment_id,)).fetchone()[0]
    return round(p["amount"] + (p["tds_amount"] or 0) - used, 2)

def allocate(conn, payment_id, invoice_ids=None):
    """Spend the unallocated part of a payment on the payer's invoices, oldest first (or on the given ones)."""
    p = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    left, done = unapplied(conn, payment_id), 0.0
    invs = _party_open(conn, _party_of(p))
    if invoice_ids:
        wanted = set(invoice_ids); invs = [i for i in invs if i["id"] in wanted]
    for inv in invs:
        if left <= EPS:
            break
        amt = round(min(left, inv["balance"]), 2)
        conn.execute("INSERT INTO allocations(payment_id,invoice_id,amount) VALUES(?,?,?)", (payment_id, inv["id"], amt))
        refresh_invoice(conn, inv["id"]); left -= amt; done += amt
    if p["customer_id"] or p["reseller_id"]:
        conn.execute("UPDATE payments SET status='ALLOCATED' WHERE id=?", (payment_id,))
    return round(done, 2), round(left, 2)

def apply_unapplied(conn, party):
    """Advances/excess payments automatically settle newly raised invoices."""
    col = "reseller_id" if party[0] == "R" else "customer_id"
    extra = "AND reseller_id IS NULL" if party[0] == "C" else ""
    for p in conn.execute(f"SELECT id FROM payments WHERE {col}=? {extra} AND status='ALLOCATED' ORDER BY received_on, id", (party[1],)).fetchall():
        if unapplied(conn, p["id"]) > EPS:
            allocate(conn, p["id"])

def record_payment(conn, customer_id, amount, mode, received_on=None, reference="", narration="", invoice_ids=None,
                   reseller_id=None, tds=0.0, source="MANUAL", commit=True):
    if not customer_id and not reseller_id:
        raise ValueError("customer or reseller required")
    cur = conn.execute("INSERT INTO payments(customer_id,reseller_id,mode,amount,tds_amount,received_on,reference,narration,source) VALUES(?,?,?,?,?,?,?,?,?)",
                       (None if reseller_id else customer_id, reseller_id, mode, round(float(amount), 2), round(float(tds or 0), 2),
                        received_on or today(), reference, narration, source))
    pid = cur.lastrowid
    done, left = allocate(conn, pid, invoice_ids)
    audit(conn, "payment", f"{'R'+str(reseller_id) if reseller_id else 'C'+str(customer_id)} {mode} {amount} tds={tds} allocated={done} advance={left}")
    if commit:
        conn.commit()
    return pid, done, left

def reverse_payment(conn, payment_id):
    p = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    inv_ids = [r[0] for r in conn.execute("SELECT invoice_id FROM allocations WHERE payment_id=?", (payment_id,))]
    conn.execute("DELETE FROM allocations WHERE payment_id=?", (payment_id,))
    for i in inv_ids:
        refresh_invoice(conn, i)
    conn.execute("DELETE FROM payments WHERE id=?", (payment_id,))
    audit(conn, "reverse_payment", f"id={payment_id} {p['mode']} {p['amount']}"); conn.commit()

# ------------------------------------------------------------------ wallet (mirror of the money held on the ops platform)
def wallet_set(conn, customer_id, balance):
    conn.execute("UPDATE customers SET wallet_balance=? WHERE id=?", (round(float(balance), 2), customer_id)); conn.commit()

def apply_wallet(conn, customer_id, max_amount=None, mode="WALLET", actor="user", reduce_wallet=True):
    """Settle dues from the held balance. Never overdraws. reduce_wallet=False just records a deduction that the platform already made."""
    c = conn.execute("SELECT wallet_balance FROM customers WHERE id=?", (customer_id,)).fetchone()
    cap = c["wallet_balance"] if reduce_wallet else 1e18
    amt = min(cap, outstanding(conn, customer_id, "CUSTOMER"), max_amount or 1e18)
    if amt < 1:
        return 0.0
    if reduce_wallet:
        conn.execute("UPDATE customers SET wallet_balance=wallet_balance-? WHERE id=?", (amt, customer_id))
    record_payment(conn, customer_id, round(amt, 2), mode, narration="Deducted from held balance", commit=False)
    audit(conn, "wallet_apply", f"cust={customer_id} amt={amt}", actor); conn.commit()
    return round(amt, 2)

def run_auto_apply(conn):
    """Optional daily job (setting auto_apply_wallet=1)."""
    out = []
    for c in conn.execute("""SELECT id FROM customers WHERE status='ACTIVE' AND auto_debit_allowed=1 AND wallet_balance>=1""").fetchall():
        a = apply_wallet(conn, c["id"], mode="AUTO_DEBIT", actor="auto")
        if a:
            out.append((c["id"], a))
    return out

# ------------------------------------------------------------------ bank statement matching
def _norm(s):
    s = re.sub(r"\b(pvt|private|ltd|limited|llp|india|the|and)\b", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9 ]", " ", s).split()

def match_party(conn, narration, amount):
    """Best (party, score) for a bank narration. party is ('C',id) or ('R',id)."""
    toks = set(_norm(narration)); best, bs = None, 0.0
    cands = [(("C", c["id"]), c["name"], lambda cid=c["id"]: outstanding(conn, cid, "CUSTOMER"), lambda cid=c["id"]: open_invoices(conn, cid, "CUSTOMER"))
             for c in conn.execute("SELECT id,name FROM customers WHERE status!='STOPPED'")]
    cands += [(("R", r["id"]), r["name"], lambda rid=r["id"]: reseller_outstanding(conn, rid), lambda rid=r["id"]: open_invoices_reseller(conn, rid))
              for r in conn.execute("SELECT id,name FROM resellers")]
    for party, name, out_fn, inv_fn in cands:
        ct = set(_norm(name))
        if not ct:
            continue
        s = len(ct & toks) / len(ct)
        if s >= 0.5:
            if abs(out_fn() - amount) < 1:
                s += 0.5
            elif any(abs(i["balance"] - amount) < 1 for i in inv_fn()):
                s += 0.25
            if s > bs:
                best, bs = party, s
    return best, bs

def import_bank_rows(conn, rows, auto_apply_threshold=1.2):
    stats = {"new": 0, "duplicate": 0, "auto_applied": 0, "suggested": 0, "unmatched": 0}
    for r in rows:
        amt = float(r["amount"] or 0)
        if amt <= 0:
            continue
        h = hashlib.sha1(f'{r["date"]}|{amt}|{r.get("reference","")}|{r["narration"]}'.encode()).hexdigest()
        if conn.execute("SELECT 1 FROM payments WHERE bank_hash=?", (h,)).fetchone():
            stats["duplicate"] += 1; continue
        party, score = match_party(conn, r["narration"], amt)
        sp = f"{party[0]}:{party[1]}" if party else None
        cur = conn.execute("INSERT INTO payments(mode,amount,received_on,reference,narration,status,suggested_party,bank_hash,source) VALUES('BANK',?,?,?,?,?,?,?,'BANKFILE')",
                           (amt, r["date"], r.get("reference", ""), r["narration"], "SUGGESTED" if party else "UNMATCHED", sp, h))
        stats["new"] += 1
        if party and score >= auto_apply_threshold:
            confirm_bank_payment(conn, cur.lastrowid, sp); stats["auto_applied"] += 1
        elif party:
            stats["suggested"] += 1
        else:
            stats["unmatched"] += 1
    conn.commit()
    return stats

def confirm_bank_payment(conn, payment_id, party_str, tds=0.0):
    kind, pid = str(party_str).split(":") if ":" in str(party_str) else ("C", party_str)
    if kind == "R":
        conn.execute("UPDATE payments SET reseller_id=?, customer_id=NULL, tds_amount=? WHERE id=?", (int(pid), tds, payment_id))
    else:
        conn.execute("UPDATE payments SET customer_id=?, reseller_id=NULL, tds_amount=? WHERE id=?", (int(pid), tds, payment_id))
    done, left = allocate(conn, payment_id)
    audit(conn, "bank_match", f"payment={payment_id} party={kind}:{pid} allocated={done} advance={left}"); conn.commit()
    return done, left

"""Money logic: outstanding, payment allocation (invoice closing), wallet deduction, auto-debit, bank matching.

Single rule for closing invoices: every rupee that settles an invoice is an `allocation` row from a `payment`
(BANK / WALLET / AUTO_DEBIT / CREDIT_NOTE / SNAPSHOT). Invoice status is derived from allocations, never typed by hand.
"""
import datetime, hashlib, re
from .db import audit

EPS = 0.5  # rupee tolerance for "fully paid"

def today():
    return datetime.date.today().isoformat()

def refresh_invoice(conn, invoice_id):
    r = conn.execute("""SELECT i.amount, i.status, COALESCE(SUM(a.amount),0) paid FROM invoices i
        LEFT JOIN allocations a ON a.invoice_id=i.id WHERE i.id=? GROUP BY i.id""", (invoice_id,)).fetchone()
    if r["status"] == "CANCELLED":
        return
    st = "CLOSED" if r["paid"] >= r["amount"] - EPS else ("PARTIAL" if r["paid"] > 0 else "OPEN")
    conn.execute("UPDATE invoices SET status=? WHERE id=?", (st, invoice_id))

def open_invoices(conn, customer_id):
    """Oldest first (FIFO). Returns rows with `balance`."""
    return conn.execute("""SELECT i.*, i.amount - COALESCE((SELECT SUM(amount) FROM allocations WHERE invoice_id=i.id),0) AS balance
        FROM invoices i WHERE i.customer_id=? AND i.status IN ('OPEN','PARTIAL')
        ORDER BY i.due_date, i.period, i.id""", (customer_id,)).fetchall()

def outstanding(conn, customer_id):
    return sum(r["balance"] for r in open_invoices(conn, customer_id))

def allocate(conn, payment_id, invoice_ids=None):
    """Spend an (unallocated part of a) payment on the customer's invoices, oldest first, or on the given invoices.
    Returns (allocated, leftover)."""
    p = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    used = conn.execute("SELECT COALESCE(SUM(amount),0) FROM allocations WHERE payment_id=?", (payment_id,)).fetchone()[0]
    left = p["amount"] - used
    done = 0.0
    invs = open_invoices(conn, p["customer_id"])
    if invoice_ids:
        invs = [i for i in invs if i["id"] in set(invoice_ids)]
    for inv in invs:
        if left <= EPS:
            break
        amt = round(min(left, inv["balance"]), 2)
        conn.execute("INSERT INTO allocations(payment_id,invoice_id,amount) VALUES(?,?,?)", (payment_id, inv["id"], amt))
        refresh_invoice(conn, inv["id"])
        left -= amt; done += amt
    conn.execute("UPDATE payments SET status=? WHERE id=?", ("ALLOCATED" if p["customer_id"] else p["status"], payment_id))
    return round(done, 2), round(left, 2)

def record_payment(conn, customer_id, amount, mode, received_on=None, reference="", narration="", invoice_ids=None):
    cur = conn.execute("INSERT INTO payments(customer_id,mode,amount,received_on,reference,narration) VALUES(?,?,?,?,?,?)",
                       (customer_id, mode, amount, received_on or today(), reference, narration))
    pid = cur.lastrowid
    done, left = allocate(conn, pid, invoice_ids)
    audit(conn, "payment", f"cust={customer_id} {mode} {amount} allocated={done} unapplied={left}")
    if left > EPS and mode == "BANK":     # excess bank money sits as an advance the customer can be debited from later
        pass
    conn.commit()
    return pid, done, left

# ---- Mechanism 2: customer money held on our platform ------------------------------------------------------------
def wallet_topup(conn, customer_id, amount):
    conn.execute("UPDATE customers SET wallet_balance=wallet_balance+? WHERE id=?", (amount, customer_id))
    conn.commit()

def wallet_deduct(conn, customer_id, max_amount=None, actor="system"):
    """Deduct from the held wallet balance against the oldest dues. Never overdraws."""
    c = conn.execute("SELECT wallet_balance FROM customers WHERE id=?", (customer_id,)).fetchone()
    amt = min(c["wallet_balance"], outstanding(conn, customer_id), max_amount or 1e18)
    if amt < 1:
        return 0.0
    conn.execute("UPDATE customers SET wallet_balance=wallet_balance-? WHERE id=?", (amt, customer_id))
    record_payment(conn, customer_id, round(amt, 2), "WALLET", narration="Deducted from held balance")
    audit(conn, "wallet_deduct", f"cust={customer_id} amt={amt}", actor)
    return round(amt, 2)

# ---- Mechanism 3: auto-debit ---------------------------------------------------------------------------------------
def auto_debit_candidates(conn):
    """Customers eligible for the auto-debit run: opted in, not blocked ("Do not auto debit"), has dues and wallet money."""
    rows = conn.execute("""SELECT id FROM customers WHERE status='ACTIVE' AND auto_debit_allowed=1
        AND collection_mode IN ('AUTO_DEBIT','WALLET') AND wallet_balance>=1""").fetchall()
    return [r["id"] for r in rows if outstanding(conn, r["id"]) >= 1]

def run_auto_debit(conn):
    out = []
    for cid in auto_debit_candidates(conn):
        amt = wallet_deduct(conn, cid, actor="auto-debit")
        if amt:
            # relabel as AUTO_DEBIT for reporting
            conn.execute("UPDATE payments SET mode='AUTO_DEBIT' WHERE id=(SELECT MAX(id) FROM payments WHERE customer_id=?)", (cid,))
            out.append((cid, amt))
    conn.commit()
    return out

def apply_credit_note(conn, customer_id, amount, reference="", invoice_ids=None):
    return record_payment(conn, customer_id, amount, "CREDIT_NOTE", reference=reference, invoice_ids=invoice_ids)

def reverse_payment(conn, payment_id):
    """Undo a wrongly applied payment: invoices re-open, wallet money is returned for WALLET/AUTO_DEBIT."""
    p = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    inv_ids = [r[0] for r in conn.execute("SELECT invoice_id FROM allocations WHERE payment_id=?", (payment_id,))]
    conn.execute("DELETE FROM allocations WHERE payment_id=?", (payment_id,))
    for i in inv_ids:
        refresh_invoice(conn, i)
    if p["mode"] in ("WALLET", "AUTO_DEBIT"):
        conn.execute("UPDATE customers SET wallet_balance=wallet_balance+? WHERE id=?", (p["amount"], p["customer_id"]))
    conn.execute("DELETE FROM payments WHERE id=?", (payment_id,))
    audit(conn, "reverse_payment", f"id={payment_id}")
    conn.commit()

# ---- Mechanism 1: bank statement -------------------------------------------------------------------------------------
def _norm(s):
    s = re.sub(r"\b(pvt|private|ltd|limited|llp|india|the)\b", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9 ]", " ", s).split()

def match_customer(conn, narration, amount):
    """Score customers against a bank narration: name-token overlap, boosted if amount equals open balance or an invoice."""
    toks = set(_norm(narration))
    best, best_score = None, 0.0
    for c in conn.execute("SELECT id,name FROM customers WHERE status!='STOPPED'"):
        ct = set(_norm(c["name"]))
        if not ct:
            continue
        s = len(ct & toks) / len(ct)
        if s >= 0.5:
            out = outstanding(conn, c["id"])
            if abs(out - amount) < 1:
                s += 0.5
            elif any(abs(i["balance"] - amount) < 1 for i in open_invoices(conn, c["id"])):
                s += 0.25
            if s > best_score:
                best, best_score = c["id"], s
    return best, best_score

def import_bank_rows(conn, rows, auto_apply_threshold=1.2):
    """rows: dicts {date, amount, narration, reference}. Credits only. Idempotent via hash. High-confidence matches are
    applied automatically, the rest wait in UNMATCHED/SUGGESTED for one-click confirmation."""
    stats = {"new": 0, "duplicate": 0, "auto_applied": 0, "suggested": 0, "unmatched": 0}
    for r in rows:
        amt = float(r["amount"] or 0)
        if amt <= 0:
            continue
        h = hashlib.sha1(f'{r["date"]}|{amt}|{r.get("reference","")}|{r["narration"]}'.encode()).hexdigest()
        if conn.execute("SELECT 1 FROM payments WHERE bank_hash=?", (h,)).fetchone():
            stats["duplicate"] += 1; continue
        cid, score = match_customer(conn, r["narration"], amt)
        cur = conn.execute("INSERT INTO payments(customer_id,mode,amount,received_on,reference,narration,status,suggested_customer_id,bank_hash)"
                           " VALUES(NULL,'BANK',?,?,?,?,?,?,?)",
                           (amt, r["date"], r.get("reference", ""), r["narration"], "SUGGESTED" if cid else "UNMATCHED", cid, h))
        stats["new"] += 1
        if cid and score >= auto_apply_threshold:
            confirm_bank_payment(conn, cur.lastrowid, cid); stats["auto_applied"] += 1
        elif cid:
            stats["suggested"] += 1
        else:
            stats["unmatched"] += 1
    conn.commit()
    return stats

def confirm_bank_payment(conn, payment_id, customer_id):
    conn.execute("UPDATE payments SET customer_id=? WHERE id=?", (customer_id, payment_id))
    done, left = allocate(conn, payment_id)
    audit(conn, "bank_match", f"payment={payment_id} cust={customer_id} allocated={done} advance={left}")
    conn.commit()
    return done, left

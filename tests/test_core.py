import os, tempfile, datetime, pytest
os.environ["DUES_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DUES_SCHEDULER"] = "0"
from app import db, ledger, reminders

@pytest.fixture()
def c():
    conn = db.connect(); db.init(conn); conn.executescript("DELETE FROM allocations;DELETE FROM payments;DELETE FROM invoices;DELETE FROM customers;DELETE FROM reminder_log;")
    yield conn
    conn.close()

def mk(conn, name="Acme Pvt Ltd", amounts=(1000, 2000, 3000), start="2025-01-01", **kw):
    cid = conn.execute("INSERT INTO customers(name,email_to,kam) VALUES(?,?,?)", (name, "a@acme.com", "Tanisha")).lastrowid
    for k, v in kw.items(): conn.execute(f"UPDATE customers SET {k}=? WHERE id=?", (v, cid))
    y, m = int(start[:4]), int(start[5:7])
    for i, a in enumerate(amounts):
        mm = (m - 1 + i) % 12 + 1; yy = y + (m - 1 + i) // 12
        conn.execute("INSERT INTO invoices(customer_id,kind,period,amount,due_date) VALUES(?,?,?,?,?)", (cid, "SPEND_MGMT", f"{yy}-{mm:02d}-01", a, f"{yy}-{mm:02d}-28"))
    conn.commit(); return cid

def test_fifo_allocation_closes_oldest_first(c):
    cid = mk(c); pid, done, left = ledger.record_payment(c, cid, 2500, "BANK")
    st = [r["status"] for r in c.execute("SELECT status FROM invoices ORDER BY period")]
    assert st == ["CLOSED", "PARTIAL", "OPEN"] and done == 2500 and left == 0
    assert ledger.outstanding(c, cid) == 3500

def test_reverse_reopens(c):
    cid = mk(c); pid, *_ = ledger.record_payment(c, cid, 6000, "BANK"); assert ledger.outstanding(c, cid) == 0
    ledger.reverse_payment(c, pid); assert ledger.outstanding(c, cid) == 6000

def test_wallet_deduct_never_overdraws_and_autodebit_respects_flag(c):
    a = mk(c, "A Ltd", wallet_balance=1500, collection_mode="WALLET"); b = mk(c, "B Ltd", wallet_balance=9999, collection_mode="WALLET", auto_debit_allowed=0)
    ledger.run_auto_debit(c)
    assert ledger.outstanding(c, a) == 4500 and c.execute("SELECT wallet_balance FROM customers WHERE id=?", (a,)).fetchone()[0] == 0
    assert ledger.outstanding(c, b) == 6000   # "Do not auto debit" honoured

def test_bank_matching_idempotent(c):
    cid = mk(c, "Zenith Logistics Private Limited", amounts=(5000,))
    rows = [{"date": "2026-01-02", "amount": 5000, "narration": "NEFT ZENITH LOGISTICS PVT LTD", "reference": "U1"},
            {"date": "2026-01-02", "amount": 70, "narration": "random credit", "reference": "U2"}]
    s = ledger.import_bank_rows(c, rows); assert s["auto_applied"] == 1 and s["unmatched"] == 1
    assert ledger.outstanding(c, cid) == 0
    assert ledger.import_bank_rows(c, rows)["duplicate"] == 2

def test_reminder_stages_and_no_spam(c):
    cid = mk(c, amounts=(5000,), start="2026-01-01")      # due 2026-01-28
    today = datetime.date(2026, 2, 5)                       # 8 days overdue -> stage 2
    p = [x for x in reminders.plan(c, today) if x["customer_id"] == cid][0]; assert p["action"] == "REMIND" and p["stage"] == 2
    reminders.run(c, today, send_mode="DRY_RUN")            # dry run must not advance stages
    assert [x for x in reminders.plan(c, today) if x["customer_id"] == cid][0]["action"] == "REMIND"
    c.execute("INSERT INTO reminder_log(customer_id,stage,sent_at,status) VALUES(?,?,?,?)", (cid, 2, "2026-02-05T09:00:00", "SENT"))
    assert reminders.plan(c, datetime.date(2026, 2, 8))[0]["action"] == "SKIP"            # min gap
    assert reminders.plan(c, datetime.date(2026, 2, 20))[0]["stage"] == 3                 # escalates
    c.execute("UPDATE customers SET email_to='' WHERE id=?", (cid,))
    assert reminders.plan(c, datetime.date(2026, 3, 20))[0]["action"] == "SKIP"

def test_wallet_customer_gets_autodebit_not_reminder(c):
    cid = mk(c, amounts=(5000,), start="2026-01-01", wallet_balance=8000)
    assert reminders.plan(c, datetime.date(2026, 3, 1))[0]["action"] == "AUTO_DEBIT"

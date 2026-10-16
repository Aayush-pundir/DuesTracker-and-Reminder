import os, tempfile, datetime, io, pytest
os.environ["DUES_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DUES_SCHEDULER"] = "0"
import openpyxl
from app import db, ledger, reminders, importers, zoho

@pytest.fixture()
def c():
    conn = db.connect(); db.init(conn); db.wipe_business_data(conn)
    for k in ("is_demo", "migration_done"): db.set_setting(conn, k, "0")
    db.set_setting(conn, "channel_whatsapp", "0"); conn.commit()
    yield conn
    conn.close()

def cust(conn, name="Acme Pvt Ltd", **kw):
    cid = conn.execute("INSERT INTO customers(name,email_to,kam,whatsapp) VALUES(?,?,?,?)", (name, "a@acme.example", "Tanisha", "919900000001")).lastrowid
    for k, v in kw.items(): conn.execute(f"UPDATE customers SET {k}=? WHERE id=?", (v, cid))
    conn.commit(); return cid

def months(conn, cid, amounts=(1000, 2000, 3000), start="2025-01-01"):
    y, m = int(start[:4]), int(start[5:7]); out = []
    for i, a in enumerate(amounts):
        mm = (m - 1 + i) % 12 + 1; yy = y + (m - 1 + i) // 12
        out += ledger.create_invoice(conn, cid, a, f"{yy}-{mm:02d}-28", "SPEND_MGMT", f"I{yy}{mm:02d}")
    return out

# ---------------------------------------------------------------- invoices & credit terms
def test_due_date_uses_customer_credit_days(c):
    a = cust(c, credit_days=15); b = cust(c, "B Ltd", credit_days=45)
    ia = ledger.create_invoice(c, a, 100, "2026-01-10")[0]; ib = ledger.create_invoice(c, b, 100, "2026-01-10")[0]
    d = lambda i: c.execute("SELECT due_date FROM invoices WHERE id=?", (i,)).fetchone()[0]
    assert d(ia) == "2026-01-25" and d(ib) == "2026-02-24"
    c.execute("UPDATE customers SET credit_days=30 WHERE id=?", (a,)); ledger.recompute_due_dates(c, a)
    assert d(ia) == "2026-02-09"

def test_reseller_split_creates_two_independent_receivables(c):
    rid = c.execute("SELECT id FROM resellers WHERE name='IDFC FIRST Bank'").fetchone()[0]
    cid = cust(c, reseller_id=rid, reseller_share_pct=50)
    ids = ledger.create_invoice(c, cid, 100, "2026-01-01", invoice_no="INV1")
    assert len(ids) == 2
    assert ledger.outstanding(c, cid, "CUSTOMER") == 50 and ledger.outstanding(c, cid, "RESELLER") == 50 and ledger.reseller_outstanding(c, rid) == 50
    ledger.record_payment(c, cid, 50, "BANK")                      # customer pays its half
    assert ledger.outstanding(c, cid, "CUSTOMER") == 0 and ledger.reseller_outstanding(c, rid) == 50
    ledger.record_payment(c, None, 50, "BANK", reseller_id=rid)    # reseller pays its half
    assert [r[0] for r in c.execute("SELECT status FROM invoices ORDER BY id")] == ["CLOSED", "CLOSED"]

def test_fifo_tds_and_reverse(c):
    cid = cust(c); months(c, cid)
    pid, done, left = ledger.record_payment(c, cid, 1450, "BANK", tds=50)       # 1450 cash + 50 TDS closes first invoice + part of second
    assert done == 1500 and [r[0] for r in c.execute("SELECT status FROM invoices ORDER BY invoice_date")] == ["CLOSED", "PARTIAL", "OPEN"]
    ledger.reverse_payment(c, pid); assert ledger.outstanding(c, cid) == 6000

def test_advance_is_applied_to_next_invoice(c):
    cid = cust(c); ledger.record_payment(c, cid, 700, "BANK")                    # nothing to settle yet
    assert c.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 0
    ledger.create_invoice(c, cid, 1000, "2026-03-01")
    assert ledger.outstanding(c, cid) == 300

def test_cancel_edit_rules(c):
    cid = cust(c); i = ledger.create_invoice(c, cid, 1000, "2026-03-01")[0]
    ledger.record_payment(c, cid, 400, "BANK")
    with pytest.raises(ValueError): ledger.cancel_invoice(c, i)
    with pytest.raises(ValueError): ledger.update_invoice(c, i, amount=300)
    ledger.update_invoice(c, i, amount=900); assert ledger.outstanding(c, cid) == 500
    p = c.execute("SELECT id FROM payments").fetchone()[0]; ledger.reverse_payment(c, p)
    ledger.cancel_invoice(c, i, "raised by mistake"); assert ledger.outstanding(c, cid) == 0

def test_wallet_never_overdraws(c):
    cid = cust(c, wallet_balance=1500); months(c, cid)
    assert ledger.apply_wallet(c, cid) == 1500 and ledger.outstanding(c, cid) == 4500
    assert c.execute("SELECT wallet_balance FROM customers").fetchone()[0] == 0

# ---------------------------------------------------------------- bank
def test_bank_matching_customer_and_reseller_idempotent(c):
    cid = cust(c, "Zenith Logistics Private Limited"); ledger.create_invoice(c, cid, 5000, "2026-01-01")
    rid = c.execute("SELECT id FROM resellers WHERE name='IDFC FIRST Bank'").fetchone()[0]
    c2 = cust(c, "Beta Foods Pvt Ltd", reseller_id=rid, reseller_share_pct=100); ledger.create_invoice(c, c2, 8000, "2026-01-01")
    rows = [{"date": "2026-02-02", "amount": 5000, "narration": "NEFT ZENITH LOGISTICS PVT LTD", "reference": "U1"},
            {"date": "2026-02-02", "amount": 8000, "narration": "RTGS IDFC FIRST BANK LTD", "reference": "U2"},
            {"date": "2026-02-02", "amount": 70, "narration": "random credit", "reference": "U3"}]
    s = ledger.import_bank_rows(c, rows); assert s["auto_applied"] == 2 and s["unmatched"] == 1
    assert ledger.outstanding(c, cid) == 0 and ledger.reseller_outstanding(c, rid) == 0
    assert ledger.import_bank_rows(c, rows)["duplicate"] == 3

# ---------------------------------------------------------------- bulk uploads
def xl(header, rows):
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(header); [ws.append(r) for r in rows]
    p = os.path.join(tempfile.mkdtemp(), "x.xlsx"); wb.save(p); return importers.read_table(p)

def test_bulk_collections_validate_then_apply(c):
    cid = cust(c); months(c, cid)
    rows = xl(["customer", "date", "mode", "amount", "tds", "reference", "invoice_no"],
              [["Acme Pvt Ltd", "2025-04-02", "BANK", 1000, 0, "U1", "I202501"], ["Nobody Ltd", "2025-04-02", "BANK", 5, 0, "", ""],
               ["Acme Pvt Ltd", "2025-04-03", "WALLET", 1900, 100, "OPS1", ""]])
    r = importers.bulk_collections(c, rows, apply=False); assert r["ok"] == 2 and len(r["errors"]) == 1
    assert ledger.outstanding(c, cid) == 6000                                    # preview changed nothing
    r = importers.bulk_collections(c, rows, apply=True); assert ledger.outstanding(c, cid) == 3000

def test_bulk_invoices_add_update_cancel(c):
    cid = cust(c)
    rows = xl(["customer", "invoice_no", "invoice_date", "amount", "description", "kind", "action"],
              [["Acme Pvt Ltd", "N1", "2026-05-01", 1000, "x", "OTHER", "ADD"], ["Acme Pvt Ltd", "N2", "2026-05-01", 500, "", "OTHER", "ADD"],
               ["Acme Pvt Ltd", "N1", "", 1200, "", "", "UPDATE"], ["Acme Pvt Ltd", "N2", "", "", "", "", "CANCEL"], ["Acme Pvt Ltd", "N9", "", "", "", "", "CANCEL"]])
    r = importers.bulk_invoices(c, rows[:2], apply=True); assert r["ok"] == 2
    r = importers.bulk_invoices(c, rows[2:], apply=True); assert r["ok"] == 2 and len(r["errors"]) == 1
    assert ledger.outstanding(c, cid) == 1200

def test_bulk_customers_sets_terms_and_split(c):
    cust(c)
    rows = xl(["name", "credit_days", "reseller", "reseller_share_pct", "whatsapp", "auto_debit_allowed"], [["Acme Pvt Ltd", 30, "IDFC FIRST Bank", 40, "9810012345", "no"], ["New Co", 15, "", "", "", ""]])
    r = importers.bulk_customers(c, rows, apply=True); assert r["ok"] == 2
    x = c.execute("SELECT * FROM customers WHERE name='Acme Pvt Ltd'").fetchone(); assert (x["credit_days"], x["reseller_share_pct"], x["auto_debit_allowed"]) == (30, 40, 0)
    assert c.execute("SELECT credit_days FROM customers WHERE name='New Co'").fetchone()[0] == 15

# ---------------------------------------------------------------- zoho
class FakeZoho:
    def invoices(self, s, e):
        mk = lambda i, n, st, bal, d="2026-09-30": dict(invoice_id=i, customer_id="Z1", customer_name="Acme Pvt Ltd", invoice_number=n, date=d, due_date=d, total=1000, balance=bal, status=st, last_payment_date="2026-10-05", email="a@x")
        return [mk("1", "ZI-1", "sent", 1000), mk("2", "ZI-2", "paid", 0), mk("3", "OLD-1", "sent", 1000), mk("4", "ZI-4", "draft", 1000)]

def test_zoho_import_links_existing_closes_paid_and_is_idempotent(c):
    cid = cust(c); ledger.create_invoice(c, cid, 1000, "2026-09-01", invoice_no="OLD-1")
    z = FakeZoho().invoices("", ""); pv = {r["number"]: r["import_status"] for r in zoho.preview(c, z)}
    assert pv == {"ZI-1": "NEW", "ZI-2": "NEW", "OLD-1": "LINK_EXISTING", "ZI-4": "IGNORED"}
    r = zoho.import_invoices(c, z); assert (r["created"], r["linked"], r["closed_as_paid"]) == (2, 1, 1)
    assert ledger.outstanding(c, cid) == 2000                                  # OLD-1 + ZI-1 open, ZI-2 closed as paid
    r2 = zoho.import_invoices(c, z); assert r2["created"] == 0                  # nothing duplicated

# ---------------------------------------------------------------- reminders
def test_stages_dryrun_gap_and_promise(c):
    cid = cust(c); ledger.create_invoice(c, cid, 5000, "2026-01-01")        # due 2026-01-16
    day = datetime.date(2026, 1, 24)                                            # 8 days overdue -> stage 2
    p = [x for x in reminders.plan(c, day) if x["id"] == cid][0]; assert p["action"] == "REMIND" and p["stage"] == 2 and p["channels"] == ["EMAIL"]
    reminders.run(c, day, send_mode="DRY_RUN")
    assert [x for x in reminders.plan(c, day) if x["id"] == cid][0]["action"] == "REMIND"      # dry-run does not advance
    c.execute("INSERT INTO reminder_log(customer_id,stage,channel,sent_at,status) VALUES(?,?,?,?,?)", (cid, 2, "EMAIL", "2026-01-24T09:00:00", "SENT"))
    assert reminders.plan(c, datetime.date(2026, 1, 27))[0]["reason"].startswith("contacted")
    assert reminders.plan(c, datetime.date(2026, 2, 10))[0]["stage"] == 3
    c.execute("UPDATE customers SET promise_date='2026-02-20' WHERE id=?", (cid,))
    assert "promised" in reminders.plan(c, datetime.date(2026, 2, 10))[0]["reason"]

def test_disputed_invoice_not_chased_and_whatsapp_channel(c):
    cid = cust(c); i = ledger.create_invoice(c, cid, 5000, "2026-01-01")[0]; ledger.set_dispute(c, i, True, "outage")
    assert reminders.plan(c, datetime.date(2026, 3, 1)) == []                   # only invoice is disputed
    ledger.set_dispute(c, i, False); db.set_setting(c, "channel_whatsapp", "1")
    p = reminders.plan(c, datetime.date(2026, 3, 1))[0]; assert p["channels"] == ["EMAIL", "WHATSAPP"]
    m = reminders.compose(c, p["party"], p["stage"], "WHATSAPP", datetime.date(2026, 3, 1))
    assert m["subject"] == "dues_reminder_4" and m["params"][1] == "5,000"

def test_reseller_gets_own_consolidated_reminder(c):
    rid = c.execute("SELECT id FROM resellers WHERE name='IDFC FIRST Bank'").fetchone()[0]
    c.execute("UPDATE resellers SET contact_emails='ops@idfc.example' WHERE id=?", (rid,))
    cid = cust(c, reseller_id=rid, reseller_share_pct=50); ledger.create_invoice(c, cid, 10000, "2026-01-01", invoice_no="B1")
    plan = {x["party"]: x for x in reminders.plan(c, datetime.date(2026, 3, 1))}
    assert plan[f"R:{rid}"]["total"] == 5000 and plan[f"C:{cid}"]["total"] == 5000
    m = reminders.compose(c, f"R:{rid}", 4, "EMAIL", datetime.date(2026, 3, 1)); assert "Acme Pvt Ltd" in m["body"] and m["to"] == ["ops@idfc.example"]

def test_demo_mode_never_sends_live(c):
    cid = cust(c); ledger.create_invoice(c, cid, 5000, "2026-01-01"); db.set_setting(c, "is_demo", "1")
    r = reminders.run(c, datetime.date(2026, 3, 1), send_mode="LIVE"); assert r["dry_run"] >= 1 and r["sent"] == 0

def test_template_sandbox_blocks_unsafe_expressions(c):
    cid = cust(c); ledger.create_invoice(c, cid, 5000, "2026-01-01")
    c.execute("UPDATE templates SET body=? WHERE key='EMAIL:1'", ("{{ ''.__class__.__mro__[1].__subclasses__() }}",))
    with pytest.raises(Exception): reminders.compose(c, f"C:{cid}", 1, "EMAIL")

def test_indian_number_format():
    assert reminders.inr(709855) == "7,09,855" and reminders.inr(1796313) == "17,96,313" and reminders.inr(950) == "950"

# ---------------------------------------------------------------- zoho hold / file import / deductions
def test_zoho_open_invoices_are_held_until_released(c):
    cid = cust(c, "Acme Private Limited")
    z = [dict(invoice_id="9", customer_id="Z9", customer_name="ACME PVT LTD", invoice_number="ZZ-9", date="2026-08-01", due_date="2026-08-01",
              total=1000, balance=1000, status="overdue", email="accounts@eroute.in")]
    r = zoho.import_invoices(c, z); assert r["created"] == 1 and r["held"] == 1 and r["new_customers"] == 0      # loose name match, no duplicate customer
    assert c.execute("SELECT credit_days FROM customers WHERE id=?", (cid,)).fetchone()[0] == 15
    assert reminders.plan(c, datetime.date(2026, 10, 1)) == []                                                   # held -> never chased
    assert ledger.release_hold(c, source="ZOHO") == 1
    assert reminders.plan(c, datetime.date(2026, 10, 1))[0]["action"] == "REMIND"

def test_zoho_new_customer_does_not_copy_our_own_mailbox(c):
    z = [dict(invoice_id="1", customer_id="Z1", customer_name="Brand New Co", invoice_number="N-1", date="2026-08-01", due_date="2026-08-01",
              total=500, balance=500, status="sent", email="accounts@eroute.in")]
    zoho.import_invoices(c, z); assert c.execute("SELECT email_to FROM customers WHERE name='Brand New Co'").fetchone()[0] == ""

def test_zoho_export_file_rows_are_normalised(c):
    rows = xl(["Invoice Date", "Invoice Number", "Invoice Status", "Customer Name", "Total", "Balance", "Due Date"],
              [["2026-09-30", "INV-5", "Partially Paid", "File Co", 1000, 400, "2026-09-30"], ["", "bad", "", "", "", "", ""]])
    inv = zoho.invoices_from_rows(rows); assert len(inv) == 1 and inv[0]["status"] == "partially_paid" and inv[0]["invoice_id"] == "file:INV-5"
    r = zoho.import_invoices(c, inv, hold_open=False)
    cid = c.execute("SELECT id FROM customers WHERE name='File Co'").fetchone()[0]; assert ledger.outstanding(c, cid) == 400

def test_deductions_file_defaults_to_wallet_mode(c):
    cid = cust(c); months(c, cid)
    rows = xl(["customer", "date", "amount", "reference"], [["Acme Pvt Ltd", "2025-04-15", 1500, "OPS-1"]])
    r = importers.bulk_collections(c, rows, apply=True, default_mode="WALLET"); assert r["ok"] == 1
    assert c.execute("SELECT mode FROM payments").fetchone()[0] == "WALLET" and ledger.outstanding(c, cid) == 4500

def test_zoho_file_without_customer_ids_never_cross_assigns(c):
    a = cust(c, "Acme Pvt Ltd")
    rows = xl(["Invoice Date", "Invoice Number", "Invoice Status", "Customer Name", "Total", "Balance"],
              [["2026-09-30", "A-1", "Sent", "Acme Pvt Ltd", 100, 100], ["2026-09-30", "B-1", "Sent", "Other Co", 200, 200], ["2026-09-30", "C-1", "Paid", "Third Co", 300, 0]])
    zoho.import_invoices(c, zoho.invoices_from_rows(rows))
    got = {r[0]: r[1] for r in c.execute("SELECT i.invoice_no, c.name FROM invoices i JOIN customers c ON c.id=i.customer_id")}
    assert got == {"A-1": "Acme Pvt Ltd", "B-1": "Other Co", "C-1": "Third Co"}

# ---------------------------------------------------------------- reports, backup, merge, send-now
from app import reports, backup

def test_reports_tie_out_to_outstanding(c):
    a = cust(c); b = cust(c, "B Ltd", kam="Sumit"); months(c, a, start="2026-05-01"); months(c, b, (500, 700), start="2026-06-01")
    ledger.record_payment(c, a, 1000, "BANK")
    m = reports.matrix(c); tot = ledger.outstanding(c, a) + ledger.outstanding(c, b)
    assert m["grand"] == tot == sum(r["total"] for r in m["rows"]) and "2026-06" in m["months"] and "2026-05" not in m["months"]
    ag = reports.ageing(c); assert ag["grand"] == tot and len(ag["totals"]) == 5
    assert reports.matrix(c, kam="Sumit")["grand"] == 1200
    assert reports.matrix_xlsx(c)[:2] == b"PK" and reports.all_data_xlsx(c)[:2] == b"PK"
    assert sum(r["billed"] for r in reports.trend(c)) >= 0

def test_backup_is_restorable_and_pruned(c, tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("DUES_BACKUP_DIR", str(tmp_path)); cid = cust(c); months(c, cid)
    for i in range(4): p = backup.run(c, keep=2, tag=f"-{i}")
    assert len(list(tmp_path.glob("dues-*.db"))) == 2
    assert sqlite3.connect(p).execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 3

def test_merge_customers_moves_everything_and_blocks_clashes(c):
    a = cust(c, "Alpha Pvt Ltd"); b = cust(c, "Alpha Private Limited", whatsapp="919800000009")
    assert [sorted(g) for g in ledger.duplicate_groups(c)] == [sorted([a, b])]
    ledger.create_invoice(c, a, 100, "2026-01-01", invoice_no="X1"); ledger.create_invoice(c, b, 50, "2026-02-01", invoice_no="Y1")
    ledger.record_payment(c, b, 20, "BANK")
    ledger.merge_customers(c, b, a)
    assert c.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1 and ledger.outstanding(c, a) == 130
    assert c.execute("SELECT whatsapp FROM customers WHERE id=?", (a,)).fetchone()[0] in ("919900000001", "919800000009")
    d = cust(c, "Alpha Foods"); ledger.create_invoice(c, d, 10, "2026-01-01", invoice_no="X1")
    with pytest.raises(ValueError): ledger.merge_customers(c, d, a)

def test_send_now_ignores_gap_but_respects_dryrun_and_holds(c):
    cid = cust(c); ledger.create_invoice(c, cid, 5000, "2026-01-01")
    c.execute("INSERT INTO reminder_log(customer_id,stage,channel,sent_at,status) VALUES(?,?,?,?,?)", (cid, 4, "EMAIL", db.now(), "SENT")); c.commit()
    r = reminders.send_now(c, f"C:{cid}", ["EMAIL"]); assert r["results"] == {"EMAIL": "DRY_RUN"} and r["stage"] >= 1
    c.execute("UPDATE invoices SET hold=1"); c.commit()
    with pytest.raises(ValueError): reminders.send_now(c, f"C:{cid}")

def test_bulk_customers_remind_flag_and_notes(c):
    cust(c); rows = xl(["name", "remind", "notes"], [["Acme Pvt Ltd", "no", "legal case pending"]])
    importers.bulk_customers(c, rows, apply=True); x = c.execute("SELECT remind, notes FROM customers").fetchone(); assert (x[0], x[1]) == (0, "legal case pending")

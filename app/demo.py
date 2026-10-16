"""Synthetic demo data so you can click through everything without touching real customer data.
All names/e-mails/phones are fake (example TLDs). While is_demo=1 the reminder engine is forced into DRY_RUN."""
import csv, datetime, io, random
from . import ledger, db
from .db import set_setting, now

A = ["Acme", "Zenith", "Nimbus", "Orbit", "Pinnacle", "Quanta", "Redwood", "Sunrise", "Tandem", "Umbra", "Vertex", "Willow", "Apex", "Bluefin", "Cobalt", "Delta", "Ember", "Falcon", "Granite", "Horizon", "Indigo", "Juniper", "Kestrel", "Lumen", "Meridian", "Nova", "Onyx", "Prism", "Quill", "Rivet", "Summit", "Titan", "Unity", "Vista", "Wren", "Xenon", "Yonder", "Zephyr", "Alder", "Birch", "Cedar", "Dune", "Elm"]
B = ["Retail", "Logistics", "Foods", "Finserv", "Mobility", "Pharma", "Textiles", "Realty", "Payments", "Hospitality", "Labs", "Traders"]
KAMS = ["Tanisha Verma", "Abhishek Srivastava", "Akanksha Gupta", "Sumit Tyagi", "Roshni Khatri"]

def load(conn, seed=7):
    rnd = random.Random(seed); today = datetime.date.today()
    db.wipe_business_data(conn); set_setting(conn, "is_demo", "1"); set_setting(conn, "channel_whatsapp", "1")
    conn.execute("UPDATE resellers SET contact_emails=? , whatsapp=? WHERE name='IDFC FIRST Bank'", ("partner-ops@idfc-demo.example", "919900099001"))
    conn.execute("UPDATE resellers SET contact_emails=? , whatsapp=? WHERE name='Stride One'", ("billing@stride-demo.example", "919900099002"))
    rs = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM resellers")}
    months = []
    d = today.replace(day=1)
    for _ in range(7):
        months.append(d); d = (d - datetime.timedelta(days=1)).replace(day=1)
    months.reverse()
    names = [f"{a} {b} Pvt Ltd" for a, b in zip(A, (B * 4)[:len(A)])]
    prof = ["good"] * 14 + ["slow"] * 10 + ["bad"] * 8 + ["wallet"] * 6 + ["tds"] * 5 + ["good"] * (len(names) - 43)
    rnd.shuffle(prof)
    for i, (nm, pf) in enumerate(zip(names, prof), start=1):
        slug = nm.split()[0].lower() + str(i)
        reseller, share = None, 0
        if i % 9 == 0: reseller, share = rs["IDFC FIRST Bank"], 50
        elif i % 14 == 0: reseller, share = rs["Stride One"], 30
        cid = conn.execute("""INSERT INTO customers(name,kam,contact_person,email_to,email_cc,whatsapp,credit_days,reseller_id,reseller_share_pct,collection_mode,
            auto_debit_allowed,wallet_balance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (nm, KAMS[i % 5], rnd.choice(["Ravi", "Meera", "Imran", "Sneha", "Karthik"]), "" if i in (11, 37) else f"accounts@{slug}.example",
             f"cfo@{slug}.example" if i % 4 == 0 else "", f"9199000{i:05d}" if i % 3 else "", rnd.choice([15] * 8 + [30, 45, 7]), reseller, share,
             "WALLET" if pf == "wallet" else "BANK", 0 if i % 10 == 0 else 1, 0)).lastrowid
        base = rnd.choice([5900, 17700, 29500, 41300, 59000, 88500, 118000, 177000])
        invs = []
        for m in months:
            amt = round(base * rnd.uniform(0.9, 1.15), 0)
            invd = min(ledger.month_end(m.isoformat()), today.isoformat())
            ids = ledger.create_invoice(conn, cid, amt, invd, "SPEND_MGMT", f"INV-{m:%Y%m}-{i:03d}", f"Platform fee {m:%b %Y}",
                                        source="MIGRATION", commit=False)
            invs.append(ids)
        pay_n = {"good": len(invs) - 1, "slow": 3, "bad": 0, "wallet": 2, "tds": len(invs) - 1}[pf]
        for ids in invs[:pay_n]:
            for iid in ids:
                inv = conn.execute("SELECT * FROM invoices WHERE id=?", (iid,)).fetchone()
                when = min((datetime.date.fromisoformat(inv["due_date"]) + datetime.timedelta(days=rnd.randint(-3, 12))).isoformat(), today.isoformat())
                kw = dict(reseller_id=(reseller if inv["payer"] == "RESELLER" else None))
                cust = None if inv["payer"] == "RESELLER" else cid
                if pf == "tds" and inv["payer"] == "CUSTOMER":
                    tds = round(inv["amount"] * 0.02, 2)
                    ledger.record_payment(conn, cust, inv["amount"] - tds, "BANK", when, f"UTR{rnd.randint(10**9, 10**10-1)}", tds=tds, invoice_ids=[iid], commit=False, **kw)
                else:
                    mode = "WALLET" if pf == "wallet" else "BANK"
                    ledger.record_payment(conn, cust, inv["amount"], mode, when, f"UTR{rnd.randint(10**9, 10**10-1)}", invoice_ids=[iid], commit=False, **kw)
        if pf == "wallet":
            conn.execute("UPDATE customers SET wallet_balance=? WHERE id=?", (round(base * rnd.choice([0.3, 0.8, 2.0]), 0), cid))
        if pf == "bad" and i % 2 == 0:       # earlier reminders already went out
            for st, ago in ((1, 35), (2, 24), (3, 8)):
                conn.execute("INSERT INTO reminder_log(customer_id,stage,channel,sent_at,to_addr,outstanding,status,subject) VALUES(?,?,?,?,?,?,?,?)",
                             (cid, st, "EMAIL", (datetime.datetime.now() - datetime.timedelta(days=ago)).isoformat(timespec="seconds"),
                              f"accounts@{slug}.example", base * 3, "SENT", f"Demo reminder stage {st}"))
    # promises, a dispute, notes
    cs = [r["id"] for r in conn.execute("SELECT id FROM customers WHERE name IN (SELECT name FROM customers) ORDER BY id")]
    bad = [r["customer_id"] for r in conn.execute("SELECT DISTINCT customer_id FROM invoices WHERE status='OPEN' AND payer='CUSTOMER' AND due_date < ? ORDER BY customer_id", (today.isoformat(),))]
    conn.execute("UPDATE customers SET promise_date=?, promise_amount=50000, promise_note='CFO promised NEFT by Friday' WHERE id=?", ((today + datetime.timedelta(days=3)).isoformat(), bad[0]))
    conn.execute("UPDATE customers SET promise_date=?, promise_amount=30000, promise_note='Promised last week - not received' WHERE id=?", ((today - datetime.timedelta(days=4)).isoformat(), bad[2]))
    inv = conn.execute("SELECT id FROM invoices WHERE customer_id=? AND payer='CUSTOMER' AND status='OPEN' LIMIT 1", (bad[4],)).fetchone()
    ledger.set_dispute(conn, inv["id"], True, "Customer says service was down in this month")
    conn.execute("INSERT INTO notes(customer_id,at,author,kind,text) VALUES(?,?,?,?,?)", (bad[4], now(), "Tanisha Verma", "DISPUTE", "Disputes platform fee for the outage month; raising CN request."))
    conn.execute("INSERT INTO notes(customer_id,at,author,kind,text) VALUES(?,?,?,?,?)", (bad[1], now(), "Sumit Tyagi", "CALL", "Called accounts head - says payment run is on the 25th."))
    # bank statement rows to review
    ledger.import_bank_rows(conn, _bank_rows(conn, today))
    conn.commit(); db.audit(conn, "demo_load", "")
    return {"customers": len(names)}

def _bank_rows(conn, today):
    rows = []
    cands = conn.execute("SELECT c.id,c.name FROM customers c WHERE EXISTS (SELECT 1 FROM invoices i WHERE i.customer_id=c.id AND i.payer='CUSTOMER' AND i.status='OPEN') ORDER BY c.id LIMIT 12").fetchall()
    for n, c in enumerate(cands[:4]):       # exact-amount matches -> auto-applied
        inv = ledger.open_invoices(conn, c["id"], "CUSTOMER")[0]
        rows.append({"date": (today - datetime.timedelta(days=n + 1)).isoformat(), "amount": round(inv["balance"], 2),
                     "narration": f"NEFT-{c['name'].upper().replace(' PVT LTD','')} PVT LTD-FEE", "reference": f"UTR9{n:09d}"})
    c = cands[5]; rows.append({"date": today.isoformat(), "amount": 12345.0, "narration": f"IMPS {c['name'].split()[0]} {c['name'].split()[1]}", "reference": "UTR7700000001"})
    rows.append({"date": today.isoformat(), "amount": 777.0, "narration": "UPI/unknown remitter/refund", "reference": "UTR7700000002"})
    return rows

def sample_bank_csv(conn):
    today = datetime.date.today(); out = io.StringIO(); w = csv.writer(out)
    w.writerow(["Txn Date", "Narration", "Chq/Ref No", "Withdrawal", "Deposit"])
    cands = conn.execute("SELECT c.id,c.name FROM customers c WHERE EXISTS (SELECT 1 FROM invoices i WHERE i.customer_id=c.id AND i.payer='CUSTOMER' AND i.status IN ('OPEN','PARTIAL')) ORDER BY c.id DESC LIMIT 5").fetchall()
    for n, c in enumerate(cands):
        inv = ledger.open_invoices(conn, c["id"], "CUSTOMER")[0]
        w.writerow([(today - datetime.timedelta(days=n)).strftime("%d/%m/%Y"), f"RTGS {c['name'].upper()} INV PAYMENT", f"SAMPLE{n:08d}", "", f"{inv['balance']:.2f}"])
    w.writerow([today.strftime("%d/%m/%Y"), "ATM CASH WDL", "ATM1", "2000.00", ""])
    w.writerow([today.strftime("%d/%m/%Y"), "UPI random credit", "SAMPLE9999", "", "431.00"])
    return out.getvalue()

def reset(conn):
    db.wipe_business_data(conn); set_setting(conn, "is_demo", "0"); set_setting(conn, "channel_whatsapp", "0"); conn.commit()

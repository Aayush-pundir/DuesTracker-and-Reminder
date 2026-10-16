"""Excel importers.
 * import_consol / import_contacts : ONE-TIME migration of the legacy Excel files (locked afterwards - the app is the source of truth).
 * bulk_invoices / bulk_collections / bulk_customers : everyday bulk upload (validate first, then apply).
 * read_bank_statement : CSV/XLSX bank statement -> credit rows for ledger.import_bank_rows."""
import csv, datetime, io, re
import openpyxl
from .db import audit, get_settings, now
from . import ledger

# ------------------------------------------------------------------ helpers
def _month(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return datetime.date(v.year, v.month, 1).isoformat()
    if isinstance(v, str):
        for fmt in ("%b %Y", "%B %Y", "%Y-%m-%d"):
            try:
                d = datetime.datetime.strptime(v.strip(), fmt); return datetime.date(d.year, d.month, 1).isoformat()
            except ValueError:
                pass
    return None

def _date(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.date().isoformat() if isinstance(v, datetime.datetime) else v.isoformat()
    s = str(v or "").strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None

def _num(v):
    try:
        return float(str(v).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return 0.0

def _key(name):
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()

def _clean(name):
    return re.sub(r"\s+", " ", str(name)).strip()

def _customer(conn, cache, name):
    """Find-or-create by whitespace/case-insensitive name. `cache` is shared for one import run."""
    if not cache.get("_loaded"):
        cache["_loaded"] = True
        dc = get_settings(conn)["default_credit_days"]; cache["_cd"] = int(dc)
        for r in conn.execute("SELECT id,name FROM customers"):
            cache[_key(r["name"])] = r["id"]
    k = _key(name)
    if k not in cache:
        cache[k] = conn.execute("INSERT INTO customers(name,credit_days) VALUES(?,?)", (_clean(name), cache["_cd"])).lastrowid
    return cache[k]

def _tag_reseller(conn, cid):
    c = conn.execute("SELECT name,email_to,reseller_id FROM customers WHERE id=?", (cid,)).fetchone()
    if c["reseller_id"]:
        return
    for rs in conn.execute("SELECT * FROM resellers"):
        doms = [d.strip().lower() for d in rs["email_domains"].split(",") if d.strip()]
        marks = [m.strip().lower() for m in rs["name_markers"].split(",") if m.strip()]
        if any(d in (c["email_to"] or "").lower() for d in doms) or any(m in c["name"].lower() for m in marks):
            conn.execute("UPDATE customers SET reseller_id=? WHERE id=?", (rs["id"], cid)); return

def normalize_kams(conn):
    """'Tanisha' / 'tanisha verma' / 'Tanisha Verma' -> one spelling."""
    names = {r[0] for r in conn.execute("SELECT DISTINCT kam FROM customers WHERE kam!=''")}
    full = {n.title() for n in names if len(n.split()) > 1}
    for n in names:
        t = n.title()
        if len(t.split()) == 1:
            m = [f for f in full if f.split()[0] == t]
            t = m[0] if len(m) == 1 else t
        conn.execute("UPDATE customers SET kam=? WHERE kam=?", (t, n))

# ------------------------------------------------------------------ one-time migration
def _upsert_legacy(conn, cid, kind, period, no, pending, desc, invoice_date):
    row = conn.execute("SELECT id FROM invoices WHERE customer_id=? AND payer='CUSTOMER' AND kind=? AND period=? AND invoice_no=?",
                       (cid, kind, period, no)).fetchone()
    if row:
        paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM allocations WHERE invoice_id=?", (row["id"],)).fetchone()[0]
        conn.execute("UPDATE invoices SET amount=?, description=? WHERE id=?", (pending + paid, desc, row["id"])); ledger.refresh_invoice(conn, row["id"])
        return "updated"
    ledger.create_invoice(conn, cid, pending, invoice_date=invoice_date, kind=kind, invoice_no=no, description=desc,
                          source="MIGRATION", period=period, split=False, commit=False)
    return "created"

def import_consol(conn, path):
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    cache, rep = {}, {"spend_mgmt": {"created": 0, "updated": 0}, "other": {"created": 0, "updated": 0}}
    ws = wb["Summary SM"]; rows = list(ws.iter_rows(min_row=2, values_only=True))
    months = {i: _month(v) for i, v in enumerate(rows[0]) if i >= 3 and _month(v)}
    for r in rows[1:]:
        name = r[0]
        if not name or _key(name) == "grand total":
            continue
        cid = _customer(conn, cache, name)
        kam = _clean(r[2] or "")
        conn.execute("UPDATE customers SET enterprise_id=COALESCE(?,enterprise_id) WHERE id=?", (r[1], cid))
        if re.search(r"freeze|stopped", kam, re.I):
            conn.execute("UPDATE customers SET status='FROZEN', remind=0 WHERE id=?", (cid,))
        elif kam:
            conn.execute("UPDATE customers SET kam=? WHERE id=?", (kam, cid))
        for i, p in months.items():
            v = _num(r[i]) if i < len(r) else 0
            if v > 0.5:
                rep["spend_mgmt"][_upsert_legacy(conn, cid, "SPEND_MGMT", p, "", v, "Platform fee / spend management", ledger.month_end(p))] += 1
    for r in wb["Other than S Mgt"].iter_rows(min_row=2, values_only=True):
        if not (r[1] and isinstance(r[3], (int, float)) and r[4]):
            continue
        cid = _customer(conn, cache, r[1]); p = _month(r[5]) or ledger.today()[:8] + "01"
        if r[7]:
            conn.execute("UPDATE customers SET kam=COALESCE(NULLIF(kam,''),?) WHERE id=?", (_clean(r[7]), cid))
        rep["other"][_upsert_legacy(conn, cid, "OTHER", p, str(r[4]).strip(), float(r[3]), str(r[6] or "")[:200], ledger.month_end(p))] += 1
        if r[8]:
            conn.execute("INSERT INTO notes(customer_id,at,author,kind,text) VALUES(?,?,?,?,?)", (cid, now(), "migration", "NOTE", str(r[8])[:300]))
    if "Spend Management" in wb.sheetnames:
        seen = set()
        for r in wb["Spend Management"].iter_rows(min_row=3, max_col=11, values_only=True):
            if not r[1] or _key(r[1]) in ("grand total", "total") or _key(r[1]) in seen:
                continue
            seen.add(_key(r[1])); cid = _customer(conn, cache, r[1]); ops = _clean(r[5] or ""); o = ops.lower()
            conn.execute("UPDATE customers SET ops_status=?, auto_debit_allowed=?, collection_mode=CASE WHEN ? THEN 'WALLET' ELSE collection_mode END,"
                         " email_to=CASE WHEN email_to='' THEN ? ELSE email_to END, enterprise_id=COALESCE(enterprise_id,?) WHERE id=?",
                         (ops, 0 if o.startswith("do not auto debit") else 1, 1 if "running balance" in o else 0, str(r[10] or ""), r[9], cid))
            if r[6] and str(r[6]).strip().lower() not in ("pending", ""):
                conn.execute("INSERT INTO notes(customer_id,at,author,kind,text) VALUES(?,?,?,?,?)", (cid, now(), "migration", "NOTE", str(r[6])[:300]))
    for k, cid in cache.items():
        if not k.startswith("_"):
            _tag_reseller(conn, cid)
    if "Stride Billing" in wb.sheetnames:
        sid = conn.execute("SELECT id FROM resellers WHERE name='Stride One'").fetchone()["id"]
        for r in wb["Stride Billing"].iter_rows(min_row=2, max_col=1, values_only=True):
            if r[0] and _key(r[0]) in cache:
                conn.execute("UPDATE customers SET reseller_id=COALESCE(reseller_id,?) WHERE id=?", (sid, cache[_key(r[0])]))
    normalize_kams(conn)
    audit(conn, "migration_consol", str(rep)); conn.commit()
    rep["customers"] = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    return rep

def import_contacts(conn, path):
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True, keep_vba=False)
    n, cache = 0, {}
    for r in wb["DATA"].iter_rows(min_row=3, max_col=3, values_only=True):
        if not r[0]:
            continue
        cid = _customer(conn, cache, r[0])
        conn.execute("UPDATE customers SET email_to=COALESCE(NULLIF(?,''),email_to), email_cc=COALESCE(NULLIF(?,''),email_cc) WHERE id=?",
                     (r[1] or "", r[2] or "", cid))
        _tag_reseller(conn, cid); n += 1
    conn.commit(); audit(conn, "migration_contacts", str(n))
    return {"contacts_updated": n}

# ------------------------------------------------------------------ generic table reader + templates
def read_table(path):
    if path.lower().endswith(".csv"):
        data = list(csv.reader(open(path, newline="", encoding="utf-8-sig")))
    else:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        data = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    data = [r for r in data if any(c not in (None, "") for c in r)]
    if not data:
        return []
    hdr = [re.sub(r"[^a-z0-9]+", "_", str(h or "").lower()).strip("_") for h in data[0]]
    return [dict(zip(hdr, r)) for r in data[1:]]

TEMPLATES = {
    "invoices": (["customer", "invoice_no", "invoice_date", "amount", "description", "kind", "action"],
                 [["Acme Retail Pvt Ltd", "INV-1001", "2026-10-01", 25000, "Platform fee Oct 2026", "OTHER", "ADD"],
                  ["Acme Retail Pvt Ltd", "INV-0990", "", 24000, "", "", "UPDATE"], ["Acme Retail Pvt Ltd", "INV-0980", "", "", "", "", "CANCEL"]],
                 "action = ADD | UPDATE (amount/date/description of an existing invoice no.) | CANCEL. kind = SPEND_MGMT | OTHER | BILLING. Reseller split is applied automatically on ADD."),
    "collections": (["customer", "date", "mode", "amount", "tds", "reference", "invoice_no", "narration"],
                    [["Acme Retail Pvt Ltd", "2026-10-15", "BANK", 20000, 500, "UTR123456", "", "NEFT Acme"],
                     ["Acme Retail Pvt Ltd", "2026-10-16", "WALLET", 5000, 0, "OPS-77", "INV-1001", "Deducted from wallet"]],
                    "mode = BANK | WALLET | AUTO_DEBIT | CREDIT_NOTE. customer may also be a reseller name. tds = tax withheld (settles the invoice too). invoice_no optional - blank = oldest dues first."),
    "deductions": (["customer", "date", "amount", "reference", "invoice_no", "narration"],
                   [["Acme Retail Pvt Ltd", "2026-10-15", 5000, "OPS-BATCH-OCT-1", "", "Deducted from platform balance"]],
                   "Fortnightly / monthly file of balances deducted on the ops platform. Every row settles that customer's oldest dues (or the invoice_no given). Mode is WALLET automatically."),
    "customers": (["name", "kam", "contact_person", "email_to", "email_cc", "whatsapp", "credit_days", "reseller", "reseller_share_pct", "wallet_balance", "collection_mode", "auto_debit_allowed", "remind", "status", "notes"],
                  [["Acme Retail Pvt Ltd", "Tanisha Verma", "Ravi", "accounts@acme.com", "", "919810012345", 15, "", "", 0, "BANK", "yes", "yes", "ACTIVE", ""]],
                  "Upsert by name; blank cells are left unchanged. reseller_share_pct = % of each new invoice the reseller pays."),
}

def template_workbook(kind):
    cols, ex, note = TEMPLATES[kind]
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = kind
    ws.append(cols)
    for r in ex:
        ws.append(r)
    ws2 = wb.create_sheet("Instructions"); ws2.append([note])
    for c in ws[1]:
        c.font = openpyxl.styles.Font(bold=True)
    for i, c in enumerate(cols, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = 22
    b = io.BytesIO(); wb.save(b); return b.getvalue()

def _find_customer(conn, name):
    r = conn.execute("SELECT id FROM customers WHERE lower(name)=?", (_key(name),)).fetchone()
    return r["id"] if r else None

def _find_reseller(conn, name):
    r = conn.execute("SELECT id FROM resellers WHERE lower(name)=?", (_key(name),)).fetchone()
    return r["id"] if r else None

# ------------------------------------------------------------------ bulk: invoices
def bulk_invoices(conn, rows, apply=False):
    out = {"ok": 0, "errors": [], "preview": []}
    for n, r in enumerate(rows, start=2):
        act = str(r.get("action") or "ADD").strip().upper(); no = str(r.get("invoice_no") or "").strip()
        cid = _find_customer(conn, r.get("customer"))
        try:
            if not cid:
                raise ValueError(f"unknown customer '{r.get('customer')}'")
            amt = _num(r.get("amount")); d = _date(r.get("invoice_date")) if r.get("invoice_date") else None
            if r.get("invoice_date") and not d:
                raise ValueError("bad invoice_date")
            kind = str(r.get("kind") or "OTHER").upper()
            if kind not in ("SPEND_MGMT", "OTHER", "BILLING"):
                raise ValueError("bad kind")
            parts = conn.execute("SELECT * FROM invoices WHERE customer_id=? AND invoice_no=? AND status!='CANCELLED'", (cid, no)).fetchall() if no else []
            if act == "ADD":
                if amt <= 0: raise ValueError("amount required")
                if parts: raise ValueError(f"invoice {no} already exists")
            elif act in ("UPDATE", "CANCEL"):
                if not parts: raise ValueError(f"invoice {no or '(blank)'} not found")
                if act == "UPDATE" and amt <= 0 and not r.get("description") and not d: raise ValueError("nothing to update")
            else:
                raise ValueError("action must be ADD, UPDATE or CANCEL")
            out["preview"].append(f"{act} {r.get('customer')} {no} {amt or ''}")
            if apply:
                if act == "ADD":
                    ledger.create_invoice(conn, cid, amt, d, kind, no, str(r.get("description") or ""), source="BULK", commit=False)
                elif act == "CANCEL":
                    for p in parts: ledger.cancel_invoice(conn, p["id"], "bulk cancel")
                else:
                    tot = sum(p["amount"] for p in parts)
                    for p in parts:
                        ledger.update_invoice(conn, p["id"], amount=round(amt * p["amount"] / tot, 2) if amt > 0 else None,
                                              invoice_date=d, description=r.get("description"))
            out["ok"] += 1
        except Exception as e:
            out["errors"].append(f"row {n}: {e}")
    if apply: conn.commit(); audit(conn, "bulk_invoices", f"ok={out['ok']} err={len(out['errors'])}")
    out["preview"] = out["preview"][:15]; return out

# ------------------------------------------------------------------ bulk: collections
def bulk_collections(conn, rows, apply=False, reduce_wallet=False, default_mode="BANK"):
    out = {"ok": 0, "errors": [], "total": 0.0, "preview": []}
    for n, r in enumerate(rows, start=2):
        try:
            name = r.get("customer")
            cid, rid = _find_customer(conn, name), None
            if not cid:
                rid = _find_reseller(conn, name)
            if not cid and not rid:
                raise ValueError(f"unknown customer/reseller '{name}'")
            d = _date(r.get("date")) or (_ for _ in ()).throw(ValueError("bad date"))
            mode = str(r.get("mode") or default_mode).strip().upper().replace(" ", "_")
            if mode not in ("BANK", "WALLET", "AUTO_DEBIT", "CREDIT_NOTE"):
                raise ValueError(f"bad mode '{mode}'")
            amt, tds = _num(r.get("amount")), _num(r.get("tds"))
            if amt <= 0 and tds <= 0: raise ValueError("amount required")
            inv_ids = None
            if r.get("invoice_no"):
                inv = conn.execute("SELECT id FROM invoices WHERE invoice_no=? AND status IN ('OPEN','PARTIAL') AND "
                                   + ("customer_id IN (SELECT id FROM customers WHERE reseller_id=?) AND payer='RESELLER'" if rid else "customer_id=? AND payer='CUSTOMER'"),
                                   (str(r["invoice_no"]).strip(), rid or cid)).fetchall()
                if not inv: raise ValueError(f"open invoice {r['invoice_no']} not found for payer")
                inv_ids = [i["id"] for i in inv]
            out["preview"].append(f"{mode} {name} {amt}"); out["total"] += amt
            if apply:
                if mode in ("WALLET", "AUTO_DEBIT") and reduce_wallet and cid:
                    conn.execute("UPDATE customers SET wallet_balance=MAX(0,wallet_balance-?) WHERE id=?", (amt, cid))
                ledger.record_payment(conn, cid, amt, mode, d, str(r.get("reference") or ""), str(r.get("narration") or ""),
                                      invoice_ids=inv_ids, reseller_id=rid, tds=tds, source="BULK", commit=False)
            out["ok"] += 1
        except Exception as e:
            out["errors"].append(f"row {n}: {e}")
    if apply: conn.commit(); audit(conn, "bulk_collections", f"ok={out['ok']} err={len(out['errors'])} total={out['total']}")
    out["preview"] = out["preview"][:15]; return out

# ------------------------------------------------------------------ bulk: customers
def bulk_customers(conn, rows, apply=False):
    out = {"ok": 0, "errors": [], "preview": []}
    cache = {}
    for n, r in enumerate(rows, start=2):
        try:
            name = r.get("name") or r.get("customer")
            if not name: raise ValueError("name required")
            f = {}
            for k in ("kam", "contact_person", "email_to", "email_cc", "whatsapp", "status", "collection_mode", "notes"):
                if r.get(k) not in (None, ""): f[k] = _clean(r[k]) if k not in ("email_to", "email_cc") else str(r[k]).strip()
            for k in ("credit_days", "reseller_share_pct", "wallet_balance"):
                if r.get(k) not in (None, ""): f[k] = _num(r[k])
            if r.get("remind") not in (None, ""): f["remind"] = 1 if str(r["remind"]).strip().lower() in ("yes", "y", "1", "true") else 0
            if r.get("auto_debit_allowed") not in (None, ""): f["auto_debit_allowed"] = 1 if str(r["auto_debit_allowed"]).strip().lower() in ("yes", "y", "1", "true") else 0
            if r.get("reseller"):
                rid = _find_reseller(conn, r["reseller"])
                if not rid: raise ValueError(f"unknown reseller '{r['reseller']}'")
                f["reseller_id"] = rid
            if "reseller_share_pct" in f and not 0 <= f["reseller_share_pct"] <= 100: raise ValueError("share % must be 0-100")
            out["preview"].append(f"{name}: {', '.join(f)}")
            if apply:
                cid = _customer(conn, cache, name)
                if f:
                    conn.execute("UPDATE customers SET " + ",".join(f"{k}=?" for k in f) + " WHERE id=?", (*f.values(), cid))
                    if "credit_days" in f: ledger.recompute_due_dates(conn, cid)
            out["ok"] += 1
        except Exception as e:
            out["errors"].append(f"row {n}: {e}")
    if apply: conn.commit(); audit(conn, "bulk_customers", f"ok={out['ok']} err={len(out['errors'])}")
    out["preview"] = out["preview"][:15]; return out

# ------------------------------------------------------------------ bank statement
def read_bank_statement(path):
    """Finds the header row (date / narration / credit / ref) in a CSV or XLSX statement."""
    if path.lower().endswith(".csv"):
        data = list(csv.reader(open(path, newline="", encoding="utf-8-sig")))
    else:
        data = [list(r) for r in openpyxl.load_workbook(path, data_only=True, read_only=True).worksheets[0].iter_rows(values_only=True)]
    hi = next(i for i, r in enumerate(data) if sum(1 for c in r if c and re.search(r"date|narration|description|particulars|credit|deposit|ref|chq", str(c), re.I)) >= 3)
    h = [str(c or "").lower() for c in data[hi]]
    def col(*pats):
        return next((i for i, x in enumerate(h) if any(re.search(p, x) for p in pats)), None)
    d, n, cr, rf = col(r"date"), col(r"narration|description|particulars|remarks"), col(r"credit|deposit|cr amount|cr$"), col(r"ref|chq|utr")
    out = []
    for r in data[hi + 1:]:
        if not r or len(r) <= max(d, n, cr) or r[d] in (None, ""):
            continue
        dv = _date(r[d])
        if not dv:
            continue
        out.append({"date": dv, "amount": _num(r[cr]), "narration": str(r[n] or ""), "reference": str(r[rf] or "") if rf is not None else ""})
    return out

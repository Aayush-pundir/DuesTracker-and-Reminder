"""Zoho Books connector (REST v3, OAuth2 refresh-token). Credentials via environment only:
ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID, optional ZOHO_DC (in|com|eu, default in).

Role of Zoho in this system: it is the *source of NEW invoices only*. The app is the source of truth for receivables, because
payments were historically not marked as received in Zoho. So:
  * you pick a period; we fetch the invoices Zoho raised in it (preview first), and import the ones not already here;
  * Zoho's paid/unpaid flag is NOT trusted for open invoices - they land as open and are closed here via bank / wallet / bulk uploads;
  * invoices Zoho shows as fully paid can optionally be imported already closed (payment mode ZOHO);
  * an invoice number that already exists in the app (e.g. migrated from Excel) is linked, not duplicated;
  * due date = invoice date + the customer's credit days (Net 15 by default), not Zoho's due date.
Field names were checked against a live Zoho Books organisation (invoice_id, customer_id, customer_name, invoice_number, date,
due_date, total, balance, status, last_payment_date). The OAuth flow itself needs your credentials and is untested."""
import os, time
import httpx
from . import ledger
from .db import audit, get_settings

class ZohoNotConfigured(RuntimeError):
    pass

class Zoho:
    def __init__(self):
        self.dc = os.environ.get("ZOHO_DC", "in")
        need = ["ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_ORG_ID"]
        if any(not os.environ.get(k) for k in need):
            raise ZohoNotConfigured("set " + ", ".join(need))
        self.org = os.environ["ZOHO_ORG_ID"]; self._tok = None; self._exp = 0

    def _token(self):
        if time.time() < self._exp:
            return self._tok
        r = httpx.post(f"https://accounts.zoho.{self.dc}/oauth/v2/token", data={
            "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"], "client_id": os.environ["ZOHO_CLIENT_ID"],
            "client_secret": os.environ["ZOHO_CLIENT_SECRET"], "grant_type": "refresh_token"}, timeout=30)
        r.raise_for_status(); j = r.json(); self._tok = j["access_token"]; self._exp = time.time() + j.get("expires_in", 3600) - 60
        return self._tok

    def invoices(self, start, end):
        """All invoices dated start..end (inclusive), any status."""
        page, out = 1, []
        while True:
            r = httpx.get(f"https://www.zohoapis.{self.dc}/books/v3/invoices", timeout=60,
                          headers={"Authorization": f"Zoho-oauthtoken {self._token()}"},
                          params={"organization_id": self.org, "date_start": start, "date_end": end, "page": page, "per_page": 200})
            r.raise_for_status(); j = r.json(); out += j["invoices"]
            if not j["page_context"].get("has_more_page"):
                return out
            page += 1

def _match_customer(conn, inv):
    r = conn.execute("SELECT id FROM customers WHERE zoho_contact_id=?", (inv["customer_id"],)).fetchone()
    if not r:
        r = conn.execute("SELECT id FROM customers WHERE lower(trim(name))=lower(trim(?))", (inv["customer_name"],)).fetchone()
    return r["id"] if r else None

def classify(conn, inv):
    if inv["status"] in ("draft", "void"):
        return "IGNORED", None
    if conn.execute("SELECT 1 FROM invoices WHERE zoho_invoice_id=?", (inv["invoice_id"],)).fetchone():
        return "ALREADY_IMPORTED", None
    cid = _match_customer(conn, inv)
    if cid and conn.execute("SELECT 1 FROM invoices WHERE customer_id=? AND invoice_no=? AND status!='CANCELLED'", (cid, inv["invoice_number"])).fetchone():
        return "LINK_EXISTING", cid
    return ("NEW" if cid else "NEW_CUSTOMER"), cid

def preview(conn, zoho_invoices):
    rows = []
    for inv in zoho_invoices:
        st, _ = classify(conn, inv)
        rows.append({"invoice_id": inv["invoice_id"], "number": inv["invoice_number"], "customer": inv["customer_name"], "date": inv["date"],
                     "total": inv["total"], "zoho_status": inv["status"], "import_status": st})
    return rows

def import_invoices(conn, zoho_invoices, create_customers=True, include_paid=True, only_ids=None):
    res = {"created": 0, "linked": 0, "closed_as_paid": 0, "new_customers": 0, "skipped": 0, "errors": []}
    dc = int(get_settings(conn)["default_credit_days"])
    for inv in zoho_invoices:
        if only_ids is not None and inv["invoice_id"] not in only_ids:
            continue
        st, cid = classify(conn, inv)
        try:
            if st in ("IGNORED", "ALREADY_IMPORTED") or (inv["status"] == "paid" and not include_paid and st != "LINK_EXISTING"):
                res["skipped"] += 1; continue
            if st == "LINK_EXISTING":
                conn.execute("UPDATE invoices SET zoho_invoice_id=? WHERE customer_id=? AND invoice_no=?", (inv["invoice_id"], cid, inv["invoice_number"]))
                conn.execute("UPDATE customers SET zoho_contact_id=COALESCE(zoho_contact_id,?) WHERE id=?", (inv["customer_id"], cid))
                res["linked"] += 1; continue
            if st == "NEW_CUSTOMER":
                if not create_customers:
                    res["skipped"] += 1; continue
                cid = conn.execute("INSERT INTO customers(name,credit_days,email_to) VALUES(?,?,?)", (inv["customer_name"].strip(), dc, inv.get("email") or "")).lastrowid
                res["new_customers"] += 1
            conn.execute("UPDATE customers SET zoho_contact_id=COALESCE(zoho_contact_id,?) WHERE id=?", (inv["customer_id"], cid))
            ids = ledger.create_invoice(conn, cid, inv["total"], inv["date"], "BILLING", inv["invoice_number"], inv.get("reference_number") or "",
                                        source="ZOHO", zoho_invoice_id=inv["invoice_id"], commit=False)
            res["created"] += 1
            paid = round(inv["total"] - (inv.get("balance") or 0), 2) if inv["status"] in ("paid", "partially_paid") else 0
            if paid > 0 and include_paid:
                for iid in ids:
                    row = conn.execute("SELECT i.*, c.reseller_id FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (iid,)).fetchone()
                    take = min(paid, row["amount"])
                    if take > 0:
                        ledger.record_payment(conn, None if row["payer"] == "RESELLER" else cid, take, "ZOHO",
                                              inv.get("last_payment_date") or inv["date"], "Settled in Zoho", invoice_ids=[iid],
                                              reseller_id=row["reseller_id"] if row["payer"] == "RESELLER" else None, source="ZOHO", commit=False)
                        paid -= take
                res["closed_as_paid"] += 1
        except Exception as e:
            res["errors"].append(f"{inv.get('invoice_number')}: {e}")
    audit(conn, "zoho_import", str({k: v for k, v in res.items() if k != "errors"})); conn.commit()
    return res

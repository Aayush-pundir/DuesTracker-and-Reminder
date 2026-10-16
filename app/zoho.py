"""Zoho Books connector (REST v3, OAuth2 refresh-token flow). Credentials via environment only:
ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID, optional ZOHO_DC (com|in|eu, default in).
NOTE: written against Zoho's documented API but NOT yet exercised against a live org - validate in a sandbox first.

Direction of sync (Zoho stays the book of record for invoices & receipts):
  pull_open_invoices()  Zoho -> app   open invoices become dues (replaces the manual Consol upload)
  push_payment()        app  -> Zoho  every bank/wallet/auto-debit settlement is posted as a customer payment
                                      applied to the Zoho invoice, so the invoice closes in Zoho as well
"""
import os, time
import httpx
from .db import get_settings

class ZohoNotConfigured(RuntimeError):
    pass

class Zoho:
    def __init__(self):
        self.dc = os.environ.get("ZOHO_DC", "in")
        need = ["ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_ORG_ID"]
        if any(not os.environ.get(k) for k in need):
            raise ZohoNotConfigured("Set " + ", ".join(need))
        self.org = os.environ["ZOHO_ORG_ID"]; self._tok = None; self._exp = 0

    def _token(self):
        if time.time() < self._exp:
            return self._tok
        r = httpx.post(f"https://accounts.zoho.{self.dc}/oauth/v2/token", data={
            "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"], "client_id": os.environ["ZOHO_CLIENT_ID"],
            "client_secret": os.environ["ZOHO_CLIENT_SECRET"], "grant_type": "refresh_token"}, timeout=30)
        r.raise_for_status(); j = r.json(); self._tok = j["access_token"]; self._exp = time.time() + j.get("expires_in", 3600) - 60
        return self._tok

    def _req(self, method, path, **kw):
        r = httpx.request(method, f"https://www.zohoapis.{self.dc}/books/v3/{path}", params={"organization_id": self.org, **kw.pop("params", {})},
                          headers={"Authorization": f"Zoho-oauthtoken {self._token()}"}, timeout=60, **kw)
        r.raise_for_status(); return r.json()

    def open_invoices(self):
        page, out = 1, []
        while True:
            j = self._req("GET", "invoices", params={"status": "unpaid", "page": page, "per_page": 200})
            out += j["invoices"]
            if not j["page_context"]["has_more_page"]:
                return out
            page += 1

    def create_payment(self, contact_id, invoice_id, amount, date, mode="banktransfer", reference=""):
        return self._req("POST", "customerpayments", json={"customer_id": contact_id, "payment_mode": mode, "amount": amount, "date": date,
                         "reference_number": reference, "invoices": [{"invoice_id": invoice_id, "amount_applied": amount}]})

def pull_open_invoices(conn, zoho=None):
    """Upsert Zoho unpaid invoices as app invoices (match customer by zoho_contact_id, else by name)."""
    z = zoho or Zoho(); s = get_settings(conn); n = 0
    for inv in z.open_invoices():
        c = conn.execute("SELECT id FROM customers WHERE zoho_contact_id=? OR lower(name)=lower(?)", (inv["customer_id"], inv["customer_name"])).fetchone()
        cid = c["id"] if c else conn.execute("INSERT INTO customers(name,zoho_contact_id) VALUES(?,?)", (inv["customer_name"], inv["customer_id"])).lastrowid
        conn.execute("UPDATE customers SET zoho_contact_id=? WHERE id=?", (inv["customer_id"], cid))
        period = inv["date"][:7] + "-01"
        row = conn.execute("SELECT id FROM invoices WHERE zoho_invoice_id=?", (inv["invoice_id"],)).fetchone()
        if row:
            paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM allocations WHERE invoice_id=?", (row["id"],)).fetchone()[0]
            conn.execute("UPDATE invoices SET amount=?, due_date=? WHERE id=?", (inv["balance"] + paid, inv["due_date"], row["id"]))
        else:
            conn.execute("INSERT OR IGNORE INTO invoices(customer_id,kind,period,invoice_no,amount,due_date,zoho_invoice_id,description) VALUES(?,?,?,?,?,?,?,?)",
                         (cid, "OTHER", period, inv["invoice_number"], inv["balance"], inv["due_date"], inv["invoice_id"], "Zoho"))
        n += 1
    conn.commit(); return n

def push_payment(conn, payment_id, zoho=None):
    z = zoho or Zoho()
    p = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    c = conn.execute("SELECT zoho_contact_id FROM customers WHERE id=?", (p["customer_id"],)).fetchone()
    for a in conn.execute("SELECT a.amount, i.zoho_invoice_id FROM allocations a JOIN invoices i ON i.id=a.invoice_id WHERE a.payment_id=?", (payment_id,)):
        if a["zoho_invoice_id"] and c["zoho_contact_id"]:
            r = z.create_payment(c["zoho_contact_id"], a["zoho_invoice_id"], a["amount"], p["received_on"], reference=p["reference"])
            conn.execute("UPDATE payments SET zoho_payment_id=? WHERE id=?", (r["payment"]["payment_id"], payment_id))
    conn.commit()

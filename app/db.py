"""SQLite schema + tiny helpers. One file DB (data/dues.db) - swap for Postgres later without touching callers
because all SQL lives in the service modules."""
import os, sqlite3, contextlib

DB_PATH = os.environ.get("DUES_DB", os.path.join(os.path.dirname(__file__), "..", "data", "dues.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS resellers(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  email_domains TEXT DEFAULT '',        -- comma list, used to auto-tag customers
  name_markers TEXT DEFAULT '',         -- e.g. '-IDFC' suffix used in billing sheets
  remind_reseller INTEGER DEFAULT 0,    -- 1 = also send reseller a digest of its customers' dues
  contact_emails TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  enterprise_id TEXT, kam TEXT, email_to TEXT DEFAULT '', email_cc TEXT DEFAULT '',
  reseller_id INTEGER REFERENCES resellers(id),
  collection_mode TEXT DEFAULT 'BANK',  -- BANK | WALLET | AUTO_DEBIT  (preferred way we collect)
  auto_debit_allowed INTEGER DEFAULT 1, -- 0 = "Do not auto debit"
  wallet_balance REAL DEFAULT 0,        -- money the customer holds on our platform
  ops_status TEXT DEFAULT '', status TEXT DEFAULT 'ACTIVE', -- ACTIVE | FROZEN | STOPPED
  remind INTEGER DEFAULT 1, zoho_contact_id TEXT, notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invoices(
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  kind TEXT NOT NULL,                   -- SPEND_MGMT | OTHER
  period TEXT NOT NULL,                 -- YYYY-MM-01
  invoice_no TEXT DEFAULT '', description TEXT DEFAULT '',
  amount REAL NOT NULL, due_date TEXT NOT NULL,
  status TEXT DEFAULT 'OPEN',           -- OPEN | PARTIAL | CLOSED | CANCELLED
  zoho_invoice_id TEXT, UNIQUE(customer_id, kind, period, invoice_no)
);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
  mode TEXT NOT NULL,                   -- BANK | WALLET | AUTO_DEBIT | CREDIT_NOTE | SNAPSHOT
  amount REAL NOT NULL, received_on TEXT NOT NULL, reference TEXT DEFAULT '',
  narration TEXT DEFAULT '', status TEXT DEFAULT 'ALLOCATED', -- UNMATCHED | SUGGESTED | ALLOCATED
  suggested_customer_id INTEGER, bank_hash TEXT UNIQUE, zoho_payment_id TEXT
);
CREATE TABLE IF NOT EXISTS allocations(
  id INTEGER PRIMARY KEY, payment_id INTEGER NOT NULL REFERENCES payments(id),
  invoice_id INTEGER NOT NULL REFERENCES invoices(id), amount REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reminder_log(
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, stage INTEGER NOT NULL,
  sent_at TEXT NOT NULL, channel TEXT DEFAULT 'EMAIL', to_addr TEXT, cc_addr TEXT,
  outstanding REAL, status TEXT, error TEXT DEFAULT '', subject TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT, actor TEXT, action TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_inv_cust ON invoices(customer_id, status);
CREATE INDEX IF NOT EXISTS ix_alloc_inv ON allocations(invoice_id);
"""

DEFAULT_SETTINGS = {
    "sender_name": "Accounts Team", "sender_email": "accounts@eroute.in",
    "default_cc": "", "credit_days": "10",           # invoice due = end of billed month + credit_days
    "stages": "0:1,7:2,15:3,30:4",                   # days-past-due : stage number
    "repeat_every_days": "15",                       # after the last stage, repeat this often
    "min_gap_days": "5",                             # never mail the same customer more often than this
    "min_amount": "100",                             # ignore dust balances (e.g. Rs 32)
    "escalate_stage": "3",                           # from this stage the KAM is auto-CC'd
    "max_per_run": "40",                              # go-live safety: never mail more than this many customers per run (biggest dues first)
    "send_mode": "DRY_RUN",                          # DRY_RUN | SMTP
    "auto_debit_enabled": "1",
}

def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA journal_mode=WAL")
    return c

def init(conn):
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
    for name, doms, marks in (("IDFC FIRST Bank", "idfcfirstbank.com", "-IDFC,IDFC"),
                              ("Stride One", "strideone.in", "")):
        conn.execute("INSERT OR IGNORE INTO resellers(name,email_domains,name_markers) VALUES(?,?,?)", (name, doms, marks))
    conn.commit()

def get_settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}

def audit(conn, action, detail="", actor="system"):
    import datetime
    conn.execute("INSERT INTO audit(at,actor,action,detail) VALUES(?,?,?,?)",
                 (datetime.datetime.now().isoformat(timespec="seconds"), actor, action, detail))

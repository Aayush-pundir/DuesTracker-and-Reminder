"""SQLite schema + tiny helpers. One file DB (data/dues.db). All SQL lives in the service modules so Postgres is a drop-in later."""
import os, sqlite3, datetime

DB_PATH = os.environ.get("DUES_DB", os.path.join(os.path.dirname(__file__), "..", "data", "dues.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS resellers(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  email_domains TEXT DEFAULT '',        -- comma list, used to auto-tag customers
  name_markers TEXT DEFAULT '',         -- e.g. '-IDFC' suffix used in billing sheets
  contact_emails TEXT DEFAULT '', whatsapp TEXT DEFAULT '',
  remind_reseller INTEGER DEFAULT 1,    -- 1 = remind the reseller about ITS share of dues
  notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  enterprise_id TEXT, kam TEXT DEFAULT '', contact_person TEXT DEFAULT '',
  email_to TEXT DEFAULT '', email_cc TEXT DEFAULT '', whatsapp TEXT DEFAULT '',
  reseller_id INTEGER REFERENCES resellers(id),
  reseller_share_pct REAL DEFAULT 0,    -- % of every new invoice that the RESELLER pays (rest: customer)
  credit_days INTEGER DEFAULT 15,       -- payment terms: due = invoice date + credit_days
  collection_mode TEXT DEFAULT 'BANK',  -- BANK | WALLET | AUTO_DEBIT (preferred way we collect)
  auto_debit_allowed INTEGER DEFAULT 1, -- 0 = "Do not auto debit"
  wallet_balance REAL DEFAULT 0,        -- money the customer holds on our platform (mirror of ops portal)
  ops_status TEXT DEFAULT '', status TEXT DEFAULT 'ACTIVE', -- ACTIVE | FROZEN | STOPPED
  remind INTEGER DEFAULT 1, channel_email INTEGER DEFAULT 1, channel_whatsapp INTEGER DEFAULT 1,
  promise_date TEXT, promise_amount REAL, promise_note TEXT DEFAULT '',
  zoho_contact_id TEXT, notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invoices(
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  payer TEXT NOT NULL DEFAULT 'CUSTOMER',  -- CUSTOMER | RESELLER (who owes this part)
  kind TEXT NOT NULL,                   -- SPEND_MGMT | OTHER | BILLING (from Zoho)
  period TEXT NOT NULL,                 -- YYYY-MM-01 (billing month)
  invoice_no TEXT DEFAULT '', description TEXT DEFAULT '',
  invoice_date TEXT NOT NULL, due_date TEXT NOT NULL, amount REAL NOT NULL,
  status TEXT DEFAULT 'OPEN',           -- OPEN | PARTIAL | CLOSED | CANCELLED
  disputed INTEGER DEFAULT 0, dispute_note TEXT DEFAULT '',
  hold INTEGER DEFAULT 0, hold_reason TEXT DEFAULT '',   -- hold=1: not chased until released (e.g. Zoho import awaiting reconciliation)
  split_group INTEGER, source TEXT DEFAULT 'MANUAL',  -- MIGRATION | MANUAL | BULK | ZOHO
  zoho_invoice_id TEXT, created_at TEXT,
  UNIQUE(customer_id, payer, kind, period, invoice_no)
);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), reseller_id INTEGER REFERENCES resellers(id),
  mode TEXT NOT NULL,                   -- BANK | WALLET | AUTO_DEBIT | CREDIT_NOTE | ZOHO | SNAPSHOT
  amount REAL NOT NULL, tds_amount REAL DEFAULT 0,   -- tds = tax withheld by payer, settles the invoice too
  received_on TEXT NOT NULL, reference TEXT DEFAULT '', narration TEXT DEFAULT '',
  status TEXT DEFAULT 'ALLOCATED',      -- UNMATCHED | SUGGESTED | ALLOCATED
  suggested_party TEXT, bank_hash TEXT UNIQUE, zoho_payment_id TEXT, source TEXT DEFAULT 'MANUAL'
);
CREATE TABLE IF NOT EXISTS allocations(
  id INTEGER PRIMARY KEY, payment_id INTEGER NOT NULL REFERENCES payments(id),
  invoice_id INTEGER NOT NULL REFERENCES invoices(id), amount REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reminder_log(
  id INTEGER PRIMARY KEY, customer_id INTEGER, reseller_id INTEGER, stage INTEGER NOT NULL,
  channel TEXT DEFAULT 'EMAIL', sent_at TEXT NOT NULL, to_addr TEXT, cc_addr TEXT,
  outstanding REAL, status TEXT, error TEXT DEFAULT '', subject TEXT DEFAULT '', body TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, customer_id INTEGER, reseller_id INTEGER, at TEXT, author TEXT DEFAULT '',
  kind TEXT DEFAULT 'NOTE',             -- NOTE | CALL | PROMISE | DISPUTE
  text TEXT
);
CREATE TABLE IF NOT EXISTS templates(
  key TEXT PRIMARY KEY, channel TEXT, stage INTEGER, subject TEXT DEFAULT '', body TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT, actor TEXT, action TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_inv_cust ON invoices(customer_id, status);
CREATE INDEX IF NOT EXISTS ix_inv_zoho ON invoices(zoho_invoice_id);
CREATE INDEX IF NOT EXISTS ix_alloc_inv ON allocations(invoice_id);
CREATE INDEX IF NOT EXISTS ix_alloc_pay ON allocations(payment_id);
"""

DEFAULT_SETTINGS = {
    "sender_name": "Accounts Team, Eroute Technologies Pvt. Ltd.", "sender_email": "accounts@eroute.in",
    "default_cc": "", "default_credit_days": "15",
    "stages": "0:1,7:2,15:3,30:4",                   # days-past-due : stage number
    "repeat_every_days": "15", "min_gap_days": "5", "min_amount": "100",
    "escalate_stage": "3", "kam_emails": "{}",       # JSON {"kam name lowercase": "email"}
    "send_delay_sec": "2",                           # pause between e-mails (Graph/Exchange throttle ~30 msgs/min per mailbox)
    "max_per_run": "40",                             # go-live safety: max parties contacted per run (largest dues first)
    "send_mode": "DRY_RUN",                          # DRY_RUN | LIVE
    "channel_email": "1", "channel_whatsapp": "0",   # global switches
    "auto_apply_wallet": "0",                        # 1 = auto-apply held wallet balance to dues daily (only if the app mirrors the platform wallet)
    "bank_details": "Account name: Eroute Technologies Pvt. Ltd.\nBank: <bank>  A/C: <account no>  IFSC: <ifsc>",
    "payment_link": "",                              # optional Razorpay / UPI link shown in messages
    "migration_done": "0", "is_demo": "0",
}

def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA journal_mode=WAL")
    return c

def _migrate(conn):
    """Add columns introduced after a DB file was first created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
    for name, ddl in (("hold", "INTEGER DEFAULT 0"), ("hold_reason", "TEXT DEFAULT ''")):
        if name not in cols:
            conn.execute(f"ALTER TABLE invoices ADD COLUMN {name} {ddl}")

def init(conn):
    conn.executescript(SCHEMA)
    _migrate(conn)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
    for name, doms, marks in (("IDFC FIRST Bank", "idfcfirstbank.com", "-IDFC"), ("Stride One", "strideone.in", "")):
        conn.execute("INSERT OR IGNORE INTO resellers(name,email_domains,name_markers) VALUES(?,?,?)", (name, doms, marks))
    from .templates_default import seed_templates
    seed_templates(conn)
    conn.commit()

def get_settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}

def set_setting(conn, k, v):
    conn.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))

def now():
    return datetime.datetime.now().isoformat(timespec="seconds")

def audit(conn, action, detail="", actor="system"):
    conn.execute("INSERT INTO audit(at,actor,action,detail) VALUES(?,?,?,?)", (now(), actor, action, detail))

def wipe_business_data(conn):
    for t in ("allocations", "payments", "invoices", "reminder_log", "notes", "customers"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("UPDATE resellers SET contact_emails='', whatsapp=''")
    conn.commit()

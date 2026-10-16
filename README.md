# Dues Control Room

Single source of truth for customer receivables: invoices, collections (bank / wallet / auto-debit / credit note / TDS), reseller-split dues and automatic e-mail + WhatsApp reminders. Replaces the Consol workbook and the DuesReminder macro.

* **Today screen** - only the exceptions a human must act on (broken promises, bank credits to review, disputes, missing split %); everything else is automatic.
* **Credit terms** per customer (Net 15 default) drive due dates, ageing and reminder timing; visible on every screen.
* **Resellers** (IDFC, Stride): an invoice is split into customer share and reseller share, each chased and settled separately.
* **Zoho Books**: choose a period, preview, import *new* invoices; never trusted for paid/unpaid.
* **Bulk Excel** for invoices, collections and customer data (validate, then apply). One-time migration of the old Excel files, then locked.
* **Reports**: live dues matrix (old Summary SM), ageing, trend, KAM scorecard - with Excel export. Daily automatic backups.
* **Reminders** from your Microsoft 365 mailbox and WhatsApp, with editable templates. Starts in dry-run.

## Run it
```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000        # dashboard at http://localhost:8000
```
First screen offers **demo data** (fake customers - safe to click around; sending is disabled) or **Admin → one-time migration** of your Excel files.
Reminders start in `DRY_RUN`. Going live: set `send_mode=LIVE` after configuring channels (`docs/CHANNELS.md`: `MS_*` for Microsoft 365 mail, `WA_*` for WhatsApp).
Zoho: `ZOHO_CLIENT_ID ZOHO_CLIENT_SECRET ZOHO_REFRESH_TOKEN ZOHO_ORG_ID [ZOHO_DC=in]`. The built-in scheduler runs daily at 09:30; or cron `python -m app.jobs`.

```bash
pytest -q tests
```

## Privacy
`data/`, `*.xlsx`, `*.xlsm`, `*.csv`, `*.db` and `.env` are git-ignored – customer data is never committed. Put the source files in `data/`.

Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Channels setup: [docs/CHANNELS.md](docs/CHANNELS.md)

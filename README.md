# Dues Control Room

Automates the month-on-month dues tracking that lives in `Consol_*.xlsx` and the reminder mailing done by `DuesReminder.xlsm`.

* **One source of truth** – customers, invoices (dues), payments, wallets, resellers, reminder history.
* **Three ways money arrives, one way invoices close** – bank statement, wallet deduction, auto-debit (+ credit notes). Every settlement is an allocation against the oldest open invoice; invoice status is *derived*, never typed.
* **Reminders with zero effort** – staged (R1 → R4 → repeat), skips anyone who will be auto-debited, honours "do not remind", escalates to the KAM, caps sends per run.
* **Zoho Books** – connector for pulling open invoices and pushing payments back (needs credentials; not yet tested live).
* **Bulk or one-by-one** – upload a new Consol file any time (idempotent snapshot sync), upload a bank statement, or record a single payment / wallet top-up in the UI.

## Run it
```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000        # dashboard at http://localhost:8000
```
First time: **Data & Settings → upload** the Consol file, then the DuesReminder file (for e-mail addresses).
Reminders start in `DRY_RUN` (logged, nothing sent). To go live set `send_mode=SMTP` and export
`SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD`. The built-in scheduler runs daily at 09:30; or use cron: `python -m app.jobs`.
Optional Zoho: `ZOHO_CLIENT_ID ZOHO_CLIENT_SECRET ZOHO_REFRESH_TOKEN ZOHO_ORG_ID [ZOHO_DC=in]`.

```bash
pytest -q tests
```

## Privacy
`data/`, `*.xlsx`, `*.xlsm`, `*.csv`, `*.db` and `.env` are git-ignored – customer data is never committed. Put the source files in `data/`.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, flows and open questions.

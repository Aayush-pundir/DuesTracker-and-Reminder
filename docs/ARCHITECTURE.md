# Architecture

**Principle: the app is the single source of truth for receivables.** Excel is only for (a) a one-time migration and (b) bulk uploads of collections / invoices / customer data. Zoho only supplies *new* invoices.

## Data model (`app/db.py`)
`customers` ─< `invoices` ─< `allocations` >─ `payments`; `resellers` ─< `customers`; `notes`, `templates`, `reminder_log`, `settings`, `audit`.
* **Credit terms** are per customer (`credit_days`, default 15 = *Net 15*). `due_date = invoice_date + credit_days`; changing a customer's terms recomputes their open invoices. Shown as a badge everywhere (amber when ≠ Net 15).
* Invoice balance = `amount − Σ allocations`; status (`OPEN/PARTIAL/CLOSED`) is derived. `CANCELLED` is a soft delete (blocked while payments are allocated - reverse them first). Every change goes to `audit`.
* **Reseller model** - a customer may have a reseller and a `reseller_share_pct`. Every new invoice is split into two rows sharing `split_group`: `payer=CUSTOMER` and `payer=RESELLER`. Each part has its own ageing, reminders and settlement. A reseller's reminder is *one consolidated statement* listing its customers.
  The Consol workbook contains no explicit split percentages (only Stride Billing amounts, a "BBPS Integration-20%" note and "IDFC First Bank" ops tags), so the % is entered per customer (Today screen lists reseller customers still missing it).

## Three ways money arrives, one way invoices close (`app/ledger.py`)
Every settlement is a `payment` + `allocations` (oldest due first, or specific invoice numbers): **BANK** (statement upload with fuzzy matching → auto-apply when confident, review queue otherwise; reseller payments matched too), **WALLET** deduction and **AUTO_DEBIT** (recorded from the ops platform via bulk upload / one-by-one; the wallet balance in the app is a mirror and is only auto-applied if `auto_apply_wallet=1`), plus **CREDIT_NOTE**, **TDS** (settles the invoice, reported separately) and **ZOHO** (already paid in Zoho). Excess money stays as an *advance* and is applied automatically to the next invoice raised. Any payment can be undone.

## Getting data in
| Need | How |
|---|---|
| Initial history | Admin → one-time migration of Consol + DuesReminder files, then **Finish & lock** (endpoints return 409 afterwards) |
| New invoices | Zoho: pick a period → preview → import (dedup by Zoho id and by invoice no.; Zoho's "unpaid" is never trusted; paid-in-Zoho can land closed). Or manual / bulk Excel (ADD, UPDATE, CANCEL) |
| Platform deductions | the ops platform is not connected, so upload its deduction file every 15 days / month (Collections → *Platform deductions file*); Today nudges when the last one is >15 days old |
| Collections | single form, bank statement, bulk Excel (mode, TDS, invoice no.) - all validate-then-apply |
| Master data | customer drawer or bulk Excel (credit days, reseller & %, WhatsApp, e-mails, remind on/off, notes); **KAM assignment** by enterprise id and/or name (Customers → Bulk update KAM), plus a **handover** that moves all of one KAM's customers to another (preview first, optional "only customers with dues", a note is added to each customer) |

## Zoho: what real data showed (read-only check of your live books)
* Customer names match the Consol sheet where they overlap (a looser `Pvt`/`Private` match is also applied); Zoho contains many customers that are not in the dues sheet.
* Zoho's due date always equals the invoice date, so it is ignored - terms come from the customer (Net 15).
* Most Zoho invoices read "overdue" because receipts were never marked in Zoho. Therefore **imported open invoices land `on hold`** (not chased) until you upload the bank / deduction files and press *Release*. Invoices Zoho shows as paid land already closed.
* No API access yet? Export Invoices from Zoho Books (CSV/XLSX) and upload it in Invoices → Zoho card - same preview/import rules.
* Reseller split for old dues: already reflected in the amounts in your sheet; the % in the app applies only to invoices created after migration.

## Reports, safety and hygiene
* **Reports tab** (`app/reports.py`): the *dues matrix* (customer × billing month - the live replacement of the Summary SM sheet, filter by KAM, Excel export), *ageing by customer* (Excel export), *billed vs collected* trend (12 months), *KAM scorecard*.
* **Backups** (`app/backup.py`): SQLite online backup every day before the reminder run (14 kept in `data/backups`), plus *Backup now & download* and *Everything as Excel* in Admin. Restore = copy a backup over `data/dues.db`.
* **Duplicate customers**: names are compared ignoring `Pvt/Private/Ltd/Limited`; Admin lists look-alikes and can merge them (invoices, receipts, notes, reminder history move; blocked if the same invoice exists on both).
* **Send reminder now**: one click from a customer / reseller - ignores the gap rules, still honours holds, disputes and DRY_RUN.

## Reminders (`app/reminders.py`, `app/channels.py`)
Parties = customers + resellers. Stages by days past oldest due date (`0:1,7:2,15:3,30:4`), repeat every 15 d, ≥5 d between contacts, dust ignored, **disputed invoices excluded**, **promise-to-pay pauses chasing** (broken promises surface for a call), KAM escalated in CC from stage 3, `max_per_run` go-live cap, DRY_RUN never advances stages and demo data can never be sent. Channels: e-mail (Microsoft Graph / SMTP) and WhatsApp (Cloud API templates) - see `docs/CHANNELS.md`. Messages are editable templates.

## What mature receivable tools do - and where this stands
| Practice (Chaser, Upflow, HighRadius, Zoho dunning, Tally/Vyapar India) | Status |
|---|---|
| Staged dunning with tone escalation, channel mix | ✅ |
| Promise-to-pay, dispute flags, collector notes/calls | ✅ |
| Worklist of exceptions instead of a report | ✅ Today screen |
| Ageing, DSO, collections by route | ✅ (CEI / forecast later) |
| Customer statement of account | ✅ Excel (PDF later) |
| TDS handling (India) | ✅ per receipt; Form 26AS reconciliation later |
| Cash application / bank matching | ✅ fuzzy; UTR-in-narration & bank-feed later |
| Pay-now link / UPI QR in message | ◻ `payment_link` setting is wired into e-mail; Razorpay per-invoice links later |
| Customer self-service portal, e-mail-reply parsing ("paid, UTR…") | ◻ roadmap |
| Credit limits / auto service-hold, risk score | ◻ roadmap |
| GST e-invoice / IRN, interest on late payment | ◻ stays in Zoho / billing |

## Known limits
No login yet (put behind VPN / an auth proxy before others use it); single-process SQLite; Zoho / Microsoft Graph / WhatsApp code paths are untested against live accounts (Zoho field names were verified against a real invoice); bank-statement column detection is heuristic; later this merges into your billing utility - the `invoices` table and `/api/invoices` are the intended integration seam.

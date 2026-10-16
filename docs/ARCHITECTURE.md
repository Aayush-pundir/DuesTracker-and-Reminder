# Architecture

## What the two Excel files told us
| Sheet | Meaning | Becomes |
|---|---|---|
| Consol › Summary SM | customer × month matrix of **pending** spend-management dues (+ KAM, EnterpriseId) | `invoices(kind=SPEND_MGMT)` |
| Consol › Other than S Mgt / Summary-OSM | invoice-level dues (invoice no., period, description, KAM) – min-guarantee, setup fees, gift cards | `invoices(kind=OTHER)` |
| Consol › Spend Management | per-customer ops status: *Running balance low* / *Do not auto debit* / *IDFC First Bank*, enterprise e-mails | `customers.collection_mode`, `auto_debit_allowed`, contacts |
| Consol › Stride Billing | billing for reseller-linked customers | reseller tag |
| DuesReminder › DATA | To / CC e-mails, last "Sent" status | contacts |
| DuesReminder › VBA | Outlook mailer behind a button | replaced by the reminder engine |

Sanity check on the supplied files: imported outstanding = ₹1,32,16,398 = Summary SM ₹83.97 L + Other ₹48.19 L (matches the sheet totals).

## Data model (`app/db.py`)
`customers` ─< `invoices` ─< `allocations` >─ `payments`; `resellers` ─< `customers`; `reminder_log`; `settings`; `audit`.
* `payments.mode`: `BANK | WALLET | AUTO_DEBIT | CREDIT_NOTE | SNAPSHOT` (SNAPSHOT = closed because a newer Consol no longer lists it).
* Invoice balance = `amount − Σ allocations`. Status (`OPEN/PARTIAL/CLOSED`) is recomputed after every allocation.

## The three collection mechanisms (`app/ledger.py`)
1. **Bank statement** – upload CSV/XLSX. Each credit is hashed (re-upload safe) and matched to a customer by name tokens, boosted when the amount equals the customer's total or one invoice. Score ≥ 1.2 → applied automatically; otherwise it waits in the *review queue* for one click. Excess stays unapplied.
2. **Held money on our platform (wallet)** – `wallet_balance` per customer; *Top-up* / *Deduct vs dues* in the UI. Never overdraws; undo returns the money.
3. **Auto-debit** – daily job debits wallets of customers with `auto_debit_allowed=1` ("Do not auto debit" is respected), oldest dues first. In the reminder plan these customers get `AUTO_DEBIT` instead of an e-mail.

Credit notes use the same allocation path. Any wrong entry can be reversed (invoices re-open).

## Reminders (`app/reminders.py`)
Default policy: stage 1 at due+0 d, 2 at +7, 3 at +15, 4 at +30, then every 15 d; min 5 d between mails; balances < ₹100 ignored; KAM auto-CC'd from stage 3 (setting `kam_emails`, JSON `{"tanisha verma":"…"}`); max 40 customers per run (go-live safety). Each e-mail lists the open invoices with stage-specific tone; wallet customers get a top-up variant. DRY_RUN entries never advance stages.

## Resellers (IDFC First Bank, Stride)
Tagged automatically (name marker like `-IDFC`, or e-mail domain) and editable per customer. Dues stay on the **end customer**, roll up per reseller on the Resellers tab, and the reseller contact can be CC'd (`resellers.remind_reseller/contact_emails`). *Assumption – to be confirmed.*

## Zoho Books (`app/zoho.py`)
Zoho stays the book of record: **pull** unpaid invoices (replaces the manual Consol upload) and **push** every settlement as a customer payment applied to the Zoho invoice, so it closes in both places. Written against the REST v3 API; **untested without credentials** – try in a sandbox first.

## Rollout
1. Import Consol, watch the *Reminders* plan in DRY_RUN for a week.
2. Go live with a low `max_per_run` (~20): the first run would otherwise hit ~280 customers, many 180+ days overdue.
3. Upload the bank statement daily (later: bank feed / Zoho bank rules).
4. Connect Zoho, retire the Consol upload.

## Known limits / next steps
No login yet (keep behind VPN / auth proxy); single-process SQLite; bank-statement column detection is heuristic; no WhatsApp/SMS (`reminder_log.channel` ready); GST/TDS short-payments not modelled; KAM names in the source are inconsistent ("Tanisha" vs "Tanisha Verma") and should be normalised.

# Sending reminders from your official mailbox and WhatsApp

Nothing is sent until **Reminders → mode = LIVE**. Credentials are environment variables only (never stored in the DB or git).
Use **Admin → Test channels** to send yourself a test first. *Both integrations are written to the vendors' documented APIs but have not been run against your accounts.*

## E-mail from accounts@eroute.in (Microsoft 365) - guided setup in **Admin → Microsoft 365 e-mail setup**
The Admin card shows which settings are present (names only, never values), the steps, and the checks. Two admins are involved once (~20 min):

| # | Who | What |
|---|---|---|
| 1 | Microsoft 365 / Entra admin | Open Azure Cloud Shell and run `setup/azure_app_registration.sh` (download link in the card). It creates the app **Dues Control Room**, grants the Microsoft Graph *application* permission **Mail.Send**, grants admin consent, creates a 1-year secret and prints 4 lines. |
| 2 | You | Paste those lines into **`data/channels.env`** (template: `setup/channels.env.example`; the folder is git-ignored). Real environment variables, if set, override the file. Restart the app. |
| 3 | Exchange admin | Run `setup/exchange_restrict_mailbox.ps1 -AppId <client id>`: a mail-enabled security group containing only the mailbox + an *Application Access Policy*, so the app can send **only** as `accounts@eroute.in` (without it Mail.Send would allow sending as anyone in the company). Takes 30-60 min to apply. |
| 4 | You | Admin → **Verify credentials** (proves tenant/client/secret and that consent was granted - sends nothing) → **Send test e-mail** to yourself → Reminders → set **LIVE** with a small `max_per_run`. |

What the app does for you: mails go through Microsoft Graph and land in the mailbox's *Sent Items* (customer replies come back to the same inbox); a pause between mails (`send_delay_sec`, default 2 s) plus automatic retry on Microsoft throttling (429/503); and failures are explained in plain words - wrong/expired secret, missing admin consent, mailbox blocked by the access policy, wrong `MS_SENDER`. Secrets live only in `data/channels.env` / the environment: the app has no login yet, so it deliberately offers **no screen to type secrets into**.
Rotate the client secret before it expires (1 year) - set a calendar reminder. SPF/DKIM/DMARC for eroute.in should already be fine if you send from Outlook today.
*The scripts and the Graph calls follow Microsoft's documentation but have not been run against your tenant - read the scripts before running them.*
Fallback: SMTP AUTH (`SMTP_HOST=smtp.office365.com SMTP_PORT=587 SMTP_USER SMTP_PASSWORD`) only if SMTP AUTH is enabled for the mailbox.

## WhatsApp
Meta only allows **business-initiated messages through pre-approved templates**. Create these 4 templates (category *Utility*, language English) in WhatsApp Manager
(or your BSP such as Gupshup / Interakt / WATI - any provider exposing the Cloud-API payload works) with exactly 4 body variables:

| Name | Body |
|---|---|
| `dues_reminder_1` | Hello {{1}}, a gentle reminder from Eroute Technologies: Rs {{2}} is pending, oldest invoice due on {{4}}. Please reply with the payment reference if already paid. |
| `dues_reminder_2` | Hello {{1}}, Rs {{2}} is now overdue by {{3}} days. Please arrange payment at the earliest or reply if there is any query. |
| `dues_reminder_3` | Hello {{1}}, Rs {{2}} has been overdue for {{3}} days despite earlier reminders. Kindly clear it immediately to avoid service disruption. |
| `dues_reminder_4` | Final reminder: Hello {{1}}, Rs {{2}} is overdue by {{3}} days. Please settle today or call our accounts team to avoid suspension. |

Variables are always sent in this order: **1 customer, 2 total (Indian format), 3 days overdue, 4 oldest due date**.
Put `WA_PHONE_ID`, `WA_TOKEN` in `data/channels.env` (and `WA_LANG` if not `en`), then switch WhatsApp on in Reminders. Customers need a WhatsApp number (Customer drawer or bulk upload);
get opt-in from customers (add it to the onboarding form) - it is a Meta policy requirement. The wording in Reminders → *Message drafts* is the preview; the approved text at Meta is what is actually delivered.

## How the e-mail text is edited
Reminders → *Message drafts*: subject and HTML body per stage, in a sandboxed template language (`{{ customer }}`, `{{ total }}`, `{{ table|safe }}` ...). A template that does not render is rejected on save.

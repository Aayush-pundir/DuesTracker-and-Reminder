# Sending reminders from your official mailbox and WhatsApp

Nothing is sent until **Reminders → mode = LIVE**. Credentials are environment variables only (never stored in the DB or git).
Use **Admin → Test channels** to send yourself a test first. *Both integrations are written to the vendors' documented APIs but have not been run against your accounts.*

## E-mail from accounts@eroute.in (Microsoft 365)
**Recommended: Microsoft Graph (no passwords, no basic auth)**
1. Entra ID (Azure AD) → App registrations → *New registration* ("Dues Control Room").
2. API permissions → Microsoft Graph → **Application** permission `Mail.Send` → *Grant admin consent*.
3. Certificates & secrets → new client secret.
4. **Restrict it to one mailbox** (Exchange Online PowerShell): create a mail-enabled security group containing only `accounts@eroute.in`, then
   `New-ApplicationAccessPolicy -AppId <client id> -PolicyScopeGroupId <group> -AccessRight RestrictAccess`.
5. Set: `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_SENDER=accounts@eroute.in`.

Mails appear in that mailbox's *Sent Items*, so customer replies ("paid, UTR …") land in the same inbox your team already watches.
Fallback: SMTP AUTH (`SMTP_HOST=smtp.office365.com SMTP_PORT=587 SMTP_USER SMTP_PASSWORD`) - only if SMTP AUTH is enabled for the mailbox.
Deliverability: make sure SPF/DKIM/DMARC for eroute.in are set (they are if you already send from Outlook), keep `max_per_run` modest at first.

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
Set `WA_PHONE_ID`, `WA_TOKEN` (and `WA_LANG` if not `en`), then switch WhatsApp on in Reminders. Customers need a WhatsApp number (Customer drawer or bulk upload);
get opt-in from customers (add it to the onboarding form) - it is a Meta policy requirement. The wording in Reminders → *Message drafts* is the preview; the approved text at Meta is what is actually delivered.

## How the e-mail text is edited
Reminders → *Message drafts*: subject and HTML body per stage, in a sandboxed template language (`{{ customer }}`, `{{ total }}`, `{{ table|safe }}` ...). A template that does not render is rejected on save.

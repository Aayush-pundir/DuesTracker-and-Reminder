"""Outbound channels. Credentials come ONLY from environment variables (never stored in the DB or repo).

E-mail from the official Microsoft 365 mailbox - two supported backends:
  1. Microsoft Graph (recommended): MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET, MS_SENDER (e.g. accounts@eroute.in).
     Entra ID app registration with *application* permission Mail.Send, restricted to that one mailbox with an Exchange
     Application Access Policy. Mail lands in the mailbox's Sent Items, so replies/UTRs come back to the same inbox.
  2. SMTP AUTH: SMTP_HOST (smtp.office365.com), SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD. Needs SMTP AUTH enabled for the mailbox
     (Microsoft is phasing out basic auth, so prefer Graph).
WhatsApp: Meta WhatsApp Business Cloud API (or any BSP exposing the same payload): WA_PHONE_ID, WA_TOKEN [, WA_LANG=en].
Business-initiated WhatsApp messages must use pre-approved templates - see docs/CHANNELS.md.
NOTE: these code paths are written to the vendors' documented APIs but have not been exercised against live accounts."""
import os, re, smtplib, ssl, time
from email.message import EmailMessage
import httpx

class ChannelNotConfigured(RuntimeError):
    pass

def email_backend():
    if all(os.environ.get(k) for k in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_SENDER")):
        return "graph"
    if os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"):
        return "smtp"
    return None

def status():
    return {"email": email_backend(), "whatsapp": bool(os.environ.get("WA_PHONE_ID") and os.environ.get("WA_TOKEN"))}

_tok = {"v": None, "exp": 0}

def _graph_token():
    if time.time() < _tok["exp"]:
        return _tok["v"]
    r = httpx.post(f"https://login.microsoftonline.com/{os.environ['MS_TENANT_ID']}/oauth2/v2.0/token", data={
        "client_id": os.environ["MS_CLIENT_ID"], "client_secret": os.environ["MS_CLIENT_SECRET"],
        "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status(); j = r.json(); _tok.update(v=j["access_token"], exp=time.time() + j.get("expires_in", 3600) - 60)
    return _tok["v"]

def send_email(sender_name, sender_email, to, cc, subject, html):
    be = email_backend()
    if be == "graph":
        mk = lambda xs: [{"emailAddress": {"address": a}} for a in xs]
        r = httpx.post(f"https://graph.microsoft.com/v1.0/users/{os.environ['MS_SENDER']}/sendMail",
                       headers={"Authorization": f"Bearer {_graph_token()}"}, timeout=60,
                       json={"message": {"subject": subject, "body": {"contentType": "HTML", "content": html},
                                         "toRecipients": mk(to), "ccRecipients": mk(cc)}, "saveToSentItems": True})
        r.raise_for_status()
    elif be == "smtp":
        m = EmailMessage(); m["Subject"] = subject; m["From"] = f"{sender_name} <{sender_email}>"
        m["To"] = ", ".join(to); m["Cc"] = ", ".join(cc)
        m.set_content("Please view this e-mail in an HTML-capable client."); m.add_alternative(html, subtype="html")
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587)), timeout=30) as s:
            s.starttls(context=ssl.create_default_context()); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]); s.send_message(m)
    else:
        raise ChannelNotConfigured("no e-mail backend configured (set MS_* or SMTP_* environment variables)")

def normalize_phone(p):
    d = re.sub(r"\D", "", str(p or ""))
    if len(d) == 10: d = "91" + d
    if len(d) == 11 and d.startswith("0"): d = "91" + d[1:]
    return d if 11 <= len(d) <= 15 else ""

def send_whatsapp(phone, template_name, params):
    if not (os.environ.get("WA_PHONE_ID") and os.environ.get("WA_TOKEN")):
        raise ChannelNotConfigured("WhatsApp not configured (set WA_PHONE_ID and WA_TOKEN)")
    to = normalize_phone(phone)
    if not to:
        raise ValueError(f"invalid WhatsApp number {phone!r}")
    r = httpx.post(f"https://graph.facebook.com/v20.0/{os.environ['WA_PHONE_ID']}/messages",
                   headers={"Authorization": f"Bearer {os.environ['WA_TOKEN']}"}, timeout=30,
                   json={"messaging_product": "whatsapp", "to": to, "type": "template",
                         "template": {"name": template_name, "language": {"code": os.environ.get("WA_LANG", "en")},
                                      "components": [{"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}]}})
    r.raise_for_status()

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
import base64, json, os, re, smtplib, ssl, time
from email.message import EmailMessage
import httpx
from . import envfile

envfile.load()          # data/channels.env -> environment (real env vars win)

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
GRAPH_VARS = ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_SENDER")
SMTP_VARS = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")
WA_VARS = ("WA_PHONE_ID", "WA_TOKEN")

_TOKEN_HINTS = {"AADSTS7000215": "The client secret is wrong. Use the secret VALUE (not the secret ID) from Certificates & secrets.",
                "AADSTS7000222": "The client secret has expired - create a new one in Certificates & secrets.",
                "AADSTS700016": "Client (application) ID not found in this tenant - check MS_CLIENT_ID and MS_TENANT_ID.",
                "AADSTS90002": "Tenant not found - check MS_TENANT_ID (Directory/tenant ID from the app's Overview page).",
                "AADSTS900023": "Tenant ID is not a valid tenant identifier."}

def _explain_token_error(r):
    try:
        j = r.json(); d = j.get("error_description", "") or j.get("error", "")
    except Exception:
        d = r.text[:200]
    for code, hint in _TOKEN_HINTS.items():
        if code in d:
            return f"{hint} ({code})"
    return f"Microsoft sign-in failed: {d[:220]}"

def _graph_token(force=False):
    if not force and time.time() < _tok["exp"]:
        return _tok["v"]
    r = httpx.post(f"https://login.microsoftonline.com/{os.environ['MS_TENANT_ID']}/oauth2/v2.0/token", data={
        "client_id": os.environ["MS_CLIENT_ID"], "client_secret": os.environ["MS_CLIENT_SECRET"],
        "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(_explain_token_error(r))
    j = r.json(); _tok.update(v=j["access_token"], exp=time.time() + j.get("expires_in", 3600) - 60)
    return _tok["v"]

def _jwt_roles(token):
    try:
        b = token.split(".")[1]; b += "=" * (-len(b) % 4)
        return json.loads(base64.urlsafe_b64decode(b)).get("roles", [])
    except Exception:
        return []

def verify_graph():
    """Proves tenant/client/secret are right and that admin consent for Mail.Send was granted. Sends nothing."""
    missing = [k for k in GRAPH_VARS if not os.environ.get(k)]
    if missing:
        raise RuntimeError("missing settings: " + ", ".join(missing))
    roles = _jwt_roles(_graph_token(force=True))
    return {"ok": "Mail.Send" in roles, "roles": roles,
            "message": "Sign-in works and Mail.Send is granted." if "Mail.Send" in roles else
                       "Sign-in works but the app has no Mail.Send permission yet - add the Microsoft Graph APPLICATION permission Mail.Send and click 'Grant admin consent'."}

def _graph_send_error(r):
    try:
        e = r.json().get("error", {}); code, msg = e.get("code", ""), e.get("message", "")
    except Exception:
        code, msg = "", r.text[:200]
    hints = {"ErrorAccessDenied": "Microsoft blocked this app from that mailbox - the Exchange Application Access Policy does not include it (or Mail.Send consent is missing).",
             "Authorization_RequestDenied": "Mail.Send has not been granted admin consent.", "ResourceNotFound": f"Mailbox {os.environ.get('MS_SENDER')} was not found - check MS_SENDER.",
             "MailboxNotEnabledForRESTAPI": "That mailbox cannot be used through Graph (not an Exchange Online mailbox).",
             "ErrorInvalidUser": f"MS_SENDER {os.environ.get('MS_SENDER')} is not a valid mailbox.", "InvalidAuthenticationToken": "Token rejected - check the secret / tenant."}
    return f"{hints.get(code, 'Microsoft Graph refused the message')} [{r.status_code} {code}] {msg[:160]}".strip()

def send_email(sender_name, sender_email, to, cc, subject, html):
    be = email_backend()
    if be == "graph":
        mk = lambda xs: [{"emailAddress": {"address": a}} for a in xs]
        body = {"message": {"subject": subject, "body": {"contentType": "HTML", "content": html}, "toRecipients": mk(to), "ccRecipients": mk(cc)}, "saveToSentItems": True}
        for attempt in range(3):                                   # Graph throttles (429/503): honour Retry-After
            r = httpx.post(f"https://graph.microsoft.com/v1.0/users/{os.environ['MS_SENDER']}/sendMail",
                           headers={"Authorization": f"Bearer {_graph_token()}"}, timeout=60, json=body)
            if r.status_code in (429, 503) and attempt < 2:
                time.sleep(min(int(r.headers.get("Retry-After", 5) or 5), 30)); continue
            if r.status_code == 401 and attempt < 2:
                _graph_token(force=True); continue
            break
        if r.status_code >= 400:
            raise RuntimeError(_graph_send_error(r))
    elif be == "smtp":
        m = EmailMessage(); m["Subject"] = subject; m["From"] = f"{sender_name} <{sender_email}>"
        m["To"] = ", ".join(to); m["Cc"] = ", ".join(cc)
        m.set_content("Please view this e-mail in an HTML-capable client."); m.add_alternative(html, subtype="html")
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587)), timeout=30) as s:
            s.starttls(context=ssl.create_default_context()); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]); s.send_message(m)
    else:
        raise ChannelNotConfigured("no e-mail backend configured (set MS_* in data/channels.env or the environment)")

def setup_status(settings_sender=""):
    """What is configured - names only, never values."""
    have = lambda ks: {k: bool(os.environ.get(k)) for k in ks}
    ms = os.environ.get("MS_SENDER", "")
    return {"backend": email_backend(), "graph": have(GRAPH_VARS), "smtp": have(SMTP_VARS), "whatsapp": have(WA_VARS),
            "ms_sender": ms, "sender_mismatch": bool(ms and settings_sender and ms.lower() != settings_sender.lower()),
            "env_file": os.path.abspath(envfile.PATH), "env_file_exists": os.path.isfile(envfile.PATH)}

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

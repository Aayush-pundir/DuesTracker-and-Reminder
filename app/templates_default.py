"""Default, user-editable message templates (Jinja syntax, rendered in a sandbox).
Variables: customer, contact, total, overdue, days, oldest_due, invoice_count, credit_terms, table (html, use |safe),
sender, bank_html (|safe), payment_link, is_reseller.
WhatsApp: business-initiated messages MUST use a Meta-approved template. The app sends template `subject` (the template name)
with the 4 positional variables {{1}}=customer {{2}}=total {{3}}=days overdue {{4}}=oldest due date. `body` is only the
human-readable draft used for preview and for submitting the template to Meta for approval."""
from .db import now

EMAIL_SUBJECT = {
    1: "Payment reminder: Rs {{ total }} outstanding | {{ customer }}",
    2: "Second reminder: Rs {{ total }} overdue | {{ customer }}",
    3: "Urgent: Rs {{ total }} overdue by {{ days }} days | {{ customer }}",
    4: "Final notice: Rs {{ total }} overdue by {{ days }} days | {{ customer }}",
}
INTRO = {
    1: "This is a gentle reminder that the invoice(s) below, on {{ credit_terms }} terms, are now due.",
    2: "We have not yet received payment for the invoice(s) below, which are past their due date. Please arrange payment at the earliest.",
    3: "Despite our earlier reminders the invoice(s) below remain unpaid. Kindly clear them immediately to avoid disruption of services.",
    4: "This is a final reminder. The dues below are significantly overdue. Please settle them, or call us today, to avoid suspension of services.",
}
EMAIL_BODY = """<p>Dear {{ contact }},</p>
<p>@@INTRO@@</p>
{{ table|safe }}
<p><b>Total outstanding: Rs {{ total }}</b> ({{ invoice_count }} invoice(s); oldest due {{ oldest_due }}; payment terms: {{ credit_terms }})</p>
<p>{% if payment_link %}Pay online: <a href="{{ payment_link }}">{{ payment_link }}</a><br>{% endif %}
Bank transfer details:<br>{{ bank_html|safe }}</p>
<p>If you have already paid, please reply with the payment reference (UTR) so we can reconcile it immediately. If TDS was deducted, please share the TDS certificate.</p>
<p>Regards,<br>{{ sender }}</p>"""

WA_TEXT = {
    1: "Hello {{ customer }}, a gentle reminder from Eroute Technologies: Rs {{ total }} is pending, oldest invoice due on {{ oldest_due }}. Please reply with the payment reference if already paid.",
    2: "Hello {{ customer }}, Rs {{ total }} is now overdue by {{ days }} days. Please arrange payment at the earliest or reply if there is any query.",
    3: "Hello {{ customer }}, Rs {{ total }} has been overdue for {{ days }} days despite earlier reminders. Kindly clear it immediately to avoid service disruption.",
    4: "Final reminder: Hello {{ customer }}, Rs {{ total }} is overdue by {{ days }} days. Please settle today or call our accounts team to avoid suspension.",
}

def seed_templates(conn):
    for st in (1, 2, 3, 4):
        conn.execute("INSERT OR IGNORE INTO templates(key,channel,stage,subject,body,updated_at) VALUES(?,?,?,?,?,?)",
                     (f"EMAIL:{st}", "EMAIL", st, EMAIL_SUBJECT[st], EMAIL_BODY.replace('@@INTRO@@', INTRO[st]), now()))
        conn.execute("INSERT OR IGNORE INTO templates(key,channel,stage,subject,body,updated_at) VALUES(?,?,?,?,?,?)",
                     (f"WHATSAPP:{st}", "WHATSAPP", st, f"dues_reminder_{st}", WA_TEXT[st], now()))

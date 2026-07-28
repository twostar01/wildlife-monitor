"""
notifications.py — stdlib-only SMTP helper shared by wildlife_processor.py and web_app.py

This module holds every piece of notification logic the pipeline needs: SMTP config
validation, the SMTP transport that never raises, the alert-policy decision, the alert
message formatter, and the two password-hygiene helpers behind D-15. Imports are limited
to the standard library so both the batch CLI and the web app can import it cheaply.
"""

import smtplib
import logging
from email.message import EmailMessage
from typing import Optional

log = logging.getLogger("wildlife_processor")

# smtplib.SMTP inherits the global socket default (no timeout) unless one is passed
# explicitly, and a stalled connect would hold up the whole nightly run.
# See 02-RESEARCH.md Pitfall 4.
SMTP_TIMEOUT_SECS = 15

SMTP_SETTING_KEYS = ("smtp_server", "smtp_port", "smtp_username", "smtp_password", "smtp_recipient")


def _header_safe(value) -> str:
    """
    Coerce `value` to a string and strip every carriage return, line feed and null byte.

    Defense-in-depth: EmailMessage's header machinery already folds/encodes headers, but
    stripping control characters first means a camera name or SMTP error string can never
    smuggle an extra header line into an outgoing message (T-02-05).
    """
    s = str(value)
    return s.replace("\r", "").replace("\n", "").replace("\x00", "")


def validate_smtp_config(cfg: dict) -> Optional[str]:
    """
    Return None when `cfg` is a usable SMTP config, else a single human-readable
    sentence naming the offending field. Messages match the UI-SPEC copywriting
    contract exactly, so they can be rendered verbatim as inline validation errors.
    """
    server = (cfg.get("smtp_server") or "").strip()
    if not server:
        return "SMTP server is required."

    port = cfg.get("smtp_port")
    try:
        port_int = int(port)
    except (TypeError, ValueError):
        return "Port must be between 1 and 65535."
    if not (1 <= port_int <= 65535):
        return "Port must be between 1 and 65535."

    recipient = (cfg.get("smtp_recipient") or "").strip()
    if not recipient:
        return "Recipient is required."
    if "@" not in recipient:
        return "Recipient must be a valid email address."

    username = (cfg.get("smtp_username") or "").strip()
    if (
        _header_safe(server) != server
        or _header_safe(username) != username
        or _header_safe(recipient) != recipient
    ):
        return "SMTP settings must not contain line breaks."

    return None


def send_notification_email(smtp_config: dict, subject: str, body: str) -> tuple:
    """
    Send a plaintext email via STARTTLS.

    NOTIFY-05 isolation boundary: this function never raises under any SMTP, DNS, TLS
    or socket failure. Callers may ignore its return value without risk to their own
    control flow. Always returns (success: bool, error_message: Optional[str]).
    """
    error = validate_smtp_config(smtp_config)
    if error:
        return False, error

    server = (smtp_config.get("smtp_server") or "").strip()
    port = int(smtp_config.get("smtp_port"))
    username = (smtp_config.get("smtp_username") or "").strip()
    password = smtp_config.get("smtp_password") or ""
    recipient = (smtp_config.get("smtp_recipient") or "").strip()

    msg = EmailMessage()
    msg["Subject"] = _header_safe(subject)
    msg["From"] = _header_safe(username or recipient)
    msg["To"] = _header_safe(recipient)
    msg.set_content(str(body))

    try:
        with smtplib.SMTP(server, port, timeout=SMTP_TIMEOUT_SECS) as smtp:
            smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
        return True, None
    except Exception as e:
        # Never log or return smtp_config itself — only the exception message, so the
        # password cannot leak into the run log (T-02-06).
        log.error("SMTP send failed: %s", e)
        return False, str(e)

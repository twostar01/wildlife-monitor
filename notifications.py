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


def decide_run_alert(run: dict, settings: dict) -> Optional[str]:
    """
    Pure function over the run dict shape produced by database.py::_run_row_to_dict
    and the merged settings dict.

    Ordering is load-bearing: at most one alert kind is produced per run, so an
    errored run that also happened to find nothing never also generates a
    zero-detection email.
    """
    status = run.get("status")
    if status in ("failure", "partial"):
        # D-14: a degraded (partial) run is alert-worthy, not just a total failure.
        return "error"

    # D-09: .get() with no default means an absent toggle key behaves as off.
    # D-08's quiet-night guard: videos_processed > 0 excludes runs where nothing
    # new synced — that's mundane, not a broken camera.
    if (
        settings.get("alert_on_zero_detections")
        and status == "success"
        and int(run.get("videos_processed") or 0) > 0
        and int(run.get("detections_found") or 0) == 0
    ):
        return "zero_detections"

    return None


def format_run_alert(kind: str, run: dict) -> tuple:
    """
    Build (subject, body) for a run alert. Free of I/O — no file reads, no network,
    no database access — so this stays directly unit-testable.
    """
    trigger = run.get("trigger", "")
    status = run.get("status", "")
    if kind == "error":
        subject = f"Wildlife Monitor — run {str(status).upper()} ({trigger})"
    else:
        subject = f"Wildlife Monitor — no detections ({trigger})"
    # Pass the whole assembled subject through _header_safe so a malformed trigger
    # or status value can never inject a header break.
    subject = _header_safe(subject)

    end_time = run.get("end_time") or "still in progress"
    duration = run.get("duration_secs")
    duration_str = f"{duration}s" if duration is not None else "-"

    lines = [
        f"Run ID: {run.get('id')}",
        f"Trigger: {trigger}",
        f"Status: {status}",
        f"Started: {run.get('start_time')}",
        f"Ended: {end_time}",
        f"Duration: {duration_str}",
        f"Videos processed: {run.get('videos_processed')}",
        f"Detections found: {run.get('detections_found')}",
        "",
        "Cameras:",
    ]
    cameras = run.get("cameras") or {}
    for name in sorted(cameras.keys()):
        info = cameras.get(name) or {}
        lines.append(f"  {name}: {info.get('videos', 0)} videos, {info.get('detections', 0)} detections")

    offline = run.get("offline_cameras") or []
    lines.append(f"Offline cameras: {', '.join(offline) if offline else 'none'}")

    error_summary = run.get("error_summary")
    if error_summary:
        lines.append("")
        lines.append(f"Error: {error_summary}")

    # The body is passed to set_content() by the caller, so newlines are fine here.
    body = "\n".join(lines)
    return subject, body


def redact_smtp_password(settings: dict) -> dict:
    """
    Return a shallow copy of `settings` with `smtp_password` forced to the empty
    string and an added boolean `smtp_password_set` flag reporting whether the
    original value was a non-empty string. Never mutates its argument.

    D-15 read half: GET /api/settings sends this redacted copy so the browser can
    render a blank password input with a "(unchanged)" affordance without ever
    receiving the stored secret. D-10 accepts plaintext storage in settings.json
    given the project's local-LAN, no-authentication posture (CLAUDE.md) — this
    helper exists for transport hygiene, not to re-litigate storage encryption.
    """
    copy = dict(settings)
    password_set = bool(copy.get("smtp_password"))
    copy["smtp_password"] = ""
    copy["smtp_password_set"] = password_set
    return copy


def merge_preserved_password(incoming: dict, stored: dict) -> dict:
    """
    Return a shallow copy of `incoming` with the display-only `smtp_password_set`
    key discarded, and with `smtp_password` carried over from `stored` when the
    incoming value is missing, None, or blank after stripping. A non-blank
    incoming password always wins, so saving with the field left blank leaves the
    stored password exactly as it was — there is deliberately no code path in
    which a blank submission clears a previously saved password.

    D-15 write half. D-10 accepts plaintext storage in settings.json given the
    project's local-LAN, no-authentication posture (CLAUDE.md) — this helper
    exists for round-trip hygiene, not to re-litigate storage encryption.
    """
    copy = dict(incoming)
    copy.pop("smtp_password_set", None)
    incoming_password = copy.get("smtp_password")
    if incoming_password is None or not str(incoming_password).strip():
        copy["smtp_password"] = stored.get("smtp_password", "")
    return copy

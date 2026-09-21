"""Send an email notification via SMTP.

Credentials/recipients come entirely from environment variables (populated
from GitHub Actions secrets), never from config.yaml:

  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS  -- your SMTP account
  EMAIL_FROM  (defaults to SMTP_USER)
  EMAIL_TO    -- where alerts get sent
"""
from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText

from .search import FlightResult


class NotifyConfigError(RuntimeError):
    pass


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise NotifyConfigError(f"Missing required environment variable: {name}")
    return value


def format_results_html(results: list[FlightResult]) -> str:
    rows = []
    for r in results:
        points = f"{r.points:,}" if r.points is not None else "?"
        rows.append(
            f"<tr><td>{r.origin}</td><td>{r.destination}</td><td>{r.date}</td>"
            f"<td>{r.cabin}</td><td>{r.flight_number}</td><td>{points}</td></tr>"
        )
    return (
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<tr><th>From</th><th>To</th><th>Date</th><th>Cabin</th>"
        "<th>Flight</th><th>Points</th></tr>" + "".join(rows) + "</table>"
    )


def send_email(subject: str, results: list[FlightResult]) -> None:
    host = _env("SMTP_HOST", required=True)
    port = int(_env("SMTP_PORT", "587"))
    user = _env("SMTP_USER", required=True)
    password = _env("SMTP_PASS", required=True)
    sender = _env("EMAIL_FROM", user)
    recipient = _env("EMAIL_TO", required=True)

    body = format_results_html(results)
    msg = MIMEText(body, "html")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(sender, [recipient], msg.as_string())

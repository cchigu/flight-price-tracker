"""Sends email through a Gmail account using an app password.

The owner sets the Gmail address and app password once on their Account page (stored in the
local database on the server). Gmail requires 2-Step Verification for app passwords:
https://myaccount.google.com/apppasswords
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable, Optional

SMTP_HOST = "smtp.gmail.com"


class MailError(Exception):
    pass


class Mailer:
    def __init__(self, get_setting: Callable[[str, str], str]):
        self._get = get_setting

    @property
    def sender(self) -> str:
        return self._get("smtp_user", "")

    def configured(self) -> bool:
        return bool(self._get("smtp_user", "") and self._get("smtp_password", ""))

    def send(self, to: str, subject: str, text: str, html: Optional[str] = None) -> None:
        if not self.configured():
            raise MailError("Email sending isn't set up yet. The owner can set it up under Account.")
        msg = EmailMessage()
        msg["From"] = f"Flight Price Tracker <{self.sender}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        password = self._get("smtp_password", "").replace(" ", "")
        ctx = ssl.create_default_context()
        last: Exception | None = None
        # 587 (STARTTLS) first, 465 (SSL) if a network blocks it.
        for attempt in ("starttls", "ssl"):
            try:
                if attempt == "starttls":
                    with smtplib.SMTP(SMTP_HOST, 587, timeout=20) as s:
                        s.starttls(context=ctx)
                        s.login(self.sender, password)
                        s.send_message(msg)
                else:
                    with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=20, context=ctx) as s:
                        s.login(self.sender, password)
                        s.send_message(msg)
                return
            except smtplib.SMTPAuthenticationError as e:
                raise MailError("Gmail didn't accept the address and app password. Check both on the Account page.") from e
            except (OSError, smtplib.SMTPException) as e:
                last = e
        raise MailError(f"Couldn't reach Gmail to send email ({last}).")


class FakeMailer:
    """Collects messages instead of sending them (tests and offline demos)."""

    def __init__(self):
        self.sent: list[dict] = []
        self.sender = "tracker@example.com"

    def configured(self) -> bool:
        return True

    def send(self, to: str, subject: str, text: str, html: Optional[str] = None) -> None:
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})

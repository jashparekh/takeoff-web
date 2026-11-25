"""Sending the sign-in email. `console` logs the link (development and tests); `smtp` sends it."""
from __future__ import annotations
import logging
import smtplib
from email.message import EmailMessage

from ..settings import get_settings

log = logging.getLogger('takeoff.mail')
outbox: list[EmailMessage] = []        # console backend keeps messages here (tests read it)


def sign_in_message(to: str, link: str, minutes: int) -> EmailMessage:
    m = EmailMessage()
    m['From'] = get_settings().mail_from
    m['To'] = to
    m['Subject'] = 'Your Takeoff sign-in link'
    m.set_content(f'Open this link to sign in to Takeoff:\n\n{link}\n\n'
                  f'It works once and expires in {minutes} minutes. If you did not ask for it, ignore this email.')
    return m


def send(m: EmailMessage) -> None:
    s = get_settings()
    if s.mail_backend == 'console':
        outbox.append(m)
        log.warning('Sign-in email to %s (console backend):\n%s', m['To'], m.get_content())
        return
    if s.mail_backend != 'smtp' or not s.smtp_host:
        raise RuntimeError('MAIL_BACKEND must be "console" or "smtp" with SMTP_HOST set')
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as c:
        c.starttls()
        if s.smtp_user:
            c.login(s.smtp_user, s.smtp_password or '')
        c.send_message(m)

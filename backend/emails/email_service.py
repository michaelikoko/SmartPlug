import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from models.otp_code import OtpPurpose

logger = logging.getLogger("email_service")

SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ["SMTP_USERNAME"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USERNAME)
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "SmartPlug")

OTP_EXPIRE_MINUTES = int(os.getenv("OTP_EXPIRE_MINUTES", str(10)))


def send_otp_email(to: str, otp: str, purpose: OtpPurpose) -> None:
    """
    Send an OTP code via email for the specified purpose, using Gmail SMTP.
    """
    if purpose == OtpPurpose.PASSWORD_RESET:
        subject = "Your SmartPlug Pulse password reset code"
        heading = "Reset your password"
    else:
        subject = "Your SmartPlug Pulse verification code"
        heading = "Verify your email"

    html = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto;">
      <h2 style="color: #171717;">{heading}</h2>
      <p style="color: #525252; font-size: 15px;">
        Use the code below. It expires in {OTP_EXPIRE_MINUTES} minutes.
      </p>
      <div style="background: #f5f5f5; border-radius: 12px; padding: 24px; text-align: center; margin: 24px 0;">
        <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #171717;">{otp}</span>
      </div>
      <p style="color: #a3a3a3; font-size: 12px;">
        If you didn't request this code, you can safely ignore this email.
      </p>
    </div>
    """

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = formataddr((EMAIL_FROM_NAME, EMAIL_FROM))
    message["To"] = to
    message.attach(MIMEText(f"Your code is {otp}. It expires in {OTP_EXPIRE_MINUTES} minutes.", "plain"))
    message.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [to], message.as_string())
        logger.info("Sent OTP email to %s (purpose=%s)", to, purpose.value)
    except Exception as e:
        # Don't let an SMTP hiccup silently swallow the failure — log
        # loudly and re-raise. The OTP row already exists in the DB
        # (issue_otp commits before this is called), so the person can
        # still use "resend code" to retry once the underlying issue
        # (e.g. wrong app password) is fixed.
        logger.error("Failed to send OTP email to %s: %s", to, e)
        raise
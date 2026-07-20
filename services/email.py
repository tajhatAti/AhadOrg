"""OTP email delivery (Brevo). Self-contained: reads its own env config."""
import os
import logging
import requests
from fastapi import HTTPException

logger = logging.getLogger("ahad-co-app")

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES", "10"))


def send_email(receiver_email: str, subject: str, otp: str, username: str, purpose: str):
    brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()
    sender_email = os.getenv("SENDER_EMAIL", "").strip()
    sender_name = os.getenv("SENDER_NAME", "Ahad Co").strip()

    if not brevo_api_key or not sender_email:
        logger.error("BREVO_API_KEY or SENDER_EMAIL missing.")
        raise HTTPException(status_code=500, detail="Email service is not configured.")

    headers = {"accept": "application/json", "api-key": brevo_api_key, "content-type": "application/json"}

    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height:1.6; background:#0B0C14; padding:24px;">
        <div style="max-width:420px;margin:0 auto;background:#14152a;border-radius:16px;padding:28px;color:#F5F5FA;">
          <h2 style="color:#7C6CF6;margin-top:0;">{purpose}</h2>
          <p>Hello <b>{username}</b>,</p>
          <p>Your verification code is:</p>
          <div style="font-size:32px;font-weight:bold;letter-spacing:6px;color:#2FD9C4;text-align:center;
                      background:rgba(255,255,255,0.06);padding:16px;border-radius:12px;margin:16px 0;">
            {otp}
          </div>
          <p style="color:#A0A0B2;font-size:13px;">This code expires in {OTP_EXPIRY_MINUTES} minutes.
          If you did not request this, you can safely ignore this email.</p>
          <p style="color:#A0A0B2;font-size:12px;margin-top:24px;">— Ahad Co</p>
        </div>
      </body>
    </html>
    """

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": receiver_email}],
        "subject": subject,
        "textContent": f"Hello {username},\n\nYour code is: {otp}\n\nExpires in {OTP_EXPIRY_MINUTES} minutes.",
        "htmlContent": html_content
    }

    try:
        response = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=20)
        logger.info("Brevo status: %s", response.status_code)
        if response.status_code not in (200, 201, 202):
            raise HTTPException(status_code=500, detail="Failed to send email. Please try again.")
    except requests.RequestException:
        logger.exception("Brevo request failed")
        raise HTTPException(status_code=500, detail="Email service is temporarily unavailable.")

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging

from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

_gmail_creds = None


def _get_gmail_creds():
    global _gmail_creds
    if _gmail_creds:
        return _gmail_creds
    try:
        user = get_secret('KUMORI_GMAIL_USERNAME')
        password = get_secret('KUMORI_GMAIL_APP_PASSWORD')
        if user and password:
            _gmail_creds = {'user': user, 'password': password}
            return _gmail_creds
    except Exception as e:
        logger.error(f"Gmail credentials failed: {e}")
    return None


def send_simple_email(subject, body, to_email, from_name="crab.travel"):
    """Send a simple email via Gmail SMTP."""
    try:
        creds = _get_gmail_creds()
        if not creds:
            logger.error("No Gmail credentials available")
            return False

        message = MIMEMultipart()
        message['From'] = f'{from_name} <{creds["user"]}>'
        message['To'] = to_email
        message['Subject'] = subject
        message.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(creds['user'], creds['password'])
            server.send_message(message)

        logger.info(f"Email sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False

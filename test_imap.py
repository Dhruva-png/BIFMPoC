import os
import imaplib
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("IMAP_USERNAME")
PASSWORD = os.getenv("IMAP_PASSWORD")

try:
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login(EMAIL, PASSWORD)
    print("✅ IMAP login successful")

    mail.select("INBOX")

    status, messages = mail.search(None, "ALL")
    print("Status:", status)

    ids = messages[0].split()
    print("Number of emails:", len(ids))

    mail.logout()

except Exception as e:
    print("❌", e)
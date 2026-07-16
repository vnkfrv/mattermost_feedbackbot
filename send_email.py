import os
import ssl
import smtplib
import logging
from email.mime.text import MIMEText

log = logging.getLogger("email_sender")

GMAIL_PRESET = {"server": "smtp.gmail.com", "port": 587, "security": "starttls"}


def _mail_config():  #    Возвращает dict с настройками или None, если конфиг неполный/кривой
    system = (os.getenv("MAIL_SYSTEM") or "").strip().lower()

    if system == "gmail":
        cfg = {
            **GMAIL_PRESET,
            "sender": os.getenv("GMAIL_SENDER"),
            "login": os.getenv("GMAIL_SENDER"),
            "password": os.getenv("GMAIL_PASSWORD"),
            "recipient": os.getenv("GMAIL_RECIPIENT"),
        }
    elif system == "magnit":
        cfg = {
            "server": os.getenv("MAGNIT_SMTP_SERVER"),
            "port": os.getenv("MAGNIT_SMTP_PORT"),
            "security": (os.getenv("MAGNIT_SMTP_SECURITY") or "starttls")
            .strip()
            .lower(),
            "sender": os.getenv("MAGNIT_SENDER"),
            "login": os.getenv("MAGNIT_LOGIN") or os.getenv("MAGNIT_SENDER"),
            "password": os.getenv("MAGNIT_PASSWORD"),
            "recipient": os.getenv("MAGNIT_RECIPIENT"),
        }
    else:
        log.error("MAIL_SYSTEM должен быть gmail или magnit, получено: %r", system)
        return None

    if not all([cfg["server"], cfg["port"], cfg["sender"], cfg["recipient"]]):
        log.error("Почта (%s) не настроена: заполните переменные в .env", system)
        return None
    try:
        cfg["port"] = int(cfg["port"])
    except (TypeError, ValueError):
        log.error("SMTP-порт должен быть числом, получено: %r", cfg["port"])
        return None
    if cfg["security"] not in ("starttls", "ssl", "none"):
        log.error(
            "SMTP_SECURITY: ожидается starttls/ssl/none, получено %r", cfg["security"]
        )
        return None
    return cfg


def _tls_context():  
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def send_email(subject: str, body: str) -> bool:
    cfg = _mail_config()
    if cfg is None:
        return False

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["recipient"]

    try:
        if cfg["security"] == "ssl":
            server = smtplib.SMTP_SSL(
                cfg["server"], cfg["port"], timeout=10, context=_tls_context()
            )
        else:
            server = smtplib.SMTP(cfg["server"], cfg["port"], timeout=10)

        with server:
            if cfg["security"] == "starttls":
                server.starttls(context=_tls_context())
            if cfg["password"]:
                server.login(cfg["login"], cfg["password"])
            server.send_message(msg)
        log.info("Письмо успешно отправлено на %s", cfg["recipient"])
        return True
    except Exception as e:
        log.error(
            "Не удалось отправить письмо (%s:%s, режим %s): %s",
            cfg["server"],
            cfg["port"],
            cfg["security"],
            e,
        )
        return False

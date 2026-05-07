"""
FastAPI endpoint для получения переписки из Yandex Mail через IMAP.
Используется N8N для вызова через HTTP Request node.

Endpoints:
  POST /api/v1/emails/thread
    Body: {
      "manager_email": "manager@example.com",
      "manager_password": "app_password",
      "client_email": "client@example.com"
    }
    Response: {
      "success": true,
      "client_email": "client@example.com",
      "emails": [...]
    }

  GET /health - проверка работоспособности
"""

from __future__ import annotations

import email
import imaplib
import os
import re
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, EmailStr, Field

IMAP_SERVER = "imap.yandex.ru"
IMAP_PORT = 993

# API Key для безопасности (можно установить через переменную окружения)
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

app = FastAPI(
    title="MovetoRussia Mail Agent API",
    description="IMAP email thread extraction for N8N workflows",
    version="1.0.0",
)


def get_api_key() -> str:
    """Получить API ключ из переменной окружения."""
    return os.environ.get("API_KEY", "")


async def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    """Проверка API ключа."""
    expected_key = get_api_key()
    if not expected_key:
        # Если API_KEY не установлен, пропускаем проверку (для локальной разработки)
        return "development"
    if api_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Неверный API ключ",
        )
    return api_key


class EmailThreadRequest(BaseModel):
    """Запрос на получение переписки."""

    manager_email: EmailStr = Field(..., description="Email менеджера (Yandex)")
    manager_password: str = Field(
        ..., description="Пароль приложения Yandex (не основной пароль)"
    )
    client_email: EmailStr = Field(..., description="Email клиента")
    sent_mailbox: str | None = Field(
        "Sent", description="Название папки исходящих (Sent или Отправленные)"
    )


class EmailItem(BaseModel):
    """Одно письмо из переписки."""

    model_config = {"populate_by_name": True}

    folder: str = Field(..., description="Папка (INBOX или Sent)")
    subject: str = Field(..., description="Тема письма")
    from_addr: str = Field(..., description="От кого", alias="from")
    to_addr: str = Field(..., description="Кому", alias="to")
    date: str = Field(..., description="Дата отправки")
    text_plain: str = Field(..., description="Текст письма (plain text)")


class EmailThreadResponse(BaseModel):
    """Ответ с перепиской."""

    success: bool = Field(..., description="Успешность операции")
    client_email: str = Field(..., description="Email клиента")
    emails: list[EmailItem] = Field(..., description="Список писем")
    total_count: int = Field(..., description="Общее количество писем")
    error: str | None = Field(None, description="Описание ошибки (если есть)")


class HealthResponse(BaseModel):
    """Ответ health check."""

    status: str
    service: str


def decode_mime_words(s: str | None) -> str:
    """Декодирование MIME заголовков."""
    if s is None:
        return ""
    decoded_fragments = decode_header(s)
    return "".join(
        str(t, enc or "utf-8") if isinstance(t, bytes) else str(t)
        for t, enc in decoded_fragments
    )


def _decode_part_payload(part: email.message.Message) -> str:
    """Декодирование содержимого части письма."""
    try:
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="ignore")
    except Exception:
        return ""


def get_text_from_email(msg: email.message.Message) -> str:
    """
    Извлечение text/plain из MIME-дерева.
    HTML части игнорируются.
    """
    disp_attachment = re.compile(r"attachment", re.I)
    chunks: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() != "text/plain":
                continue
            if disp_attachment.search(str(part.get("Content-Disposition", ""))):
                continue
            block = _decode_part_payload(part).strip()
            if block:
                chunks.append(block)
        return "\n\n".join(chunks).strip()

    if msg.get_content_type() == "text/plain":
        return _decode_part_payload(msg).strip()
    return ""


def _imap_quote_addr(addr: str) -> str:
    """Экранирование email для IMAP запросов."""
    return addr.replace("\\", "\\\\").replace('"', '\\"')


def _raw_message_from_fetch(msg_data: list[Any] | None) -> bytes | None:
    """Извлечение сырого RFC822 сообщения из ответа FETCH."""
    if not msg_data:
        return None
    candidates: list[bytes] = []
    for item in msg_data:
        if isinstance(item, tuple):
            for part in item:
                if isinstance(part, (bytes, bytearray)):
                    candidates.append(bytes(part))
        elif isinstance(item, (bytes, bytearray)):
            candidates.append(bytes(item))
    if not candidates:
        return None
    return max(candidates, key=len)


def search_email_ids(
    mail: imaplib.IMAP4_SSL, contact_email: str, mailbox: str = "INBOX"
) -> list[bytes]:
    """Поиск ID писем, где клиент в FROM, TO или CC."""
    mail.select(mailbox, readonly=True)
    q = _imap_quote_addr(contact_email.strip())
    found: set[bytes] = set()
    for crit in (f'FROM "{q}"', f'TO "{q}"', f'CC "{q}"'):
        status, messages = mail.search(None, crit)
        if status == "OK" and messages and messages[0]:
            found.update(messages[0].split())
    return list(found)


def fetch_emails_by_ids(
    mail: imaplib.IMAP4_SSL,
    email_ids: list[bytes],
    folder_label: str = "",
) -> list[dict[str, Any]]:
    """Загрузка писем по списку ID."""
    seen: set[bytes] = set()
    out: list[dict[str, Any]] = []
    for eid in email_ids:
        if eid in seen:
            continue
        seen.add(eid)
        status, msg_data = mail.fetch(eid, "(BODY.PEEK[])")
        if status != "OK" or not msg_data:
            continue
        raw = _raw_message_from_fetch(msg_data)
        if not raw:
            continue
        msg = email.message_from_bytes(raw)
        subject = decode_mime_words(msg["Subject"]) or "Без темы"
        from_header = decode_mime_words(msg["From"]) or ""
        to_header = decode_mime_words(msg["To"]) or ""
        date_str = msg["Date"] or ""
        text_plain = get_text_from_email(msg)
        ts = 0.0
        if date_str:
            try:
                ts = parsedate_to_datetime(date_str).timestamp()
            except (TypeError, ValueError, OverflowError):
                ts = 0.0
        out.append(
            {
                "folder": folder_label,
                "subject": subject,
                "from": from_header,
                "to": to_header,
                "date": date_str,
                "textPlain": text_plain,
                "_ts": ts,
            }
        )
    return out


def connect_imap(manager_email: str, manager_password: str) -> imaplib.IMAP4_SSL:
    """Подключение к IMAP серверу Yandex."""
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(manager_email, manager_password)
    return mail


def collect_emails(
    manager_email: str, manager_password: str, client_email: str, sent_mailbox: str
) -> list[dict[str, Any]]:
    """Сбор всех писем между менеджером и клиентом."""
    mail = connect_imap(manager_email, manager_password)
    try:
        emails: list[dict[str, Any]] = []

        # Входящие письма
        ids_in = search_email_ids(mail, client_email, "INBOX")
        emails.extend(fetch_emails_by_ids(mail, ids_in, "INBOX"))

        # Исходящие письма
        try:
            ids_out = search_email_ids(mail, client_email, sent_mailbox)
            emails.extend(fetch_emails_by_ids(mail, ids_out, sent_mailbox))
        except (UnicodeEncodeError, imaplib.IMAP4.error) as e:
            # Если не удалось открыть папку исходящих, продолжаем с тем что есть
            print(f"Warning: не удалось открыть папку {sent_mailbox}: {e}")

        # Сортировка по времени
        emails.sort(key=lambda x: float(x.get("_ts", 0.0)))
        for item in emails:
            item.pop("_ts", None)

        return emails
    finally:
        try:
            mail.close()
        except Exception:
            pass
        mail.logout()


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Проверка работоспособности сервиса."""
    return HealthResponse(
        status="healthy",
        service="MovetoRussia Mail Agent API",
    )


@app.post(
    "/api/v1/emails/thread",
    response_model=EmailThreadResponse,
    dependencies=[Security(verify_api_key)],
)
async def get_email_thread(request: EmailThreadRequest):
    """
    Получить полную переписку между менеджером и клиентом.

    Требуется API ключ в заголовке X-API-Key (если установлен в переменных окружения).
    """
    try:
        emails = collect_emails(
            manager_email=request.manager_email,
            manager_password=request.manager_password,
            client_email=request.client_email,
            sent_mailbox=request.sent_mailbox or "Sent",
        )

        # Преобразование в EmailItem модели
        email_items = [
            EmailItem(
                folder=e["folder"],
                subject=e["subject"],
                from_addr=e["from"],
                to_addr=e["to"],
                date=e["date"],
                text_plain=e["textPlain"],
            )
            for e in emails
        ]

        return EmailThreadResponse(
            success=True,
            client_email=request.client_email,
            emails=email_items,
            total_count=len(email_items),
            error=None,
        )

    except imaplib.IMAP4.error as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Ошибка IMAP аутентификации: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка при получении писем: {str(e)}",
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

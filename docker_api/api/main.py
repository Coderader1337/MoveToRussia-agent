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
import html
import imaplib
import logging
import os
import re
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, EmailStr, Field

IMAP_SERVER = "imap.yandex.ru"
IMAP_PORT = 993
DEFAULT_LOG_FILE = "/app/logs/imap_debug.log"
IMAP_SEARCH_RETRIES = 10
IMAP_SEARCH_RETRY_DELAY_SECONDS = 1.0

# API Key для безопасности (можно установить через переменную окружения)
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def configure_logging() -> logging.Logger:
    """Настройка логирования в stdout и в отдельный файл."""
    log_file = Path(os.environ.get("LOG_FILE_PATH", DEFAULT_LOG_FILE))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | pid=%(process)d | %(name)s | %(message)s"
    )

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)

    logger_instance = logging.getLogger("mail_agent.imap")
    logger_instance.info("Логирование IMAP инициализировано, файл: %s", log_file)
    return logger_instance


logger = configure_logging()

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


class IncompleteImapSearchError(RuntimeError):
    """Поиск в IMAP завершился с временной ошибкой и дал неполный результат."""


def _decode_imap_values(values: list[Any] | tuple[Any, ...] | None) -> list[str]:
    """Безопасное преобразование IMAP ответа в строки для логов."""
    if not values:
        return []

    decoded: list[str] = []
    for value in values:
        if isinstance(value, (bytes, bytearray)):
            decoded.append(value.decode("utf-8", errors="ignore"))
        else:
            decoded.append(str(value))
    return decoded


def _email_ids_to_log(email_ids: list[bytes]) -> list[str]:
    """Преобразование списка IMAP ID в читаемый вид."""
    return [eid.decode("utf-8", errors="ignore") for eid in email_ids]


def _trim_for_log(value: str, limit: int = 160) -> str:
    """Обрезка длинных значений для читаемого лога."""
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def _is_transient_imap_search_error(status: str, messages: list[Any] | None) -> bool:
    """Проверка, что SEARCH упал на временной backend error у провайдера."""
    if status == "OK":
        return False

    joined_messages = " ".join(_decode_imap_values(messages)).upper()
    return "SEARCH BACKEND ERROR" in joined_messages or "UNAVAILABLE" in joined_messages


def _search_ids_by_criterion(
    mail: imaplib.IMAP4_SSL,
    mailbox: str,
    criterion: str,
    retries: int = IMAP_SEARCH_RETRIES,
) -> list[bytes]:
    """Поиск писем по одному критерию с повтором при временной ошибке IMAP."""
    last_status = ""
    last_messages: list[Any] | None = None

    for attempt in range(1, retries + 1):
        logger.info(
            "IMAP SEARCH attempt: mailbox=%s, criteria=%s, attempt=%s/%s",
            mailbox,
            criterion,
            attempt,
            retries,
        )
        status, messages = mail.search(None, criterion)
        ids = messages[0].split() if status == "OK" and messages and messages[0] else []
        logger.info(
            "IMAP SEARCH result: mailbox=%s, criteria=%s, attempt=%s/%s, status=%s, ids_count=%s, ids=%s, raw=%s",
            mailbox,
            criterion,
            attempt,
            retries,
            status,
            len(ids),
            _email_ids_to_log(ids),
            _decode_imap_values(messages),
        )

        if status == "OK":
            return ids

        last_status = status
        last_messages = messages

        if not _is_transient_imap_search_error(status, messages):
            raise imaplib.IMAP4.error(
                f"IMAP SEARCH failed for {mailbox} with criteria {criterion}: "
                f"status={status}, response={_decode_imap_values(messages)}"
            )

        if attempt < retries:
            logger.warning(
                "Временная ошибка IMAP SEARCH, повтор через %.1f сек: mailbox=%s, criteria=%s",
                IMAP_SEARCH_RETRY_DELAY_SECONDS,
                mailbox,
                criterion,
            )
            time.sleep(IMAP_SEARCH_RETRY_DELAY_SECONDS)

    raise IncompleteImapSearchError(
        f"IMAP SEARCH backend error for {mailbox} with criteria {criterion}: "
        f"status={last_status}, response={_decode_imap_values(last_messages)}"
    )


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


def _html_to_text(value: str) -> str:
    """Преобразование HTML в читаемый plain text."""
    if not value:
        return ""

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)</div\s*>", "\n", text)
    text = re.sub(r"(?i)</li\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\r", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _strip_quoted_reply_text(value: str) -> str:
    """Удаление вложенной истории переписки из ответа."""
    if not value:
        return ""

    text = value.replace("\r", "").strip()
    split_patterns = [
        r"(?im)^\s*On .+wrote:\s*$",
        r"(?im)^\s*В .+писал\(а\):\s*$",
        r"(?im)^\s*-{2,}\s*Original Message\s*-{2,}\s*$",
        r"(?im)^\s*Begin forwarded message:\s*$",
        r"(?im)^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$",
        r"(?im)^\s*From:\s+.+$",
        r"(?im)^\s*От:\s+.+$",
        r"(?im)^\s*Sent:\s+.+$",
        r"(?im)^\s*Дата:\s+.+$",
        r"(?im)^\s*To:\s+.+$",
        r"(?im)^\s*Кому:\s+.+$",
        r"(?im)^\s*Subject:\s+.+$",
        r"(?im)^\s*Тема:\s+.+$",
    ]

    cut_positions = [
        match.start()
        for pattern in split_patterns
        for match in [re.search(pattern, text)]
        if match
    ]

    if cut_positions:
        text = text[: min(cut_positions)].rstrip()

    lines = text.split("\n")
    cleaned_lines: list[str] = []
    for line in lines:
        if re.match(r"^\s*>+", line):
            break
        cleaned_lines.append(line)

    cleaned_text = "\n".join(cleaned_lines).strip()
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text)
    return cleaned_text.strip()


def _collect_part_content_types(msg: email.message.Message) -> list[str]:
    """Список MIME типов письма для логирования."""
    if msg.is_multipart():
        return [part.get_content_type() for part in msg.walk()]
    return [msg.get_content_type()]


def get_text_from_email(msg: email.message.Message) -> str:
    """
    Извлечение текста из MIME-дерева.
    Сначала text/plain, затем fallback на text/html.
    """
    disp_attachment = re.compile(r"attachment", re.I)
    plain_chunks: list[str] = []
    html_chunks: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if disp_attachment.search(str(part.get("Content-Disposition", ""))):
                continue

            content_type = part.get_content_type()
            block = _decode_part_payload(part).strip()
            if not block:
                continue

            if content_type == "text/plain":
                plain_chunks.append(block)
            elif content_type == "text/html":
                html_chunks.append(_html_to_text(block))

        if plain_chunks:
            joined_plain = "\n\n".join(chunk for chunk in plain_chunks if chunk).strip()
            return _strip_quoted_reply_text(joined_plain)
        if html_chunks:
            joined_html = "\n\n".join(chunk for chunk in html_chunks if chunk).strip()
            return _strip_quoted_reply_text(joined_html)
        return ""

    if msg.get_content_type() == "text/plain":
        return _strip_quoted_reply_text(_decode_part_payload(msg).strip())
    if msg.get_content_type() == "text/html":
        return _strip_quoted_reply_text(_html_to_text(_decode_part_payload(msg)))
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
    logger.info(
        "Старт поиска писем: mailbox=%s, contact_email=%s",
        mailbox,
        contact_email,
    )
    select_status, select_data = mail.select(mailbox, readonly=True)
    logger.info(
        "IMAP SELECT: mailbox=%s, readonly=%s, status=%s, data=%s",
        mailbox,
        True,
        select_status,
        _decode_imap_values(select_data),
    )
    if select_status != "OK":
        raise imaplib.IMAP4.error(f"Не удалось открыть папку {mailbox}")

    q = _imap_quote_addr(contact_email.strip())
    found: set[bytes] = set()
    for crit in (f'FROM "{q}"', f'TO "{q}"', f'CC "{q}"'):
        current_ids = _search_ids_by_criterion(mail, mailbox, crit)
        found.update(current_ids)

    result_ids = sorted(found, key=lambda item: int(item))
    logger.info(
        "Поиск завершен: mailbox=%s, total_unique_ids=%s, ids=%s",
        mailbox,
        len(result_ids),
        _email_ids_to_log(result_ids),
    )
    return result_ids


def fetch_emails_by_ids(
    mail: imaplib.IMAP4_SSL,
    email_ids: list[bytes],
    folder_label: str = "",
) -> list[dict[str, Any]]:
    """Загрузка писем по списку ID."""
    logger.info(
        "Старт загрузки писем: folder=%s, ids_count=%s, ids=%s",
        folder_label,
        len(email_ids),
        _email_ids_to_log(email_ids),
    )
    seen: set[bytes] = set()
    out: list[dict[str, Any]] = []
    for eid in email_ids:
        eid_text = eid.decode("utf-8", errors="ignore")
        if eid in seen:
            logger.info("IMAP FETCH пропущен повторно: folder=%s, id=%s", folder_label, eid_text)
            continue
        seen.add(eid)

        logger.info("IMAP FETCH start: folder=%s, id=%s", folder_label, eid_text)
        status, msg_data = mail.fetch(eid, "(BODY.PEEK[])")
        logger.info(
            "IMAP FETCH result: folder=%s, id=%s, status=%s, chunks=%s",
            folder_label,
            eid_text,
            status,
            len(msg_data or []),
        )
        if status != "OK" or not msg_data:
            logger.warning(
                "IMAP FETCH не вернул письмо: folder=%s, id=%s, status=%s",
                folder_label,
                eid_text,
                status,
            )
            continue
        raw = _raw_message_from_fetch(msg_data)
        if not raw:
            logger.warning(
                "Не удалось извлечь RFC822 из FETCH ответа: folder=%s, id=%s",
                folder_label,
                eid_text,
            )
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
        logger.info(
            "Письмо обработано: folder=%s, id=%s, subject=%s, date=%s, from=%s, to=%s, text_len=%s, content_types=%s",
            folder_label,
            eid_text,
            _trim_for_log(subject),
            date_str,
            _trim_for_log(from_header),
            _trim_for_log(to_header),
            len(text_plain),
            _collect_part_content_types(msg),
        )
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
    logger.info("Загрузка писем завершена: folder=%s, loaded_count=%s", folder_label, len(out))
    return out


def connect_imap(manager_email: str, manager_password: str) -> imaplib.IMAP4_SSL:
    """Подключение к IMAP серверу Yandex."""
    logger.info(
        "Подключение к IMAP Yandex: server=%s, port=%s, manager_email=%s",
        IMAP_SERVER,
        IMAP_PORT,
        manager_email,
    )
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    logger.info("SSL соединение с IMAP установлено")
    mail.login(manager_email, manager_password)
    logger.info("IMAP аутентификация успешна: manager_email=%s", manager_email)

    try:
        status, mailboxes = mail.list()
        logger.info(
            "IMAP LIST mailboxes: status=%s, mailboxes=%s",
            status,
            _decode_imap_values(mailboxes),
        )
    except imaplib.IMAP4.error:
        logger.exception("Не удалось получить список IMAP папок")

    return mail


def collect_emails(
    manager_email: str, manager_password: str, client_email: str, sent_mailbox: str
) -> list[dict[str, Any]]:
    """Сбор всех писем между менеджером и клиентом."""
    logger.info(
        "Старт сбора переписки: manager_email=%s, client_email=%s, sent_mailbox=%s",
        manager_email,
        client_email,
        sent_mailbox,
    )
    mail = connect_imap(manager_email, manager_password)
    try:
        emails: list[dict[str, Any]] = []

        # Входящие письма
        ids_in = search_email_ids(mail, client_email, "INBOX")
        logger.info("Найдены входящие письма: count=%s", len(ids_in))
        emails.extend(fetch_emails_by_ids(mail, ids_in, "INBOX"))

        # Исходящие письма
        try:
            ids_out = search_email_ids(mail, client_email, sent_mailbox)
            logger.info(
                "Найдены исходящие письма: mailbox=%s, count=%s",
                sent_mailbox,
                len(ids_out),
            )
            emails.extend(fetch_emails_by_ids(mail, ids_out, sent_mailbox))
        except (UnicodeEncodeError, imaplib.IMAP4.error) as e:
            # Если не удалось открыть папку исходящих, продолжаем с тем что есть
            logger.warning("Не удалось открыть папку исходящих %s: %s", sent_mailbox, e)

        # Сортировка по времени
        emails.sort(key=lambda x: float(x.get("_ts", 0.0)))
        for item in emails:
            item.pop("_ts", None)

        logger.info("Сбор переписки завершен: total_emails=%s", len(emails))
        return emails
    finally:
        try:
            mail.close()
            logger.info("IMAP mailbox закрыт")
        except Exception:
            logger.exception("Не удалось закрыть текущую IMAP папку")
        mail.logout()
        logger.info("IMAP сессия завершена")


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
        logger.info(
            "HTTP запрос на получение переписки: manager_email=%s, client_email=%s, sent_mailbox=%s",
            request.manager_email,
            request.client_email,
            request.sent_mailbox or "Sent",
        )
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

        logger.info(
            "HTTP запрос обработан успешно: client_email=%s, total_count=%s",
            request.client_email,
            len(email_items),
        )
        return EmailThreadResponse(
            success=True,
            client_email=request.client_email,
            emails=email_items,
            total_count=len(email_items),
            error=None,
        )

    except imaplib.IMAP4.error as e:
        logger.exception("Ошибка IMAP при получении переписки: client_email=%s", request.client_email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Ошибка IMAP аутентификации: {str(e)}",
        )
    except IncompleteImapSearchError as e:
        logger.exception(
            "Поиск переписки неполный из-за временной ошибки IMAP: client_email=%s",
            request.client_email,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "IMAP Yandex временно вернул неполный результат поиска. "
                "Повторите запрос позже."
            ),
        )
    except Exception as e:
        logger.exception(
            "Внутренняя ошибка при получении переписки: client_email=%s",
            request.client_email,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка при получении писем: {str(e)}",
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

# N8N Cloud — Получение писем Яндекс Почты

## 📌 Решение для N8N Cloud (без внешних серверов)

Используем **Code Node** в N8N Cloud с встроенной IMAP библиотекой.

---

## 🔧 Code Node для N8N Cloud

### Node.js Code Node конфиг

Создай в N8N новый узел **Code** с типом **Execute once**:

```javascript
const imapSimple = require('imap-simple');
const { simpleParser } = require('mailparser');

async function getEmails(clientEmail) {
  const config = {
    imap: {
      user: process.env.MAIL_ADRESS,
      password: process.env.MAIL_KEY,
      host: 'imap.yandex.ru',
      port: 993,
      tls: true,
      tlsOptions: { rejectUnauthorized: false },
      authTimeout: 10000,
    },
  };

  if (!config.imap.user || !config.imap.password) {
    return { error: 'MAIL_ADRESS и MAIL_KEY не установлены' };
  }

  try {
    const connection = await imapSimple.connect(config);
    const allEmails = [];

    // Поиск в INBOX
    await connection.openBox('INBOX', false);
    let searchCriteria = ['ALL', ['FROM', clientEmail]];
    let emails = await connection.search(searchCriteria, { bodies: '' });
    allEmails.push(...await parseEmails(connection, emails, 'INBOX'));

    // Поиск в Sent (исходящие)
    try {
      await connection.openBox('Sent', false);
      searchCriteria = ['ALL', ['TO', clientEmail]];
      emails = await connection.search(searchCriteria, { bodies: '' });
      allEmails.push(...await parseEmails(connection, emails, 'Sent'));
    } catch (e) {
      console.log('Папка Sent не найдена');
    }

    await connection.end();

    // Сортировка по дате
    allEmails.sort((a, b) => new Date(a.date) - new Date(b.date));

    return {
      client_email: clientEmail,
      emails_count: allEmails.length,
      emails: allEmails,
    };
  } catch (error) {
    return { error: `Ошибка IMAP: ${error.message}` };
  }
}

async function parseEmails(connection, emails, folder) {
  const parsed = [];
  
  for (const message of emails) {
    try {
      const msg = await simpleParser(message.parts[0].body);
      parsed.push({
        folder,
        subject: msg.subject || 'Без темы',
        from: msg.from?.text || '',
        to: msg.to?.text || '',
        date: msg.date?.toISOString() || '',
        textPlain: msg.text || '',
      });
    } catch (e) {
      console.log(`Ошибка парсинга письма: ${e.message}`);
    }
  }
  
  return parsed;
}

// Получаем email клиента из входа
const clientEmail = $input.first().json.client_email || 'peterdixon86@outlook.com';
const result = await getEmails(clientEmail);

return [{ json: result }];
```

---

## ⚙️ Настройка N8N

### 1. Добавь переменные окружения в N8N Cloud

В панели управления → **Settings** → **Environment Variables**:

```
MAIL_ADRESS = manager@yandex.ru
MAIL_KEY = your_app_password
```

### 2. Workflow структура

```
Input (Webhook/Trigger)
    ↓
Code Node (IMAP логика выше)
    ↓
Process (Function Node — форматирование)
    ↓
LLM (DeepSeek для ответа)
    ↓
Output (Email/Sheets)
```

### 3. Входные данные для Code Node

Предыдущий узел должен передать:
```json
{
  "client_email": "peterdixon86@outlook.com"
}
```

---

## 🔄 Альтернатива: Более простой вариант через Function Node

Если Code Node не работает, используй **Function Node**:

```javascript
return [
  {
    json: {
      // Placeholder — N8N Cloud не поддерживает IMAP прямо
      // Используй вместо этого webhook на внешний микросервис
      error: 'Нужен микросервис для IMAP',
      hint: 'Разверни imap_http_server.py на Render/Railway/Heroku'
    }
  }
];
```

---

## 🚀 Если Code Node не работает → используй HTTP запрос к микросервису

### Вариант: Развернуть сервис на **Render.com** (бесплатно)

1. Создай репо на GitHub с файлом `imap_http_server.py` (из документации ниже)
2. В Render: `New → Web Service → GitHub repo`
3. В N8N добавь HTTP узел:

```json
{
  "url": "https://your-service.onrender.com/emails",
  "method": "POST",
  "headers": {
    "Content-Type": "application/json"
  },
  "body": {
    "client_email": "{{ $json.client_email }}"
  }
}
```

---

## 📌 Python микросервис (если нужен внешний сервис)

### `imap_http_server.py`

```python
from flask import Flask, request, jsonify
import imaplib
import email
import re
from email.header import decode_header
from email.utils import parsedate_to_datetime
import os

app = Flask(__name__)

IMAP_SERVER = "imap.yandex.ru"
IMAP_PORT = 993

def decode_mime_words(s):
    if s is None:
        return ""
    decoded_fragments = decode_header(s)
    result = ""
    for t, enc in decoded_fragments:
        if isinstance(t, bytes):
            result += t.decode(enc or 'utf-8', errors='ignore')
        else:
            result += str(t)
    return result

def get_text_from_email(msg):
    """Извлекает text/plain"""
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() != "text/plain":
                continue
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or 'utf-8'
                text = payload.decode(charset, errors='ignore').strip()
                if text:
                    chunks.append(text)
    else:
        if msg.get_content_type() == "text/plain":
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                return payload.decode(charset, errors='ignore').strip()
    return "\n\n".join(chunks).strip()

def search_emails(client_email):
    """Получает письма через IMAP"""
    mail_addr = os.getenv("MAIL_ADRESS")
    mail_key = os.getenv("MAIL_KEY")
    
    if not mail_addr or not mail_key:
        return {"error": "Переменные окружения не установлены"}
    
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=10)
        mail.login(mail_addr, mail_key)
    except Exception as e:
        return {"error": f"Ошибка подключения: {str(e)}"}
    
    emails = []
    
    try:
        # INBOX
        mail.select("INBOX", readonly=True)
        status, messages = mail.search(None, f'FROM "{client_email}"')
        if status == "OK" and messages[0]:
            for msg_id in messages[0].split():
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status == "OK":
                    msg = email.message_from_bytes(msg_data[0][1])
                    emails.append({
                        "folder": "INBOX",
                        "subject": decode_mime_words(msg["Subject"]) or "Без темы",
                        "from": decode_mime_words(msg["From"]) or "",
                        "to": decode_mime_words(msg["To"]) or "",
                        "date": msg["Date"] or "",
                        "textPlain": get_text_from_email(msg),
                    })
        
        # Sent
        try:
            mail.select("Sent", readonly=True)
            status, messages = mail.search(None, f'TO "{client_email}"')
            if status == "OK" and messages[0]:
                for msg_id in messages[0].split():
                    status, msg_data = mail.fetch(msg_id, "(RFC822)")
                    if status == "OK":
                        msg = email.message_from_bytes(msg_data[0][1])
                        emails.append({
                            "folder": "Sent",
                            "subject": decode_mime_words(msg["Subject"]) or "Без темы",
                            "from": decode_mime_words(msg["From"]) or "",
                            "to": decode_mime_words(msg["To"]) or "",
                            "date": msg["Date"] or "",
                            "textPlain": get_text_from_email(msg),
                        })
        except:
            pass
        
        mail.close()
        mail.logout()
    except Exception as e:
        return {"error": f"Ошибка при получении писем: {str(e)}"}
    
    return {
        "client_email": client_email,
        "emails_count": len(emails),
        "emails": emails,
    }

@app.route("/emails", methods=["POST"])
def get_emails():
    data = request.get_json() or {}
    client_email = data.get("client_email", "")
    
    if not client_email:
        return jsonify({"error": "client_email required"}), 400
    
    result = search_emails(client_email)
    return jsonify(result)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
```

### `requirements.txt`

```
Flask==3.0.0
python-dotenv==1.0.0
```

### `Procfile` (для Render/Heroku)

```
web: python imap_http_server.py
```

---

## 📋 N8N Workflow (финальный вариант)

### Узел 1: Trigger (Webhook или Manual)
Передаёт:
```json
{
  "client_email": "peterdixon86@outlook.com"
}
```

### Узел 2: HTTP Request (если используешь микросервис)
```
Method: POST
URL: https://your-service.onrender.com/emails
Body:
{
  "client_email": "{{ $json.client_email }}"
}
```

### Узел 3: Function Node (обработка ответа)
```javascript
return items.map(item => ({
  json: {
    emails: item.json.emails,
    client_email: item.json.client_email,
    count: item.json.emails_count,
  }
}));
```

### Узел 4: Code Node (передача в LLM)
```javascript
const emails = $input.first().json.emails;
const emailText = emails
  .map(e => `[${e.folder}] ${e.date}\nFrom: ${e.from}\nTo: ${e.to}\nSubject: ${e.subject}\n\n${e.textPlain}`)
  .join('\n' + '='.repeat(80) + '\n');

return [{
  json: {
    client_email: $input.first().json.client_email,
    emails_text: emailText,
  }
}];
```

### Узел 5: LLM (DeepSeek/Claude)
Отправляешь `emails_text` в LLM для генерации ответа

---

## ✅ Итоговая рекомендация

1. **Если нужно только в N8N Cloud**: используй **Code Node** выше ↑
2. **Если Code Node не работает**: разверни микросервис на **Render.com** (5 минут) + HTTP узел в N8N
3. **Тестируй** на малом количестве писем первый раз

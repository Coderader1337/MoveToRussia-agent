# N8N Workflow: MovetoRussia Mail Agent

## 🎯 Задача
Автоматизировать первый контакт с лидами. Workflow читает письмо клиента из Google Sheets, извлекает контекст из CRM и почты, собирает промпт, генерирует ответ через LLM и записывает результат обратно в Sheets.

## 📐 Архитектура
Google Sheets (триггер) → CRM Lookup → IMAP Email History → Assemble Prompt → LLM API → Google Sheets Update

---

## 🔧 Узлы и конфигурация

**1. Google Sheets Trigger (Polling)**
- Spreadsheet: "MovetoRussia Mail Agent"
- Sheet: "Обработка писем"
- Watch Column: "email_клиента"
- Polling Interval: 300 сек (5 минут)
- Trigger Mode: "Rows Changed"
- Output fields: {email_клиента, manager_id, дата_добавления}

**2. HTTP: CRM Lookup**
- Method: GET
- URL: `https://crm.movetorussia.com/api/v1/contacts?email={{$json.email_клиента}}`
- Auth Header: `Authorization: Bearer {{$env.CRM_API_KEY}}`
- Extract from response:
  - manager_id
  - first_message_from_client (первый запрос клиента)
  - client_metadata (имя, страна, статус, компания)
- Add retry: 2 attempts with delay

**3. Set Data: Map Manager ID to Email**
- Lookup table: {{$env.MANAGER_ID_MAPPING}} (JSON: {"1":"manager1@yandex.com", "2":"manager2@yandex.com"})
- Output: {manager_email}

**4. Email: IMAP**
- Host: imap.yandex.com, Port: 993, SSL: enabled
- Email: {{$json.manager_email}}
- Password: {{$env.YANDEX_MAIL_PASSWORD}} (use App Password for security)
- Search Criteria: FROM {{$json.email_клиента}}
- Sort: DATE DESC (newest first)
- Max Results: 50
- Extract: {from, to, date, subject, body} for each email
- Format output: chronological order (oldest → newest)

**5. Set Data: Assemble Prompt**
Combine into final_prompt:
- System prompt (правила: мягкость, вежливость, эмпатия, созвон)
- Client info: {{$json.client_metadata}} + {{$json.email_клиента}}
- Conversation history: all emails chronologically
- First question from CRM: {{$json.first_message_from_client}}
- 2-3 examples of ideal responses
- Task: "Generate next email to client"

**6. HTTP: LLM API (DeepSeek)**
- Method: POST
- URL: `https://api.deepseek.com/v1/chat/completions`
- Auth: Bearer {{$env.DEEPSEEK_API_KEY}}
- Body (JSON):
```
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "Ты менеджер MovetoRussia. Помогай лидам переехать в Россию. Будь вежлив, предсказуем, заботлив. Не выдумывай факты. Предложи созвон."},
    {"role": "user", "content": "{{$json.final_prompt}}"}
  ],
  "temperature": 0.7,
  "max_tokens": 1000
}
```
- Alternative: Claude (Anthropic) или GPT-4 (OpenAI)
- Retry: 3 attempts

**7. Extract JSON / Parse Response**
- Extract: response.choices[0].message.content
- Clean up: remove extra whitespace, trim
- Output: {generated_email}

**8. Google Sheets: Update**
- Write {{$json.generated_email}} to column "generated_response" in same row
- Update column "status" to "обработано"
- Add timestamp in column "processed_at"
- Highlight row (optional)

---

## 🔐 Environment Variables (N8N Secrets)
```
GOOGLE_SHEETS_API_KEY=[Your Google API key]
CRM_API_KEY=[Your CRM API key]
YANDEX_MAIL_PASSWORD=[Yandex App Password]
DEEPSEEK_API_KEY=[DeepSeek API key]
MANAGER_ID_MAPPING={"1":"manager1@yandex.com","2":"manager2@yandex.com"}
```

---

## ✅ Key Requirements
- Answer ALL direct questions from client
- Sound like experienced live manager (not robotic)
- Handle sensitive topics (visa, politics) with care: acknowledge → explain → suggest next step
- DO NOT invent facts, links, timelines, statistics, or 100% visa guarantees
- DO NOT mention AI, automation, or that you are a bot
- DO NOT go into politics; focus on service and practice
- Gently guide conversation toward phone call / consultation

## 🧪 Testing Checklist
1. ✅ Add test row to Google Sheets with test email
2. ✅ Verify CRM lookup returns correct manager_id
3. ✅ Check IMAP loads email history correctly
4. ✅ Manually inspect assembled prompt
5. ✅ Verify LLM generates appropriate response (not generic)
6. ✅ Confirm result appears in Google Sheets within 2 minutes
7. ✅ Check error handling: test with invalid email, missing CRM data

## 💡 Tips
- Use "Test" button on each node to debug
- Log intermediate results (add Set Data nodes for inspection)
- Monitor API rate limits (CRM, LLM)
- Cache CRM results for 24h if rate-limited
- Set up alerts in N8N if workflow fails

import imaplib
import email
import os
import json
import re
from email.header import decode_header
from dotenv import load_dotenv
from pathlib import Path
import datetime
import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Загружаем переменные окружения из .env файла
load_dotenv()

# Получаем данные для подключения к почте
MAIL_ADDRESS = os.getenv('MAIL_ADRESS').strip()
MAIL_KEY = os.getenv('MAIL_KEY').strip()

# Настройки IMAP для Яндекса
IMAP_SERVER = 'imap.yandex.ru'
IMAP_PORT = 993

# Адрес отправителя, от которого нужно экспортировать письма
SENDER_EMAIL = 'palamarchuk.lesha@gmail.com'

# Настройки Google Sheets
GOOGLE_CREDENTIALS_FILE = 'credentials.json'  # Файл, скачанный с Google Cloud Console
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID')
TOKEN_FILE = 'token.json'  # Файл для сохранения токена (создается автоматически)

# Папка для сохранения локальной копии JSON
OUTPUT_DIR = Path('exported_emails_json')
OUTPUT_DIR.mkdir(exist_ok=True)

def clean_text(text):
    """Очищает текст от артефактов \r, \n и лишних пробелов"""
    if not text:
        return ""
    
    # Заменяем \r\n и \n на пробелы
    text = text.replace('\r\n', ' ')
    text = text.replace('\r', ' ')
    text = text.replace('\n', ' ')
    
    # Заменяем множественные пробелы на один
    text = re.sub(r'\s+', ' ', text)
    
    # Убираем пробелы в начале и конце
    text = text.strip()
    
    return text

def decode_mime_words(s):
    """Декодирует MIME-закодированные строки"""
    if s is None:
        return ''
    decoded_fragments = decode_header(s)
    return ''.join(
        str(text, encoding or 'utf-8') if isinstance(text, bytes) else str(text)
        for text, encoding in decoded_fragments
    )

def get_text_from_email(msg):
    """Извлекает текстовую часть (text/plain) из письма"""
    text_content = ""
    
    if msg.is_multipart():
        # Проходим по всем частям письма
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            
            # Ищем text/plain часть (не вложения)
            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    # Получаем payload
                    payload = part.get_payload(decode=True)
                    
                    if payload:
                        # Определяем кодировку
                        charset = part.get_content_charset()
                        if charset is None:
                            charset = 'utf-8'
                        
                        # Декодируем в UTF-8
                        text_content = payload.decode(charset, errors='ignore')
                        break
                except Exception as e:
                    print(f"  Ошибка при декодировании части письма: {e}")
                    continue
    else:
        # Если письмо не multipart
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset()
                if charset is None:
                    charset = 'utf-8'
                text_content = payload.decode(charset, errors='ignore')
        except Exception as e:
            print(f"  Ошибка при декодировании письма: {e}")
    
    return text_content.strip()

def connect_to_imap():
    """Подключается к IMAP серверу"""
    print(f'Подключение к {IMAP_SERVER}...')
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    print(f'Авторизация для {MAIL_ADDRESS}...')
    mail.login(MAIL_ADDRESS, MAIL_KEY)
    return mail

def get_google_sheets_auth():
    """Получает OAuth токен для Google Sheets"""
    creds = None
    
    # Если токен уже сохранен, используем его
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, scopes=[
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ])
    
    # Если токен истек или отсутствует, просим аутентификацию
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print('Обновление токена Google...')
            creds.refresh(Request())
        else:
            if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
                print('\n⚠️ ОШИБКА: Файл credentials.json не найден!')
                print('\nЧтобы получить credentials.json:')
                print('1. Перейдите на https://console.cloud.google.com/')
                print('2. Создайте новый проект')
                print('3. Включите Google Sheets API и Google Drive API')
                print('4. Создайте OAuth 2.0 Client ID (Desktop application)')
                print('5. Скачайте файл и переименуйте его в credentials.json')
                print('6. Поместите файл в текущую папку')
                return None
            
            print('Аутентификация через Google...')
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CREDENTIALS_FILE,
                scopes=[
                    'https://www.googleapis.com/auth/spreadsheets',
                    'https://www.googleapis.com/auth/drive'
                ]
            )
            creds = flow.run_local_server(port=0)
        
        # Сохраняем токен для будущих использований
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    
    return creds

def connect_to_google_sheets():
    """Подключается к Google Sheets"""
    print('Подключение к Google Sheets...')
    
    creds = get_google_sheets_auth()
    if not creds:
        return None
    
    # Создаем клиент gspread
    client = gspread.authorize(creds)
    
    # Открываем таблицу по ID
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    
    print(f'✅ Подключено к таблице: {spreadsheet.title}')
    
    return spreadsheet

def export_emails_from_sender(mail, sender):
    """Экспортирует все письма от указанного отправителя"""
    # Выбираем папку INBOX
    mail.select('INBOX')
    
    # Ищем письма от указанного отправителя
    print(f'Поиск писем от {sender}...')
    status, messages = mail.search(None, f'FROM "{sender}"')
    
    if status != 'OK':
        print('Ошибка при поиске писем')
        return []
    
    # Получаем список ID писем
    email_ids = messages[0].split()
    total_emails = len(email_ids)
    
    print(f'Найдено писем: {total_emails}')
    
    if total_emails == 0:
        print('Нет писем от указанного отправителя')
        return []
    
    # Список для всех писем
    all_emails = []
    
    # Обрабатываем каждое письмо
    for i, email_id in enumerate(email_ids, 1):
        print(f'Обработка письма {i}/{total_emails}...')
        
        # Получаем письмо
        status, msg_data = mail.fetch(email_id, '(RFC822)')
        
        if status != 'OK':
            print(f'Ошибка при получении письма {email_id}')
            continue
        
        # Парсим письмо
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        
        # Получаем тему письма
        subject = decode_mime_words(msg['Subject']) or 'Без темы'
        
        # Получаем отправителя
        from_header = decode_mime_words(msg['From']) or ''
        
        # Получаем дату письма
        date_str = msg['Date'] or ''
        
        # Извлекаем текстовое содержимое
        text_plain = get_text_from_email(msg)
        
        # Очищаем текст от артефактов
        text_plain_cleaned = clean_text(text_plain)
        
        # Создаем объект письма
        email_data = {
            "subject": subject,
            "from": from_header,
            "date": date_str,
            "textPlain": text_plain_cleaned
        }
        
        all_emails.append(email_data)
        
        print(f'  ✓ {subject[:50]}...')
    
    return all_emails

def save_to_json(emails):
    """Сохраняет письма в JSON файлы"""
    # Сохраняем все письма в один JSON файл
    output_file = OUTPUT_DIR / 'emails.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(emails, f, ensure_ascii=False, indent=2)
    
    print(f'\n✓ Письма сохранены локально: {output_file.absolute()}')

def upload_to_google_sheets(emails, spreadsheet):
    """Загружает письма в Google Sheets"""
    print('\nЗагрузка данных в Google Sheets...')
    
    # Получаем первый лист или создаем новый
    try:
        worksheet = spreadsheet.worksheet('Emails')
        print('Используется существующий лист "Emails"')
        worksheet.clear()
    except:
        worksheet = spreadsheet.add_worksheet(title='Emails', rows=len(emails) + 100, cols=4)
        print('Создан новый лист "Emails"')
    
    # Подготавливаем данные для загрузки (включая заголовки)
    all_rows = []
    
    # Добавляем заголовки
    headers = ['Subject', 'From', 'Date', 'Text']
    all_rows.append(headers)
    
    # Добавляем все письма
    for email_data in emails:
        row = [
            email_data['subject'],
            email_data['from'],
            email_data['date'],
            email_data['textPlain']
        ]
        all_rows.append(row)
    
    # Загружаем все данные сразу
    try:
        worksheet.update('A1', all_rows, raw=False)
        print(f'✓ Загружено {len(emails)} писем')
    except Exception as e:
        print(f'Ошибка при загрузке: {e}')
        print('Пытаюсь альтернативный способ...')
        # Если первый способ не сработал, используем append_rows
        try:
            worksheet.update('A1', all_rows)
            print(f'✓ Загружено {len(emails)} писем (альтернативный метод)')
        except Exception as e2:
            print(f'Ошибка и при альтернативном методе: {e2}')
    
    # Форматируем заголовки (жирный шрифт)
    try:
        worksheet.format('A1:D1', {
            'textFormat': {'bold': True},
            'backgroundColor': {'red': 0.9, 'green': 0.9, 'blue': 0.9}
        })
    except:
        pass
    
    # Автоматически подгоняем ширину колонок
    try:
        worksheet.columns_auto_resize(0, 3)
    except:
        pass

def main():
    """Основная функция"""
    try:
        # Проверяем наличие Google Sheet ID
        if not GOOGLE_SHEET_ID:
            print('ОШИБКА: Не указан GOOGLE_SHEET_ID в .env файле')
            print('\nДобавьте в .env файл:')
            print('GOOGLE_SHEET_ID=1xsaw1jDwkZPtlF8knfDx0mgidLsAhrsj8gZjHXBNzZk')
            return
        
        # Подключаемся к IMAP
        mail = connect_to_imap()
        
        # Экспортируем письма
        emails = export_emails_from_sender(mail, SENDER_EMAIL)
        
        # Закрываем соединение с почтой
        mail.close()
        mail.logout()
        
        if not emails:
            print('Нет писем для загрузки')
            return
        
        # Сохраняем локально в JSON
        save_to_json(emails)
        
        # Подключаемся к Google Sheets
        spreadsheet = connect_to_google_sheets()
        if not spreadsheet:
            return
        
        # Загружаем данные в Google Sheets
        upload_to_google_sheets(emails, spreadsheet)
        
        print(f'\n✅ Готово!')
        print(f'Таблица: {spreadsheet.url}')
        
    except imaplib.IMAP4.error as e:
        print(f'Ошибка IMAP: {e}')
    except Exception as e:
        print(f'Произошла ошибка: {e}')
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()

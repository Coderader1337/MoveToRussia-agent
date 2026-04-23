import imaplib
import email
import os
import json
import base64
from email.header import decode_header
from dotenv import load_dotenv
from pathlib import Path
import datetime

# Загружаем переменные окружения из .env файла
load_dotenv()

# Получаем данные для подключения
MAIL_ADDRESS = os.getenv('MAIL_ADRESS').strip()
MAIL_KEY = os.getenv('MAIL_KEY').strip()

# Настройки IMAP для Яндекса
IMAP_SERVER = 'imap.yandex.ru'
IMAP_PORT = 993

# Адрес отправителя, от которого нужно экспортировать письма
SENDER_EMAIL = 'palamarchuk.lesha@gmail.com'

# Папка для сохранения писем
OUTPUT_DIR = Path('exported_emails_json')
OUTPUT_DIR.mkdir(exist_ok=True)

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

def sanitize_filename(filename):
    """Создает безопасное имя файла"""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename[:200]

def connect_to_imap():
    """Подключается к IMAP серверу"""
    print(f'Подключение к {IMAP_SERVER}...')
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    print(f'Авторизация для {MAIL_ADDRESS}...')
    mail.login(MAIL_ADDRESS, MAIL_KEY)
    return mail

def export_emails_from_sender(mail, sender):
    """Экспортирует все письма от указанного отправителя в JSON"""
    # Выбираем папку INBOX
    mail.select('INBOX')
    
    # Ищем письма от указанного отправителя
    print(f'Поиск писем от {sender}...')
    status, messages = mail.search(None, f'FROM "{sender}"')
    
    if status != 'OK':
        print('Ошибка при поиске писем')
        return
    
    # Получаем список ID писем
    email_ids = messages[0].split()
    total_emails = len(email_ids)
    
    print(f'Найдено писем: {total_emails}')
    
    if total_emails == 0:
        print('Нет писем от указанного отправителя')
        return
    
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
        
        # Создаем объект письма
        email_data = {
            "subject": subject,
            "from": from_header,
            "date": date_str,
            "textPlain": text_plain
        }
        
        all_emails.append(email_data)
        
        print(f'  Обработано: {subject[:50]}...')
    
    # Сохраняем все письма в один JSON файл
    output_file = OUTPUT_DIR / 'emails.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_emails, f, ensure_ascii=False, indent=2)
    
    print(f'\nВсего экспортировано писем: {total_emails}')
    print(f'Письма сохранены в файл: {output_file.absolute()}')
    
    # Также сохраняем каждое письмо в отдельный файл для удобства
    print('\nСохранение отдельных файлов...')
    for idx, email_data in enumerate(all_emails, 1):
        # Создаем имя файла на основе даты и темы
        try:
            date_tuple = email.utils.parsedate_tz(email_data['date'])
            if date_tuple:
                local_date = datetime.datetime.fromtimestamp(
                    email.utils.mktime_tz(date_tuple)
                )
                date_prefix = local_date.strftime('%Y-%m-%d_%H-%M-%S')
            else:
                date_prefix = f'email_{idx:03d}'
        except:
            date_prefix = f'email_{idx:03d}'
        
        filename = f'{date_prefix}_{sanitize_filename(email_data["subject"])}.json'
        filepath = OUTPUT_DIR / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(email_data, f, ensure_ascii=False, indent=2)
    
    print(f'Отдельные файлы сохранены в папке: {OUTPUT_DIR.absolute()}')

def main():
    """Основная функция"""
    try:
        # Подключаемся к IMAP
        mail = connect_to_imap()
        
        # Экспортируем письма
        export_emails_from_sender(mail, SENDER_EMAIL)
        
        # Закрываем соединение
        mail.close()
        mail.logout()
        print('\nГотово!')
        
    except imaplib.IMAP4.error as e:
        print(f'Ошибка IMAP: {e}')
    except Exception as e:
        print(f'Произошла ошибка: {e}')
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()

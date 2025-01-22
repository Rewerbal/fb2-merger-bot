import os
import uuid
import logging
import re
import aiofiles
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes
)
from lxml import etree
from xml.sax.saxutils import escape
from flask import Flask

# Инициализация Flask приложения для поддержания активности
app = Flask(__name__)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.environ.get('BOT_TOKEN')
UPLOAD_FOLDER = os.path.join('.', 'uploads')  # Путь для Glitch
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Состояния диалога
GET_TITLE = 1
user_files = {}
user_titles = {}

# ================== Обработчики бота ================== #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Привет! Отправляй FB2 файлы, затем используй /merge для объединения. "
        "После объединения можно задать название для книги!"
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    document = update.message.document

    if not (document.file_name.endswith('.fb2') or 
            document.mime_type == 'application/x-fictionbook+xml'):
        await update.message.reply_text("❌ Поддерживаются только FB2 файлы!")
        return

    try:
        file = await document.get_file()
        filename = f"{user.id}_{uuid.uuid4().hex}.fb2"
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        await file.download_to_drive(file_path)

        user_files.setdefault(user.id, []).append(file_path)
        await update.message.reply_text(f"✅ Добавлено файлов: {len(user_files[user.id])}")

    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        await update.message.reply_text("❌ Ошибка обработки файла")

async def merge_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not user_files.get(user.id):
        await update.message.reply_text("❌ Сначала отправьте файлы!")
        return ConversationHandler.END

    await update.message.reply_text(
        "📝 Введите название для объединенной книги:\n"
        "(максимум 100 символов, запрещены символы: \\/*?:\"<>|)"
    )
    return GET_TITLE

async def process_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    raw_title = update.message.text.strip()

    if not raw_title:
        await update.message.reply_text("❌ Название не может быть пустым!")
        return GET_TITLE

    if len(raw_title) > 100:
        await update.message.reply_text("❌ Слишком длинное название (макс. 100 символов)!")
        return GET_TITLE

    clean_title = re.sub(r'[\\/*?:"<>|]', '', raw_title)
    if not clean_title.endswith('.fb2'):
        clean_title += '.fb2'

    user_titles[user.id] = clean_title
    await update.message.reply_text(f"✅ Название сохранено: {clean_title}")
    await process_merge(update, user)
    return ConversationHandler.END

async def process_merge(update: Update, user):
    try:
        merged_images = {}
        image_mapping = {}
        bodies = []
        metadata = []

        for file_path in user_files[user.id]:
            meta = await extract_metadata(file_path)
            content = await process_fb2(file_path)
            metadata.append(meta)
            bodies.append(content['body'])

            for img_id, img_data in content['images'].items():
                new_id = f"img_{uuid.uuid4().hex}"
                image_mapping[img_id] = new_id
                merged_images[new_id] = img_data

        xml_content = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">',
            '  <description>',
            '    <title-info>',
            f'      <book-title>{escape(user_titles[user.id][:-4])}</book-title>',
            '    </title-info>',
            '  </description>',
            '  <body>'
        ]

        for body in bodies:
            for old_id, new_id in image_mapping.items():
                body = body.replace(f"#{old_id}", f"#{new_id}")
            xml_content.append(body)

        xml_content.append('  </body>')

        if merged_images:
            xml_content.append('  <binary>')
            for img_id, img_data in merged_images.items():
                xml_content.append(
                    f'    <binary id="{img_id}" content-type="{img_data["content-type"]}">\n'
                    f'      {img_data["data"]}\n'
                    '    </binary>'
                )
            xml_content.append('  </binary>')

        xml_content.append('</FictionBook>')

        merged_filename = f"merged_{user.id}_{uuid.uuid4().hex}.fb2"
        merged_path = os.path.join(UPLOAD_FOLDER, merged_filename)

        async with aiofiles.open(merged_path, 'w', encoding='utf-8') as f:
            await f.write('\n'.join(xml_content))

        await update.message.reply_document(
            document=open(merged_path, 'rb'),
            filename=user_titles[user.id]
        )

    except Exception as e:
        logger.error(f"Ошибка объединения: {e}")
        await update.message.reply_text("❌ Произошла ошибка при создании книги!")

    finally:
        for file_path in user_files.pop(user.id, []):
            try: os.remove(file_path)
            except: pass
        if 'merged_path' in locals():
            try: os.remove(merged_path)
            except: pass
        user_titles.pop(user.id, None)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_files.pop(user.id, None)
    user_titles.pop(user.id, None)
    await update.message.reply_text("❌ Операция отменена")
    return ConversationHandler.END

# ================== Вспомогательные функции ================== #

async def extract_metadata(file_path):
    try:
        async with aiofiles.open(file_path, 'rb') as f:
            content = await f.read()
            root = etree.fromstring(content, parser=etree.XMLParser(recover=True))
            ns = {'fb': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
            title = root.findtext('.//fb:description/fb:title-info/fb:book-title', namespaces=ns)
            return {'title': escape(title.strip()) if title else "Без названия"}
    except:
        return {'title': "Без названия"}

async def process_fb2(file_path):
    try:
        async with aiofiles.open(file_path, 'rb') as f:
            content = await f.read()
            root = etree.fromstring(content, parser=etree.XMLParser(recover=True))
            ns = {'fb': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
            body = root.find('.//fb:body', namespaces=ns)
            body_content = etree.tostring(body, encoding='unicode') if body else ''
            binaries = root.findall('.//fb:binary', namespaces=ns)
            images = {}
            for binary in binaries:
                if 'image/' in binary.get('content-type', ''):
                    images[binary.get('id')] = {
                        'content-type': binary.get('content-type'),
                        'data': binary.text.strip()
                    }
            return {'body': body_content, 'images': images}
    except:
        return {'body': '', 'images': {}}

# ================== Инициализация приложения ================== #

def setup_bot():
    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('merge', merge_start)],
        states={
            GET_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_title)]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    return application

# ================== Запуск приложения ================== #

@app.route('/')
def home():
    return "🤖 Бот работает! Не закрывайте эту вкладку."

def main():
    bot_app = setup_bot()
    bot_app.run_polling()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
    main()

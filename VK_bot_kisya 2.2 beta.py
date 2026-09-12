import base64
import json          #новое
import random
import re
import sqlite3
import string
import time
import requests
import vk_api
from vk_api.longpoll import VkEventType, VkLongPoll
import os
from dotenv import load_dotenv
import openai
from io import BytesIO


load_dotenv()

# --- ИНИЦИАЛИЗАЦИЯ SQLITE ---
conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute(
    """
  CREATE TABLE IF NOT EXISTS users (
      kod TEXT PRIMARY KEY,
      vk_id TEXT UNIQUE,
      name TEXT,
      activated_at INTEGER,
      limit_days INTEGER
  )
"""
)
conn.commit()

SECONDS_IN_DAY = 86400
DEFAULT_LIMIT_DAYS = 30

# --- НАСТРОЙКИ ---
VK_TOKEN = os.getenv("VK_TOKEN")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
FOLDER_ID = os.getenv("FOLDER_ID")
FOLDER_ID_GENERATE_PHOTO = os.getenv("FOLDER_ID_GENERATE_PHOTO")  # ← НОВАЯ ПЕРЕМЕННАЯ
ADMIN_VK_ID = int(os.getenv("ADMIN_VK_ID"))  # ID — это число, поэтому делаем int
#генерация
# YANDEX_STATIC_KEY_ID = os.getenv("YANDEX_STATIC_KEY_ID")
# YANDEX_STATIC_SECRET = os.getenv("YANDEX_STATIC_SECRET")
YANDEX_ART_API_KEY = os.getenv("YANDEX_ART_API_KEY")

VISION_MODEL = "qwen3.6-35b-a3b"

SYSTEM_PROMPT = (
    "Ты — Кися, дружелюбный и умный помощник. Отвечай кратко, по делу и на русском языке. "
    "Если это задача — реши пошагово простым текстом. "
    "ВАЖНО: Не используй математические формулы в формате LaTeX, TeX или Markdown-разметку. "
    "Пиши формулы обычными символами. Используй знак корня √ вместо sqrt."
)

user_history = {}
MAX_HISTORY_LENGTH = 10

user_modes = {}  # user_id -> "study" | "photo" | None

def get_main_keyboard():
    return {
        "one_time": False,
        "inline": False,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "🎨 Генерация фото",
                        "payload": '{"command": "gen_photo"}',
                    },
                    "color": "positive",
                },
                {
                    "action": {
                        "type": "text",
                        "label": "📚 Режим учебной киси",
                        "payload": '{"command": "study_mode"}',
                    },
                    "color": "primary",
                },
            ]
        ],
    }


# --- ФУНКЦИИ РАБОТЫ С БД ---

def generate_unique_kod():
    while True:
        suffix = "".join(
            random.choices(string.ascii_uppercase + string.digits, k=6)
        )
        new_kod = f"KEY-{suffix}"
        cursor.execute("SELECT kod FROM users WHERE kod = ?", (new_kod,))
        if not cursor.fetchone():
            return new_kod


def get_user_by_vk_id(vk_id):
    try:
        cursor.execute(
            "SELECT kod, vk_id, name, activated_at, limit_days FROM users WHERE vk_id = ?",
            (str(vk_id),),
        )
        row = cursor.fetchone()
        if row:
            return {
                "kod": row[0],
                "vk_id": row[1],
                "name": row[2],
                "activated_at": row[3],
                "limit_days": row[4],
            }
        return None
    except Exception as e:
        print(f"Ошибка чтения БД: {e}")
        return None


def create_user_auto(vk_id, name="Пользователь"):
    try:
        new_kod = generate_unique_kod()
        now = int(time.time())
        cursor.execute(
            "INSERT INTO users (kod, vk_id, name, activated_at, limit_days) VALUES (?, ?, ?, ?, ?)",
            (new_kod, str(vk_id), name, now, DEFAULT_LIMIT_DAYS),
        )
        conn.commit()
        return {
            "kod": new_kod,
            "vk_id": str(vk_id),
            "name": name,
            "activated_at": now,
            "limit_days": DEFAULT_LIMIT_DAYS,
        }
    except Exception as e:
        print(f"Ошибка создания пользователя: {e}")
        return None


# ==========================================================
# ИСПРАВЛЕННАЯ ФУНКЦИЯ check_access
# Теперь возвращает 3 значения: (has_access, days_left, message)
# ==========================================================
def check_access(data):
    if not data:
        return False, 0, "Пользователь не найден."

    activated_at = data.get("activated_at", 0)
    limit_days = data.get("limit_days", DEFAULT_LIMIT_DAYS)
    now = int(time.time())
    total_seconds = limit_days * SECONDS_IN_DAY
    time_diff = now - activated_at

    if time_diff >= total_seconds:
        days_expired = (time_diff - total_seconds) / SECONDS_IN_DAY
        # Возвращаем False, 0 дней (доступ кончился), и сообщение об ошибке
        return False, 0, f"⏰ Срок доступа истёк {days_expired:.0f} дн. назад.Обратитесь к админестратору, ВК:https://vk.ru/alexandr_28, Telegram:@i_pik"

    days_left = int((total_seconds - time_diff) / SECONDS_IN_DAY)
    status_msg = f"✅ Доступ активен! Осталось {days_left} дн."

    return True, days_left, status_msg


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_history_for_user(user_id):
    if user_id not in user_history:
        user_history[user_id] = []
    return user_history[user_id]


def add_to_history(user_id, role, text):
    history = get_history_for_user(user_id)
    history.append({"role": role, "text": text})
    if len(history) > MAX_HISTORY_LENGTH:
        user_history[user_id] = history[-MAX_HISTORY_LENGTH:]


def clean_latex(text):
    if not text:
        return ""
    text = re.sub(r"\\frac\{(.*?)\}\{(.*?)\}", r"\1/\2", text)
    text = re.sub(r"\\sqrt\{(.*?)\}", r"√\1", text)
    text = text.replace("\\cdot", "*").replace("\\times", "*")
    text = text.replace("\\pm", "±")
    text = (
        text.replace("$", "")
        .replace("[", "")
        .replace("]", "")
        .replace("{", "")
        .replace("}", "")
    )
    text = text.replace("\\", "")
    return text


def format_response(raw_text):
    if not raw_text:
        return ""
    lines = [line.strip() for line in raw_text.splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines)


def get_photo_urls(vk_session, event):
    photo_urls = []
    attachments = getattr(event, "attachments", [])

    for att in attachments:
        if isinstance(att, str) and att.startswith("photo"):
            photo_id_str = att[5:]
            try:
                response = vk_session.method(
                    "photos.getById",
                    {"photos": photo_id_str, "photo_sizes": True},
                )
                if response and len(response) > 0:
                    sizes = response[0].get("sizes", [])
                    if sizes:
                        sizes.sort(key=lambda x: x.get("width", 0), reverse=True)
                        url = sizes[0].get("url")
                        if url:
                            photo_urls.append(url)
            except Exception:
                pass

        elif isinstance(att, dict) and att.get("type") == "photo":
            sizes = att.get("photo", {}).get("sizes", [])
            if sizes:
                sizes.sort(key=lambda x: x.get("width", 0), reverse=True)
                url = sizes[0].get("url")
                if url:
                    photo_urls.append(url)

    if not photo_urls and hasattr(event, "message_id"):
        try:
            response = vk_session.method(
                "messages.getById", {"message_ids": event.message_id}
            )
            items = response.get("items", [])
            if items:
                for att in items[0].get("attachments", []):
                    if att.get("type") == "photo":
                        sizes = att.get("photo", {}).get("sizes", [])
                        if sizes:
                            sizes.sort(key=lambda x: x.get("width", 0), reverse=True)
                            url = sizes[0].get("url")
                            if url:
                                photo_urls.append(url)
        except Exception:
            pass

    return photo_urls


def download_image_as_base64(url):
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return base64.b64encode(resp.content).decode("utf-8")
    except Exception as e:
        print(f"Ошибка скачивания: {e}")
    return None


# --- YANDEX API ---

def ask_yandex_text(user_text, user_id):
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
        "x-folder-id": FOLDER_ID,
    }
    history = get_history_for_user(user_id)
    messages = [{"role": "system", "text": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "text": user_text})

    payload = {
        "modelUri": f"gpt://{FOLDER_ID}/yandexgpt/latest",
        "completionOptions": {
            "stream": False,
            "temperature": 0.7,
            "maxTokens": "2048",
        },
        "messages": messages,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(resp.text)
            return f"Ошибка API: {resp.status_code}"

        data = resp.json()
        result = data.get("result", {})
        alternatives = result.get("alternatives", [])

        if not alternatives:
            return "Нет ответа от нейросети."
        first_alt = alternatives[0]
        message = first_alt.get("message", {})
        raw_answer = message.get("text", "")

        cleaned = clean_latex(raw_answer)
        formatted = format_response(cleaned)
        add_to_history(user_id, "assistant", formatted)
        return formatted
    except Exception as e:
        print(e)
        return f"Произошла ошибка: {str(e)}"


def ask_yandex_image(user_text, image_base64_list, user_id):
    url = "https://ai.api.cloud.yandex.net/v1/chat/completions"
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
        "x-folder-id": FOLDER_ID,
    }

    text_for_prompt = (
        user_text if user_text else "На фото задача. Реши её пошагово."
    )
    content = [{"type": "text", "text": text_for_prompt}]

    for img_b64 in image_base64_list:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
        })

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    history = get_history_for_user(user_id)
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["text"]})
    messages.append({"role": "user", "content": content})

    payload = {
        "model": f"gpt://{FOLDER_ID}/{VISION_MODEL}",
        "messages": messages,
        "stream": False,
        "temperature": 0.7,
        "max_tokens": 2048,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code != 200:
            print(resp.text)
            return f"Ошибка API фото: {resp.status_code}"

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return "Не удалось получить ответ."

        first_choice = choices[0]
        message = first_choice.get("message", {})
        raw_answer = message.get("content", "")

        cleaned = clean_latex(raw_answer)
        formatted = format_response(cleaned)
        add_to_history(user_id, "user", text_for_prompt)
        add_to_history(user_id, "assistant", formatted)
        return formatted
    except Exception as e:
        print(e)
        return f"Произошла ошибка при обработке фото: {str(e)}"


def send_msg(user_id, text, keyboard=None):
    try:
        params = {
            "user_id": user_id,
            "message": text,
            "random_id": random.randint(-(2 ** 63), 2 ** 63 - 1),
        }
        if keyboard:
            params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
        vk_session.method("messages.send", params)
    except Exception as e:
        print(f"Ошибка отправки: {e}")


def generate_image_yandex(prompt: str, user_id: int):
    """Генерация картинки через YandexART. Возвращает байты картинки, а не файл."""

    YANDEX_MODEL = "aliceai-image-art-3.0"

    client = openai.OpenAI(
        api_key=YANDEX_API_KEY,
        base_url="https://ai.api.cloud.yandex.net/v1",
        project=FOLDER_ID_GENERATE_PHOTO,
    )

    try:
        response = client.images.generate(
            model=f"art://{FOLDER_ID_GENERATE_PHOTO}/{YANDEX_MODEL}",
            prompt=prompt,
            size="1024x1024",
        )

        if not response.data or len(response.data) == 0:
            return None, "Не смогла сгенерировать 😿 Попробуй другую тему!"

        b64_json = response.data[0].b64_json
        if not b64_json:
            return None, "Не смогла сгенерировать 😿 Попробуй другую тему!"

        image_bytes = base64.b64decode(b64_json)

        if not image_bytes or len(image_bytes) == 0:
            return None, "Картинка получилась пустой 😿 Попробуй ещё раз!"

        return image_bytes, None

    except Exception as e:
        return None, f"Не смогла нарисовать 😿 Попробуй другую тему!"









def send_photo_vk(user_id, image_bytes, caption=""):
    """Отправляет фото из памяти (без сохранения на диск)."""
    for attempt in range(3):
        try:
            # Шаг 1: Получаем URL для загрузки
            upload_server = vk_session.method(
                "photos.getMessagesUploadServer", {"peer_id": user_id}
            )
            upload_url = upload_server["upload_url"]

            # Шаг 2: Загружаем фото напрямую из памяти
            files = {"photo": ("image.png", BytesIO(image_bytes), "image/png")}
            upload_resp = requests.post(upload_url, files=files).json()

            # Проверка: сервер вернул photo?
            if not upload_resp.get("photo") or upload_resp["photo"] == "[]":
                print(f"Попытка {attempt+1}: VK не принял фото, ответ: {upload_resp}")
                time.sleep(1)
                continue

            # Шаг 3: Сохраняем фото в ВК
            photo = vk_session.method("photos.saveMessagesPhoto", upload_resp)

            if not photo or len(photo) == 0:
                print(f"Попытка {attempt+1}: saveMessagesPhoto вернул пустой список")
                time.sleep(1)
                continue

            owner_id = photo[0]["owner_id"]
            photo_id = photo[0]["id"]
            attachment = f"photo{owner_id}_{photo_id}"

            if photo[0].get("access_key"):
                attachment += f"_{photo[0]['access_key']}"

            # Шаг 4: Отправляем сообщение
            vk_session.method("messages.send", {
                "user_id": user_id,
                "message": caption,
                "attachment": attachment,
                "random_id": random.randint(-(2 ** 63), 2 ** 63 - 1),
            })
            return True

        except Exception as e:
            print(f"Попытка {attempt+1} не удалась: {e}")
            time.sleep(1)

    send_msg(user_id, "Не удалось отправить картинку :( Попробуй ещё раз.")
    return False



# --- ЗАПУСК ---
vk_session = vk_api.VkApi(token=VK_TOKEN)
longpoll = VkLongPoll(vk_session)

print("Бот Кися запущен. Жду сообщения...")

for event in longpoll.listen():
    if event.type == VkEventType.MESSAGE_NEW and event.to_me:
        msg_text = event.text.strip() if event.text else ""
        user_id = event.user_id
        msg_lower = msg_text.lower()

        # ======================= КОМANDЫ АДМИНА =======================
        if user_id == ADMIN_VK_ID:
            handled = False

            if msg_lower.startswith("добавить "):
                parts = msg_text.split(maxsplit=2)
                if len(parts) == 3:
                    kod = parts[1]
                    name = parts[2]
                    cursor.execute("SELECT * FROM users WHERE kod = ?", (kod,))
                    if cursor.fetchone():
                        send_msg(user_id, f"Ключ {kod} уже существует!")
                    else:
                        cursor.execute(
                            "INSERT INTO users (kod, vk_id, name, activated_at, limit_days)"
                            " VALUES (?, NULL, ?, ?, ?)",
                            (kod, name, int(time.time()), 30),
                        )
                        conn.commit()
                        send_msg(
                            user_id, f"✅ Создан ключ {kod} для {name}, лимит 30 дней."
                        )
                    handled = True
                else:
                    send_msg(user_id, "Формат: добавить КОД ИМЯ")
                    handled = True


            elif msg_lower.startswith("продлить "):
                parts = msg_text.split()
                if len(parts) == 3 and not handled:
                    kod = parts[1]
                    try:
                        new_days = int(parts[2])
                    except ValueError:
                        send_msg(user_id, "Формат: продлить КОД ДНЕЙ")
                        handled = True
                    else:
                        cursor.execute(
                            "SELECT kod FROM users WHERE kod = ?", (kod,)
                        )
                        if not cursor.fetchone():
                            send_msg(user_id, f"Ключ {kod} не найден.")
                        else:
                            cursor.execute(
                                "UPDATE users SET limit_days = ?, activated_at = ? WHERE kod = ?",
                                (new_days, int(time.time()), kod),
                            )
                            conn.commit()
                            send_msg(user_id, f"✅ Ключу {kod} установлено {new_days} дн. (отсчёт с сейчас).")
                        handled = True
                elif not handled:
                    send_msg(user_id, "Формат: продлить КОД ДНЕЙ")
                    handled = True

            elif msg_lower.startswith("сброс "):
                parts = msg_text.split()
                if len(parts) == 2 and not handled:
                    kod = parts[1]
                    cursor.execute("SELECT kod FROM users WHERE kod = ?", (kod,))
                    if not cursor.fetchone():
                        send_msg(user_id, f"Ключ {kod} не найден.")
                    else:
                        cursor.execute(
                            "UPDATE users SET activated_at = ? WHERE kod = ?",
                            (int(time.time()), kod),
                        )
                        conn.commit()
                        send_msg(user_id, f"✅ Таймер ключа {kod} сброшен.")
                    handled = True
                elif not handled:
                    send_msg(user_id, "Формат: сброс КОД")
                    handled = True

            elif msg_lower == "список" and not handled:
                cursor.execute(
                    "SELECT kod, vk_id, name, activated_at, limit_days FROM users"
                )
                rows = cursor.fetchall()
                now = time.time()
                if not rows:
                    send_msg(user_id, "База пуста.")
                    handled = True
                else:
                    lines = []
                    for row in rows:
                        kod, vk_id, name, activated_at, limit_days = row
                        limit_days = limit_days if limit_days is not None else 30
                        activated_at = activated_at or 0
                        total = limit_days * 86400
                        is_active = (now - activated_at) < total
                        status = "✅" if is_active else "❌"
                        vk_str = vk_id or "—"
                        lines.append(
                            f"{status} {kod} | {name} | VK: {vk_str} | {limit_days}д"
                        )
                    chunk = []
                    chunk_len = 0
                    for line in lines:
                        if chunk_len + len(line) > 3900:
                            send_msg(user_id, "\n".join(chunk))
                            chunk = []
                            chunk_len = 0
                        chunk.append(line)
                        chunk_len += len(line) + 1
                    if chunk:
                        send_msg(user_id, "\n".join(chunk))
                    handled = True

            elif msg_lower.startswith("отвязать ") and not handled:
                parts = msg_text.split()
                if len(parts) == 2:
                    kod = parts[1]
                    cursor.execute("UPDATE users SET vk_id = NULL WHERE kod = ?", (kod,))
                    if cursor.rowcount > 0:
                        conn.commit()
                        send_msg(user_id, f"✅ VK ID отвязан от ключа {kod}.")
                    else:
                        send_msg(user_id, f"Ключ {kod} не найден.")
                    handled = True
                else:
                    send_msg(user_id, "Формат: отвязать КОД")
                    handled = True

            elif msg_lower.startswith("имя ") and not handled:
                parts = msg_text.split(maxsplit=2)
                if len(parts) == 3:
                    kod = parts[1]
                    new_name = parts[2]
                    cursor.execute("SELECT kod FROM users WHERE kod = ?", (kod,))
                    if not cursor.fetchone():
                        send_msg(user_id, f"Ключ {kod} не найден.")
                    else:
                        cursor.execute(
                            "UPDATE users SET name = ? WHERE kod = ?",
                            (new_name, kod),
                        )
                        conn.commit()
                        send_msg(user_id, f"✅ Имя для ключа {kod} изменено на «{new_name}».")
                    handled = True
                else:
                    send_msg(user_id, "Формат: имя КОД НОВОЕ_ИМЯ")
                    handled = True

            elif msg_lower.startswith("обнулить ") and not handled:
                parts = msg_text.split()
                if len(parts) == 2:
                    kod = parts[1]
                    cursor.execute("SELECT kod FROM users WHERE kod = ?", (kod,))
                    if not cursor.fetchone():
                        send_msg(user_id, f"Ключ {kod} не найден.")
                    else:
                        cursor.execute(
                            "UPDATE users SET limit_days = 0 WHERE kod = ?",
                            (kod,),
                        )
                        conn.commit()
                        send_msg(user_id, f"✅ Срок ключа {kod} обнулён (0 дней).")
                    handled = True
                else:
                    send_msg(user_id, "Формат: обнулить КОД")
                    handled = True

            elif msg_lower == "удалить историю" and not handled:
                user_history[user_id] = []
                send_msg(user_id, "✅ Твоя история очищена.")
                handled = True

            if handled:
                continue

        # ======================= ОБРАБОТКА ПОЛЬЗОВАТЕЛЕЙ =======================

        # 1. Поиск или автосоздание пользователя
        user_data = get_user_by_vk_id(user_id)

        if user_data is None:
            user_data = create_user_auto(user_id, name="Новый друг")
            if user_data is None:
                send_msg(
                    user_id,
                    "Кися не смогла тебя зарегистрировать :( Попробуй позже.",
                )
                continue
            kod = user_data["kod"]
            user_history[user_id] = []

            # Сразу считаем оставшиеся дни для нового пользователя
            has_access, days_left, status_msg = check_access(user_data)

            send_msg(
                user_id,
                f"Привет! Я Кися 🐱\n"
                f"Тебе выдан код доступа: {kod}\n"
                f"{status_msg}\n"
                f"Кидай задачу текстом или фото — всё решу!",
            )
            continue

        # 2. Проверка срока доступа (теперь распаковываем 3 значения)
        has_access, days_left, access_msg = check_access(user_data)
        if not has_access:
            send_msg(user_id, access_msg)
            continue

        # 3. Обработка команд пользователя
        if msg_lower in ["начать", "start", "hi", "привет"]:
            user_history[user_id] = []
            name = user_data.get("name", "")
            has_access, days_left, status_msg = check_access(user_data)

            if name and name != "Новый друг":
                greet = f"Кися тут, {name}! 👋\n{status_msg}\n\n"
            else:
                greet = f"Кися тут! 👋\n{status_msg}\n\n"

            greet += "Выбери режим:"
            send_msg(user_id, greet, keyboard=get_main_keyboard())
            continue

        elif msg_lower == "привет, это амина":
            user_history[user_id] = []
            send_msg(user_id, "А, это ты, родная! Кися всё разрулит💋")
            continue

        elif msg_lower == "мой статус":
            has_access, days_left, status_msg = check_access(user_data)
            name = user_data.get("name", "друг")
            kod = user_data.get("kod", "—")
            send_msg(user_id, f"👤 {name}\n🔑 Код: {kod}\n{status_msg}")
            continue

        elif msg_lower in ["очистить", "забудь", "сбросить историю"]:
            user_history[user_id] = []
            send_msg(
                user_id, "✅ История диалога очищена. Начинаем с чистого листа!"
            )
            continue

        # --- ОБРАБОТКА КНОПОК ---
        if msg_lower == "🎨 генерация фото":
            user_modes[user_id] = "photo"
            send_msg(user_id, "Режим генерации фото! 🎨\nОпиши, что нарисовать — и Кися создаст картинку.\nБета версия🧸")
            continue

        elif msg_lower == "📚 режим учебной киси":
            user_modes[user_id] = "study"
            send_msg(user_id, "Режим учебной киси! 📚\nКидай задачу текстом или фото — всё решу!")
            continue

        # Если режим фото — генерация картинки
        if user_modes.get(user_id) == "photo" and msg_text:
            send_msg(user_id, f"🖌️ Кися рисует: «{msg_text}»...\nЭто займёт 5–10 секунд.")

            image_bytes, error = generate_image_yandex(msg_text, user_id)

            if error:
                send_msg(user_id, error)
                continue

            send_photo_vk(user_id, image_bytes)
            continue

        # Обработка фото
        photo_urls = get_photo_urls(vk_session, event)

        if photo_urls:
            send_msg(user_id, "Кися получила фото! Смотрю и решаю... 📸")
            image_base64_list = []
            for url in photo_urls:
                b64 = download_image_as_base64(url)
                if b64:
                    image_base64_list.append(b64)

            if image_base64_list:
                answer = ask_yandex_image(msg_text, image_base64_list, user_id)
                send_msg(user_id, answer)
            else:
                send_msg(user_id, "Не смогла скачать фото. Попробуй ещё раз.")
            continue

        # Пустое сообщение без вложений
        if not msg_text:
            send_msg(user_id, "Напиши текст или пришли фото!")
            continue

        # Текстовый запрос
        add_to_history(user_id, "user", msg_text)
        answer = ask_yandex_text(msg_text, user_id)
        send_msg(user_id, answer)

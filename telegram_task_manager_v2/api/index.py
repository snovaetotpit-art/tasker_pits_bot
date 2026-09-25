import os
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="TG Task Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8919019039:AAGbi8OPdhdcWyqCMjoTLECUKZlYtZOhEqc")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://qkcjkkskebttdlfqziuc.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://tasker-pits-bot.vercel.app")

PENDING_TASKS: Dict[str, Dict[str, Any]] = {}

SYSTEM_PROMPT = """Ты — ассистент руководителя проектов и продюсера.
Анализируй пересланные сообщения из Telegram и формируй задачи для таск-менеджера.

Проекты:
1. "mikhaylov" (Продюсерский центр Михалёва):
   UGC/EGC производство, контент-завод, рилс/shorts, сценарии, креаторы, блогеры, съемки, монтаж, КП, клиенты (бренды/селлеры). Если назван конкретный клиент, запиши его в client_name.
2. "big_shop" (Big Shop):
   Сайты и интернет-магазины для селлеров WB и Ozon для продаж напрямую без комиссий маркетплейсов. Интеграции, каталог, чекаут, платежи.
3. "keyb" (KeyB):
   Сервис и платформа поиска/аренды жилья. База объектов, букинг, фильтры, арендаторы.
4. "other": Общие/личные задачи.

Правила:
- title: Четкий заголовок с глаголом в инфинитиве (до 7 слов).
- description: Краткая суть задачи без лишней переписки.
- pipeline: Список из 3-5 конкретных подзадач (шагов чек-листа).
- deadline_iso: Точная дата дедлайна в ISO 8601 (или null, если срок не назван).
- priority: "urgent" | "high" | "medium" | "low".
"""

def parse_with_gemini_rest(raw_text: str, sender_name: Optional[str] = None) -> Dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"{SYSTEM_PROMPT}\n\nТекущее время: {datetime.now().isoformat()}\nПостановщик: {sender_name}\n\nСообщение:\n{raw_text}\n\nВерни ТОЛЬКО валидный JSON со структурой: project_slug, client_name, title, description, pipeline, deadline_iso, priority, reasoning."
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2
        }
    }
    resp = requests.post(url, json=payload, timeout=20)
    data = resp.json()
    candidate_text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(candidate_text)

def send_tg_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=10)
        logger.info(f"Telegram response: {r.status_code} - {r.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")

@app.get("/")
def home():
    # Отдаем HTML Mini App напрямую при открытии сайта
    try:
        html_path = os.path.join(os.path.dirname(__file__), "..", "public", "index.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
    except Exception as e:
        logger.error(f"Error reading index.html: {e}")
    return HTMLResponse(content="<h1>Task Tracker Mini App is running!</h1><p>Open via Telegram.</p>")

@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return {"ok": False}

    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        data = cq.get("data", "")
        
        if data.startswith("save:"):
            tid = data.replace("save:", "")
            task_data = PENDING_TASKS.get(tid)
            if task_data:
                # Сохранение через прямой REST API Supabase
                try:
                    headers = {
                        "apikey": SUPABASE_KEY,
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "Content-Type": "application/json",
                        "Prefer": "return=representation"
                    }
                    # Получаем ID проекта
                    proj_slug = task_data.get("project_slug", "other")
                    p_resp = requests.get(f"{SUPABASE_URL}/rest/v1/projects?slug=eq.{proj_slug}&select=id", headers=headers, timeout=10)
                    p_data = p_resp.json()
                    project_id = p_data[0]["id"] if p_data else "00000000-0000-0000-0000-000000000001"

                    # Вставляем задачу
                    insert_payload = {
                        "workspace_id": "00000000-0000-0000-0000-000000000001",
                        "project_id": project_id,
                        "title": task_data["title"],
                        "description": task_data.get("description", ""),
                        "raw_message": task_data.get("raw_message", ""),
                        "requester_name": task_data.get("sender", ""),
                        "priority": task_data.get("priority", "medium"),
                        "status": "todo",
                        "deadline": task_data.get("deadline_iso")
                    }
                    t_resp = requests.post(f"{SUPABASE_URL}/rest/v1/tasks", json=insert_payload, headers=headers, timeout=10)
                    t_data = t_resp.json()
                    
                    if t_data and isinstance(t_data, list):
                        task_db_id = t_data[0]["id"]
                        subtasks_payload = [
                            {"task_id": task_db_id, "title": s, "order_index": i}
                            for i, s in enumerate(task_data.get("pipeline", []))
                        ]
                        if subtasks_payload:
                            requests.post(f"{SUPABASE_URL}/rest/v1/subtasks", json=subtasks_payload, headers=headers, timeout=10)
                except Exception as db_err:
                    logger.error(f"Supabase REST error: {db_err}")

                send_tg_message(chat_id, f"🎉 Задача **«{task_data['title']}»** сохранена в трекер!")
                PENDING_TASKS.pop(tid, None)
            else:
                send_tg_message(chat_id, "⚠️ Данные задачи устарели. Перешлите сообщение снова.")
            return {"ok": True}

        if data.startswith("reassign:"):
            tid = data.replace("reassign:", "")
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🟣 ПЦ Михалёва", "callback_data": f"set_p:{tid}:mikhaylov"}],
                    [{"text": "🟡 Big Shop", "callback_data": f"set_p:{tid}:big_shop"}],
                    [{"text": "🟢 KeyB", "callback_data": f"set_p:{tid}:keyb"}],
                    [{"text": "⚪ Общее", "callback_data": f"set_p:{tid}:other"}]
                ]
            }
            send_tg_message(chat_id, "Выберите проект:", keyboard)
            return {"ok": True}

        if data.startswith("set_p:"):
            _, tid, new_slug = data.split(":")
            if tid in PENDING_TASKS:
                PENDING_TASKS[tid]["project_slug"] = new_slug
                proj_names = {"mikhaylov": "🟣 ПЦ Михалёва", "big_shop": "🟡 Big Shop", "keyb": "🟢 KeyB", "other": "⚪ Общее"}
                send_tg_message(chat_id, f"✅ Проект изменен на: **{proj_names.get(new_slug, new_slug)}**", {
                    "inline_keyboard": [[{"text": "💾 Сохранить", "callback_data": f"save:{tid}"}]]
                })
            return {"ok": True}

        return {"ok": True}

    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text") or msg.get("caption") or ""

        if text.startswith("/start"):
            welcome = (
                "👋 Привет! Я твой таск-менеджер для 3 проектов:\n"
                "• 🟣 **ПЦ Михалёва** (UGC/EGC контент, клиенты)\n"
                "• 🟡 **Big Shop** (сайты для селлеров WB/Ozon)\n"
                "• 🟢 **KeyB** (сервис поиска жилья)\n\n"
                "📥 **Как ставить задачи:** просто пересылай мне сообщения из любых чатов."
            )
            app_url = MINI_APP_URL or "https://tasker-pits-bot.vercel.app"
            markup = {"inline_keyboard": [[{"text": "🚀 Открыть трекер", "web_app": {"url": app_url}}]]}
            send_tg_message(chat_id, welcome, markup)
            return {"ok": True}

        # Определение автора из пересланного сообщения
        sender = "Личная заметка"
        origin = msg.get("forward_origin", {})
        if origin.get("type") == "user":
            u = origin.get("sender_user", {})
            sender = f"@{u.get('username')}" if u.get('username') else u.get('first_name', 'Коллега')
        elif origin.get("type") == "hidden_user":
            sender = origin.get("sender_user_name", "Коллега")
        elif "forward_from" in msg:
            ff = msg["forward_from"]
            sender = f"@{ff.get('username')}" if ff.get('username') else ff.get('first_name', 'Коллега')

        send_tg_message(chat_id, "🤖 Анализирую сообщение через Gemini...")
        try:
            parsed = parse_with_gemini_rest(text, sender)
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            # Базовый парсинг при сбое AI
            parsed = {
                "project_slug": "mikhaylov" if "михалев" in text.lower() or "ugc" in text.lower() else "other",
                "client_name": None,
                "title": (text[:40] + "...") if len(text) > 40 else text,
                "description": text,
                "pipeline": ["Уточнить требования", "Выполнить задачу", "Проверить результат"],
                "deadline_iso": None,
                "priority": "medium"
            }

        task_id = f"t_{msg['message_id']}"
        PENDING_TASKS[task_id] = {**parsed, "raw_message": text, "sender": sender}

        proj_names = {"mikhaylov": "🟣 ПЦ Михалёва", "big_shop": "🟡 Big Shop", "keyb": "🟢 KeyB", "other": "⚪ Общее"}
        p_name = proj_names.get(parsed.get("project_slug", "other"), "⚪ Общее")
        if parsed.get("client_name"):
            p_name += f" (Клиент: {parsed['client_name']})"

        steps = "\n".join([f"  ▫️ {s}" for s in parsed.get("pipeline", [])])
        card = (
            f"📌 **{parsed['title']}**\n"
            f"📁 **Проект**: {p_name}\n"
            f"👤 **Постановщик**: {sender}\n"
            f"⏰ **Дедлайн**: {parsed.get('deadline_iso') or 'Не указан'}\n\n"
            f"📝 **Пайплайн шагов:**\n{steps}"
        )
        app_url = MINI_APP_URL or "https://tasker-pits-bot.vercel.app"
        keyboard = {
            "inline_keyboard": [
                [{"text": "💾 Сохранить", "callback_data": f"save:{task_id}"},
                 {"text": "📁 Сменить проект", "callback_data": f"reassign:{task_id}"}],
                [{"text": "🚀 Открыть в App", "web_app": {"url": app_url}}]
            ]
        }
        send_tg_message(chat_id, card, keyboard)

    return {"ok": True}

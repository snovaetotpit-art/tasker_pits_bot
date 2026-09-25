import os
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any
import requests
from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8919019039:AAGbi8OPdhdcWyqCMjoTLECUKZlYtZOhEqc")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://qkcjkkskebttdlfqziuc.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://telegramtaskmanagerv2.vercel.app")

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
    prompt = f"{SYSTEM_PROMPT}\n\nТекущее время: {datetime.now().isoformat()}\nПостановщик: {sender_name}\n\nСообщение:\n{raw_text}\n\nВерни ТОЛЬКО валидный JSON: {{\"project_slug\": \"...\", \"client_name\": null, \"title\": \"...\", \"description\": \"...\", \"pipeline\": [\"шаг 1\", \"шаг 2\"], \"deadline_iso\": null, \"priority\": \"medium\"}}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}
    }
    resp = requests.post(url, json=payload, timeout=20)
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)

def send_tg_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=10)
        logger.info(f"TG Status: {r.status_code}, Body: {r.text}")
    except Exception as e:
        logger.error(f"TG send error: {e}")

@app.get("/health")
@app.get("/api/health")
@app.get("/webhook")
@app.get("/api/webhook")
def health():
    return {"status": "ok", "service": "Telegram Webhook Active", "time": datetime.now().isoformat()}

@app.post("/webhook")
@app.post("/api/webhook")
@app.post("/")
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
            send_tg_message(chat_id, "🎉 Задача успешно сохранена в трекер!")
            return {"ok": True}

        if data.startswith("reassign:"):
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🟣 ПЦ Михалёва", "callback_data": "set:mikhaylov"}],
                    [{"text": "🟡 Big Shop", "callback_data": "set:big_shop"}],
                    [{"text": "🟢 KeyB", "callback_data": "set:keyb"}]
                ]
            }
            send_tg_message(chat_id, "Выберите проект:", keyboard)
            return {"ok": True}

        if data.startswith("set:"):
            new_slug = data.replace("set:", "")
            proj_names = {"mikhaylov": "🟣 ПЦ Михалёва", "big_shop": "🟡 Big Shop", "keyb": "🟢 KeyB"}
            send_tg_message(chat_id, f"✅ Проект изменен на: **{proj_names.get(new_slug, new_slug)}**\nНажмите «Сохранить».", {
                "inline_keyboard": [[{"text": "💾 Сохранить", "callback_data": "save:direct"}]]
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
                "📥 **Как ставить задачи:** просто пересылай мне сообщения из любых рабочих чатов."
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "🚀 Открыть трекер", "web_app": {"url": "https://telegramtaskmanagerv2.vercel.app"}}]
                ]
            }
            send_tg_message(chat_id, welcome, markup)
            return {"ok": True}

        # Извлечение автора пересылки
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

        parsed = {}
        try:
            parsed = parse_with_gemini_rest(text, sender)
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            t_lower = text.lower()
            proj = "mikhaylov" if ("михалев" in t_lower or "ugc" in t_lower) else ("big_shop" if "wb" in t_lower or "ozon" in t_lower else "keyb" if "жилье" in t_lower else "other")
            parsed = {
                "project_slug": proj,
                "client_name": None,
                "title": (text[:45] + "...") if len(text) > 45 else text,
                "pipeline": ["Уточнить требования", "Выполнить задачу", "Проверить результат"],
                "deadline_iso": None,
                "priority": "medium"
            }

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
        keyboard = {
            "inline_keyboard": [
                [{"text": "💾 Сохранить", "callback_data": f"save:{msg['message_id']}"},
                 {"text": "📁 Сменить проект", "callback_data": f"reassign:{msg['message_id']}"}],
                [{"text": "🚀 Открыть в App", "web_app": {"url": "https://telegramtaskmanagerv2.vercel.app"}}]
            ]
        }
        send_tg_message(chat_id, card, keyboard)

    return {"ok": True}

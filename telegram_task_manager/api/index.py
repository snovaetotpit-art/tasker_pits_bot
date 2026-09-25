import os
import io
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
import requests
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "")

def get_supabase() -> Optional[Client]:
    if SUPABASE_URL and SUPABASE_KEY:
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    return None

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

TASK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "project_slug": {"type": "STRING", "enum": ["mikhaylov", "big_shop", "keyb", "other"]},
        "client_name": {"type": "STRING", "nullable": True},
        "title": {"type": "STRING"},
        "description": {"type": "STRING"},
        "pipeline": {"type": "ARRAY", "items": {"type": "STRING"}},
        "deadline_iso": {"type": "STRING", "nullable": True},
        "priority": {"type": "STRING", "enum": ["urgent", "high", "medium", "low"]},
        "reasoning": {"type": "STRING"}
    },
    "required": ["project_slug", "title", "description", "pipeline", "priority"]
}

def parse_with_gemini(raw_text: str, sender_name: Optional[str] = None) -> Dict[str, Any]:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"Текущее время: {datetime.now().isoformat()}\nПостановщик: {sender_name}\n\nСообщение:\n{raw_text}"
    response = client.models.generate_content(
        model='gemini-1.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=TASK_SCHEMA,
            temperature=0.2
        )
    )
    return json.loads(response.text)

def send_tg_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload, timeout=10)

def send_tg_document(chat_id: int, filename: str, file_bytes: bytes, caption: str = ""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    files = {"document": (filename, file_bytes)}
    data = {"chat_id": chat_id, "caption": caption}
    requests.post(url, data=data, files=files, timeout=30)

@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}

@app.get("/api/tasks")
def list_tasks():
    sp = get_supabase()
    if not sp:
        return []
    res = sp.table("tasks").select("*, projects(name, slug), subtasks(*)").order("created_at", desc=True).execute()
    return res.data or []

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()

    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        data = cq.get("data", "")
        
        if data.startswith("save:"):
            tid = data.replace("save:", "")
            task_data = PENDING_TASKS.get(tid)
            if task_data:
                sp = get_supabase()
                if sp:
                    # Находим ID проекта
                    proj_slug = task_data.get("project_slug", "other")
                    proj_res = sp.table("projects").select("id").eq("slug", proj_slug).limit(1).execute()
                    project_id = proj_res.data[0]["id"] if proj_res.data else None
                    
                    # Получаем дефолтный воркспейс
                    ws_res = sp.table("workspaces").select("id").limit(1).execute()
                    ws_id = ws_res.data[0]["id"] if ws_res.data else None

                    if ws_id and project_id:
                        insert_task = {
                            "workspace_id": ws_id,
                            "project_id": project_id,
                            "title": task_data["title"],
                            "description": task_data.get("description", ""),
                            "raw_message": task_data.get("raw_message", ""),
                            "requester_name": task_data.get("sender", ""),
                            "priority": task_data.get("priority", "medium"),
                            "status": "todo",
                            "deadline": task_data.get("deadline_iso")
                        }
                        t_res = sp.table("tasks").insert(insert_task).execute()
                        if t_res.data:
                            new_task_id = t_res.data[0]["id"]
                            subtasks_to_insert = [
                                {"task_id": new_task_id, "title": step, "order_index": idx}
                                for idx, step in enumerate(task_data.get("pipeline", []))
                            ]
                            if subtasks_to_insert:
                                sp.table("subtasks").insert(subtasks_to_insert).execute()
                
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
                "Просто пересылай мне сообщения с задачами из рабочих чатов."
            )
            markup = {"inline_keyboard": [[{"text": "🚀 Открыть трекер", "web_app": {"url": MINI_APP_URL}}]]}
            send_tg_message(chat_id, welcome, markup)
            return {"ok": True}

        # Извлечение автора пересланного сообщения
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
            parsed = parse_with_gemini(text, sender)
        except Exception as e:
            send_tg_message(chat_id, f"❌ Ошибка распознавания: {str(e)}")
            return {"ok": True}

        task_id = f"t_{msg['message_id']}"
        PENDING_TASKS[task_id] = {**parsed, "raw_message": text, "sender": sender}

        proj_names = {"mikhaylov": "🟣 ПЦ Михалёва", "big_shop": "🟡 Big Shop", "keyb": "🟢 KeyB", "other": "⚪ Общее"}
        p_name = proj_names.get(parsed["project_slug"], "⚪ Общее")
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
                [{"text": "💾 Сохранить", "callback_data": f"save:{task_id}"},
                 {"text": "📁 Сменить проект", "callback_data": f"reassign:{task_id}"}],
                [{"text": "🚀 Открыть в App", "web_app": {"url": MINI_APP_URL}}]
            ]
        }
        send_tg_message(chat_id, card, keyboard)

    return {"ok": True}

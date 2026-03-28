import os
import json
import httpx
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ── CONFIG ──────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
USER_ID        = os.environ["BJJ_USER_ID"]  # tu user_id de localStorage
ALLOWED_CHAT   = os.environ.get("ALLOWED_CHAT_ID", "")  # opcional: tu chat_id de Telegram

# ── SUPABASE ─────────────────────────────────────────────
async def supabase_upsert(table: str, data: dict):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json=data)
        return r.status_code

async def supabase_ensure_profile():
    url = f"{SUPABASE_URL}/rest/v1/profiles?user_id=eq.{USER_ID}&select=user_id"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        if r.json() == []:
            await supabase_upsert("profiles", {"user_id": USER_ID, "name": "Carlos"})

# ── CLAUDE ───────────────────────────────────────────────
SYSTEM_PROMPT = """Eres el asistente de BJJ Journey. Tu tarea es parsear mensajes en lenguaje natural sobre sesiones de BJJ y devolver un JSON estructurado.

REGLAS:
- Responde SIEMPRE y SOLO con un JSON válido, sin texto adicional, sin markdown, sin backticks.
- Si el mensaje no parece una sesión de BJJ, devuelve {"error": "no_session"}
- La fecha por defecto es hoy si no se menciona.
- duration en minutos (si dicen "hora y media" = 90, "2 horas" = 120, "45 min" = 45)
- type: "Gi", "NoGi", "Open mat", "Gym", o "Competición"
- feeling: 5=En llamas🔥, 4=Fuerte💪, 3=Normal😐, 2=Cansado😴, 1=Roto🤕 (null si no se menciona)
- position: una de estas opciones o null: "De pie", "Guardia cerrada", "Guardia abierta", "Half guard", "Side control", "Montada", "Espalda", "General / Sparring"
- notes: resumen breve de lo que mencionen (máx 100 chars)

FORMATO DE RESPUESTA:
{"date": "YYYY-MM-DD", "type": "Gi", "duration": 90, "feeling": 4, "position": "Guardia cerrada", "notes": "..."}

EJEMPLOS:
"hoy 90 min gi, trabajé guardia cerrada, me sentí fuerte" → {"date": "2026-03-28", "type": "Gi", "duration": 90, "feeling": 4, "position": "Guardia cerrada", "notes": "Guardia cerrada"}
"nogi 1 hora, estaba roto" → {"date": "2026-03-28", "type": "NoGi", "duration": 60, "feeling": 1, "position": null, "notes": "Sesión dura"}
"open mat domingo 2h, varios rounds" → {"date": "2026-03-28", "type": "Open mat", "duration": 120, "feeling": null, "position": "General / Sparring", "notes": "Varios rounds"}
"qué tiempo hace" → {"error": "no_session"}"""

async def parse_with_claude(message: str, today: str) -> dict:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"Hoy es {today}. Mensaje: {message}"}]
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=body)
        if r.status_code != 200:
            raise Exception(f"Claude API error {r.status_code}: {r.text[:200]}")
        data = r.json()
        if "content" not in data or not data["content"]:
            raise Exception(f"Claude respuesta vacía: {str(data)[:200]}")
        raw = data["content"][0]["text"].strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise Exception(f"Claude no devolvió JSON válido: {raw[:200]}")

# ── HANDLERS ─────────────────────────────────────────────
FEELING_EMOJI = {5: "🔥 En llamas", 4: "💪 Fuerte", 3: "😐 Normal", 2: "😴 Cansado", 1: "🤕 Roto"}

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola! Soy tu asistente de BJJ Journey.\n\n"
        "Cuéntame tu sesión en lenguaje natural y la guardo automáticamente.\n\n"
        "Ejemplos:\n"
        "• _Hoy 90 min gi, guardia cerrada, me sentí fuerte_\n"
        "• _NoGi 1 hora, estaba roto_\n"
        "• _Open mat 2h, varios rounds_\n\n"
        "También puedes escribir /ayuda para ver más opciones.",
        parse_mode="Markdown"
    )

async def ayuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Comandos disponibles:*\n\n"
        "/start — Bienvenida\n"
        "/ayuda — Esta ayuda\n\n"
        "O simplemente cuéntame tu sesión:\n"
        "• Tipo: gi, nogi, open mat, gym, competición\n"
        "• Duración: 90 min, 1 hora, hora y media...\n"
        "• Feeling: estaba roto, me sentí fuerte, normal...\n"
        "• Posición: guardia cerrada, side control...\n"
        "• Notas: lo que quieras añadir",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Seguridad: solo tu chat
    if ALLOWED_CHAT and str(update.effective_chat.id) != ALLOWED_CHAT:
        return

    msg = update.message.text
    today = datetime.now().strftime("%Y-%m-%d")

    # Indicador de escritura
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")

    try:
        parsed = await parse_with_claude(msg, today)
    except Exception as e:
        await update.message.reply_text(f"❌ Error al procesar: {str(e)}")
        return

    if "error" in parsed:
        await update.message.reply_text(
            "🤔 No he entendido eso como una sesión de BJJ.\n"
            "Prueba algo como: _Hoy 90 min gi, guardia cerrada_",
            parse_mode="Markdown"
        )
        return

    # Guardar en Supabase
    import uuid
    session_id = f"{USER_ID}_{parsed['date']}_{uuid.uuid4().hex[:8]}"
    session_data = {
        "id": session_id,
        "user_id": USER_ID,
        "date": parsed["date"],
        "type": parsed.get("type", "Gi"),
        "duration": parsed.get("duration", 60),
        "feeling": parsed.get("feeling"),
        "position": parsed.get("position"),
        "notes": parsed.get("notes", ""),
        "from_gcal": False
    }

    await supabase_ensure_profile()
    status = await supabase_upsert("sessions", session_data)

    if status in (200, 201):
        feeling_str = f" · {FEELING_EMOJI[parsed['feeling']]}" if parsed.get("feeling") else ""
        position_str = f" · {parsed['position']}" if parsed.get("position") else ""
        duration_h = round(parsed.get("duration", 60) / 60 * 10) / 10

        await update.message.reply_text(
            f"✅ *Sesión guardada*\n\n"
            f"📅 {parsed['date']}\n"
            f"🥋 {parsed['type']} · {duration_h}h{feeling_str}{position_str}\n"
            f"{f'📝 {parsed[chr(110)+chr(111)+chr(116)+chr(101)+chr(115)]}' if parsed.get('notes') else ''}\n\n"
            f"_Abre la app para verla en tu historial_",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"❌ Error al guardar en la base de datos (status {status})")

# ── MAIN ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 BJJ Journey Bot arrancado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

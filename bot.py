import os
import json
import httpx
import uuid
from datetime import datetime, date
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ── CONFIG ──────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
USER_ID        = os.environ["BJJ_USER_ID"]
ALLOWED_CHAT   = os.environ.get("ALLOWED_CHAT_ID", "")

# ── MEMORIA DE CONVERSACIÓN ──────────────────────────────
# Guarda los últimos mensajes por chat_id
conversation_history = {}
MAX_HISTORY = 8  # últimos 4 intercambios

# ── SUPABASE ─────────────────────────────────────────────
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates"
}

async def sb_get(table: str, filters: str = "") -> list:
    url = f"{SUPABASE_URL}/rest/v1/{table}?user_id=eq.{USER_ID}{filters}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=SB_HEADERS)
        return r.json() if r.status_code == 200 else []

async def sb_upsert(table: str, data) -> int:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=SB_HEADERS, json=data)
        return r.status_code

async def sb_ensure_profile():
    existing = await sb_get("profiles")
    if not existing:
        await sb_upsert("profiles", {"user_id": USER_ID, "name": "Carlos"})

# ── CONTEXTO DEL USUARIO ────────────────────────────────
async def load_user_context() -> str:
    sessions = await sb_get("sessions", "&order=date.desc&limit=50")
    techniques = await sb_get("techniques", "&seen=eq.true")

    today = date.today()
    this_month = [s for s in sessions if s.get("date","")[:7] == today.strftime("%Y-%m")]
    total_h = round(sum(s.get("duration",0) or 0 for s in sessions) / 60, 1)
    month_h = round(sum(s.get("duration",0) or 0 for s in this_month) / 60, 1)

    recent = sessions[:5]
    recent_str = "\n".join([
        f"  - {s['date']} | {s.get('type','?')} | {round((s.get('duration',0) or 0)/60,1)}h"
        + (f" | {s.get('position','')}" if s.get('position') else "")
        + (f" | feeling {s.get('feeling','')}/5" if s.get('feeling') else "")
        + (f" | {s.get('notes','')[:50]}" if s.get('notes') else "")
        for s in recent
    ]) or "  (sin sesiones aún)"

    seen_count = len(techniques)

    return f"""Hoy es {today.strftime('%A %d de %B de %Y')}.

STATS DE CARLOS:
- Total: {total_h}h en {len(sessions)} sesiones
- Este mes: {month_h}h en {len(this_month)} sesiones
- Técnicas vistas en clase: {seen_count}

ÚLTIMAS SESIONES:
{recent_str}"""

# ── CLAUDE ───────────────────────────────────────────────
SYSTEM_PROMPT = """Eres el asistente personal de BJJ de Carlos, un practicante de cinturón blanco que entrena en Kalmma Fight Club en Madrid. Eres como un compañero de entrenamiento inteligente — cercano, directo, con criterio de BJJ real.

TU PERSONALIDAD:
- Hablas en español, de tú, tono natural y cercano
- Tienes conocimiento real de BJJ: posiciones, técnicas, estrategia
- Eres proactivo: si falta info para guardar una sesión, preguntas
- Eres gracioso pero no payaso
- Si algo no es de BJJ, lo rechazas con humor y rediriges

TUS CAPACIDADES:
1. Guardar sesiones de entrenamiento
2. Responder preguntas sobre el progreso de Carlos
3. Dar consejos de BJJ basados en su historial
4. Charlar sobre BJJ en general

CÓMO GUARDAR UNA SESIÓN:
Cuando Carlos te cuente un entreno, extrae: fecha, tipo (Gi/NoGi/Open mat/Gym/Competición), duración en minutos, feeling (1-5), posición trabajada, notas.
- Si falta la duración, PREGUNTA antes de guardar
- Si falta el feeling o posición, puedes inferirlos o dejarlos vacíos
- Cuando tengas todo, incluye en tu respuesta un bloque JSON así (al final, invisible para el usuario):
  SAVE_SESSION:{"date":"YYYY-MM-DD","type":"Gi","duration":90,"feeling":4,"position":"Guardia cerrada","notes":"..."}

FEELING: 1=🤕Roto, 2=😴Cansado, 3=😐Normal, 4=💪Fuerte, 5=🔥En llamas

POSICIONES: "De pie", "Guardia cerrada", "Guardia abierta", "Half guard", "Side control", "Montada", "Espalda", "General / Sparring"

EJEMPLOS:
- "hoy gi 90 min, guardia cerrada, muy bien" → respuesta natural + SAVE_SESSION al final
- "jueves hice open mat, 12 rolls" → preguntar duración aproximada
- "cuántas horas llevo" → responder con los stats
- "qué tiempo hace" → "Solo hablo de BJJ 🥋 para el tiempo mejor Meteored"
- "dame consejos para mejorar mi guardia" → consejo real basado en su historial

IMPORTANTE: El bloque SAVE_SESSION solo aparece cuando tengas TODOS los datos necesarios (mínimo fecha, tipo y duración). Nunca inventes una duración."""

async def call_claude(messages: list, context: str) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    # Inyectar contexto en el primer mensaje del sistema
    system = f"{SYSTEM_PROMPT}\n\nCONTEXTO ACTUAL DE CARLOS:\n{context}"

    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 600,
        "system": system,
        "messages": messages
    }
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(url, headers=headers, json=body)
        if r.status_code != 200:
            raise Exception(f"Claude API error {r.status_code}: {r.text[:200]}")
        return r.json()["content"][0]["text"].strip()

def extract_session(text: str):
    """Extrae el bloque SAVE_SESSION del texto si existe."""
    marker = "SAVE_SESSION:"
    if marker not in text:
        return None, text
    parts = text.split(marker)
    clean_text = parts[0].strip()
    try:
        # Tomar solo la primera línea del JSON
        json_str = parts[1].strip().split("\n")[0]
        session = json.loads(json_str)
        return session, clean_text
    except:
        return None, text

# ── HANDLERS ─────────────────────────────────────────────
FEELING_EMOJI = {5:"🔥",4:"💪",3:"😐",2:"😴",1:"🤕"}

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    conversation_history[chat_id] = []
    await update.message.reply_text(
        "👋 ¡Hola Carlos! Soy tu asistente de BJJ Journey.\n\n"
        "Cuéntame cómo fue el entreno, pregúntame por tu progreso, o pídeme consejo. Estoy aquí para lo que necesites relacionado con el tatami 🥋\n\n"
        "Ejemplos:\n"
        "• _Hoy gi 90 min, trabajé guardia cerrada, me sentí fuerte_\n"
        "• _¿Cuántas horas llevo este mes?_\n"
        "• _Dame consejos para mejorar desde guard bottom_",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_CHAT and str(update.effective_chat.id) != ALLOWED_CHAT:
        return

    chat_id = str(update.effective_chat.id)
    msg = update.message.text

    # Inicializar historial si no existe
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    # Añadir mensaje del usuario al historial
    conversation_history[chat_id].append({"role": "user", "content": msg})

    # Mantener solo los últimos MAX_HISTORY mensajes
    if len(conversation_history[chat_id]) > MAX_HISTORY:
        conversation_history[chat_id] = conversation_history[chat_id][-MAX_HISTORY:]

    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")

    # Cargar contexto
    context = await load_user_context()

    try:
        response = await call_claude(conversation_history[chat_id], context)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:150]}")
        return

    # Extraer sesión si la hay
    session_data, clean_response = extract_session(response)

    # Guardar sesión en Supabase si existe
    if session_data:
        try:
            session_data["id"] = f"{USER_ID}_{session_data.get('date','x')}_{uuid.uuid4().hex[:8]}"
            session_data["user_id"] = USER_ID
            session_data.setdefault("from_gcal", False)
            await sb_ensure_profile()
            await sb_upsert("sessions", session_data)
        except Exception as e:
            clean_response += f"\n\n⚠️ Error al guardar: {str(e)[:100]}"

    # Añadir respuesta al historial
    conversation_history[chat_id].append({"role": "assistant", "content": response})

    # Enviar respuesta limpia al usuario
    await update.message.reply_text(clean_response or "👊", parse_mode="Markdown")

async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    conversation_history[chat_id] = []
    await update.message.reply_text("🔄 Conversación reiniciada. ¿Qué tal el entreno?")

# ── MAIN ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 BJJ Journey Bot v3 arrancado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

import os
import json
import httpx
import asyncio
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

async def sb_upsert(table: str, data: dict) -> int:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=SB_HEADERS, json=data)
        return r.status_code

async def sb_ensure_profile():
    existing = await sb_get("profiles")
    if not existing:
        await sb_upsert("profiles", {"user_id": USER_ID, "name": "Carlos"})

# ── LOAD USER CONTEXT ────────────────────────────────────
async def load_user_context() -> str:
    """Carga el historial del usuario de Supabase para dárselo a Claude como contexto."""
    sessions = await sb_get("sessions", "&order=date.desc&limit=50")
    techniques = await sb_get("techniques", "&seen=eq.true")

    if not sessions:
        return "El usuario no tiene sesiones registradas todavía."

    # Calcular stats
    today = date.today()
    this_month = [s for s in sessions if s.get("date", "")[:7] == today.strftime("%Y-%m")]
    total_min = sum(s.get("duration", 0) or 0 for s in sessions)
    month_min = sum(s.get("duration", 0) or 0 for s in this_month)
    total_h = round(total_min / 60, 1)
    month_h = round(month_min / 60, 1)

    # Últimas 5 sesiones
    recent = sessions[:5]
    recent_str = "\n".join([
        f"  - {s['date']} | {s.get('type','?')} | {round((s.get('duration',0) or 0)/60,1)}h"
        + (f" | {s.get('position','')}" if s.get('position') else "")
        + (f" | feeling {s.get('feeling','')}" if s.get('feeling') else "")
        + (f" | {s.get('notes','')}" if s.get('notes') else "")
        for s in recent
    ])

    # Técnicas vistas
    seen_count = len(techniques)
    seen_names = [t.get("technique_id","").replace("tech_","").replace("_"," ") for t in techniques[:10]]

    context = f"""DATOS DEL USUARIO (Carlos):
- Total horas entrenadas: {total_h}h ({len(sessions)} sesiones)
- Horas este mes: {month_h}h ({len(this_month)} sesiones)
- Técnicas vistas en clase: {seen_count}

ÚLTIMAS 5 SESIONES:
{recent_str}

ALGUNAS TÉCNICAS VISTAS: {', '.join(seen_names) if seen_names else 'ninguna aún'}
Hoy es: {today.strftime('%Y-%m-%d')} ({today.strftime('%A')})"""

    return context

# ── CLAUDE ───────────────────────────────────────────────
SYSTEM_PROMPT = """Eres el asistente personal de BJJ Journey de Carlos, un practicante de BJJ en Madrid (Kalmma Fight Club), cinturón blanco.

Tienes dos modos de operación:

MODO 1 — GUARDAR SESIÓN:
Si el mensaje parece una sesión de BJJ, devuelve EXACTAMENTE este JSON (sin texto extra, sin backticks):
{"action": "save_session", "date": "YYYY-MM-DD", "type": "Gi|NoGi|Open mat|Gym|Competición", "duration": 90, "feeling": 4, "position": "Guardia cerrada|De pie|Guardia abierta|Half guard|Side control|Montada|Espalda|General / Sparring|null", "notes": "..."}

Feeling: 5=🔥En llamas, 4=💪Fuerte, 3=😐Normal, 2=😴Cansado, 1=🤕Roto (null si no se menciona)

MODO 2 — RESPONDER PREGUNTA:
Si el mensaje es una pregunta sobre progreso, estadísticas o cualquier otra cosa, responde en texto natural usando el contexto del usuario. Sé conciso y útil. Devuelve:
{"action": "reply", "text": "tu respuesta aquí"}

EJEMPLOS DE SESIÓN: "hoy gi 90 min", "nogi 1h estaba roto", "open mat esta mañana"
EJEMPLOS DE PREGUNTA: "cuántas horas llevo", "qué trabajé ayer", "cómo voy este mes", "dame un resumen"

IMPORTANTE: Responde SIEMPRE con JSON válido, sin markdown, sin backticks."""

async def call_claude(message: str, context: str) -> dict:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": f"CONTEXTO:\n{context}\n\nMENSAJE DE CARLOS: {message}"
        }]
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=body)
        if r.status_code != 200:
            raise Exception(f"Claude API error {r.status_code}: {r.text[:200]}")
        raw = r.json()["content"][0]["text"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)

# ── HANDLERS ─────────────────────────────────────────────
FEELING_EMOJI = {5: "🔥 En llamas", 4: "💪 Fuerte", 3: "😐 Normal", 2: "😴 Cansado", 1: "🤕 Roto"}

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola Carlos! Soy tu asistente de BJJ Journey.\n\n"
        "Puedo hacer dos cosas:\n\n"
        "🥋 *Guardar sesiones* — cuéntame tu entreno:\n"
        "_Hoy 90 min gi, guardia cerrada, me sentí fuerte_\n\n"
        "📊 *Responder preguntas* — sobre tu progreso:\n"
        "_¿Cuántas horas llevo este mes?_\n"
        "_¿Cómo voy esta semana?_\n"
        "_Dame un resumen_",
        parse_mode="Markdown"
    )

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_CHAT and str(update.effective_chat.id) != ALLOWED_CHAT:
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    context = await load_user_context()
    result = await call_claude("Dame un resumen completo de mi progreso", context)
    text = result.get("text", context)
    await update.message.reply_text(text)

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_CHAT and str(update.effective_chat.id) != ALLOWED_CHAT:
        return

    msg = update.message.text
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")

    # Cargar contexto del usuario
    context = await load_user_context()

    try:
        result = await call_claude(msg, context)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")
        return

    action = result.get("action")

    # MODO 2: Respuesta a pregunta
    if action == "reply":
        await update.message.reply_text(result.get("text", "No entendí la pregunta."))
        return

    # MODO 1: Guardar sesión
    if action == "save_session":
        import uuid
        today = date.today().strftime("%Y-%m-%d")
        session_date = result.get("date", today)
        session_data = {
            "id": f"{USER_ID}_{session_date}_{uuid.uuid4().hex[:8]}",
            "user_id": USER_ID,
            "date": session_date,
            "type": result.get("type", "Gi"),
            "duration": result.get("duration", 60),
            "feeling": result.get("feeling"),
            "position": result.get("position"),
            "notes": result.get("notes", ""),
            "from_gcal": False
        }

        await sb_ensure_profile()
        status = await sb_upsert("sessions", session_data)

        if status in (200, 201):
            duration_h = round(result.get("duration", 60) / 60, 1)
            feeling = result.get("feeling")
            feeling_str = f" · {FEELING_EMOJI[feeling]}" if feeling else ""
            position_str = f" · {result['position']}" if result.get("position") else ""
            notes_str = f"\n📝 {result['notes']}" if result.get("notes") else ""

            await update.message.reply_text(
                f"✅ *Sesión guardada*\n\n"
                f"📅 {session_date}\n"
                f"🥋 {result.get('type','Gi')} · {duration_h}h{feeling_str}{position_str}"
                f"{notes_str}\n\n"
                f"_Abre la app para verla en tu historial_",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ Error al guardar (status {status})")
        return

    # Fallback
    await update.message.reply_text("🤔 No entendí eso. Cuéntame tu sesión o hazme una pregunta sobre tu progreso.")

# ── MAIN ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 BJJ Journey Bot arrancado v2")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()


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
        # Strip markdown code blocks if present
        raw = raw.replace("```json", "").replace("```", "").strip()
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

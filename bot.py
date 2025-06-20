import os
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

import datetime
from google.cloud import firestore
import openai
import matplotlib.pyplot as plt
from io import BytesIO

# 👉 Cargar variables del archivo .env
load_dotenv()

# Configurar credenciales de Firestore y OpenAI
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
openai.api_key = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

db = firestore.Client()

# --- Funciones del bot (sin cambios importantes) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 ¡Hola! Registra tus gastos escribiéndolos así:\n\nEjemplo: 20000 comida")

async def clasificar_categoria(texto: str) -> str:
    prompt = f"Clasifica el siguiente gasto en una categoría como comida, transporte, salud, ocio, educación, etc. Solo responde con la categoría:\n\n'{texto}'"

    respuesta = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )

    categoria = respuesta.choices[0].message["content"].strip().lower()
    return categoria

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    try:
        if " " in text:
            amount_str, categoria = text.split(" ", 1)
        else:
            amount_str = text
            categoria = await clasificar_categoria(amount_str)

        amount = int(amount_str)

        db.collection("gastos").add({
            "user_id": user_id,
            "monto": amount,
            "categoria": categoria,
            "fecha": datetime.datetime.now()
        })

        await update.message.reply_text(f"Gasto registrado: ${amount} en {categoria} ✅")

    except Exception as e:
        await update.message.reply_text("❌ Formato no válido. Usa: [monto] [categoría]. Ej: 12000 transporte")

async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    docs = db.collection("gastos").where("user_id", "==", user_id).stream()

    resumen = {}
    for doc in docs:
        d = doc.to_dict()
        cat = d["categoria"]
        resumen[cat] = resumen.get(cat, 0) + d["monto"]

    if not resumen:
        await update.message.reply_text("📭 No tienes gastos registrados.")
        return

    mensaje = "🧾 *Resumen de gastos:*\n\n"
    for cat, total in resumen.items():
        mensaje += f"• {cat}: ${total}\n"

    await update.message.reply_text(mensaje)

async def total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    docs = db.collection("gastos").where("user_id", "==", user_id).stream()

    total_gasto = sum(doc.to_dict()["monto"] for doc in docs)
    await update.message.reply_text(f"💰 Total gastado: ${total_gasto}")

async def ultimo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    gastos = db.collection("gastos").where("user_id", "==", user_id).order_by("fecha", direction=firestore.Query.DESCENDING).limit(1).stream()

    for g in gastos:
        d = g.to_dict()
        fecha = d["fecha"]
        fecha_str = fecha.strftime("%Y-%m-%d %H:%M") if isinstance(fecha, datetime.datetime) else str(fecha)

        await update.message.reply_text(f"📌 Último gasto:\n${d['monto']} en {d['categoria']} el {fecha_str}")
        return

    await update.message.reply_text("📭 Aún no has registrado gastos.")

async def grafico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    gastos = db.collection("gastos").where("user_id", "==", user_id).stream()

    resumen = {}
    for doc in gastos:
        d = doc.to_dict()
        resumen[d["categoria"]] = resumen.get(d["categoria"], 0) + d["monto"]

    if not resumen:
        await update.message.reply_text("📭 No tienes datos suficientes para generar el gráfico.")
        return

    categorias = list(resumen.keys())
    montos = list(resumen.values())

    plt.figure(figsize=(6,6))
    plt.pie(montos, labels=categorias, autopct='%1.1f%%')
    plt.title("Distribución de gastos")

    buf = BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)

    await update.message.reply_photo(buf)
    buf.close()

# --- Configuración del bot ---
app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("resumen", resumen))
app.add_handler(CommandHandler("total", total))
app.add_handler(CommandHandler("ultimo", ultimo))
app.add_handler(CommandHandler("grafico", grafico))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app.run_polling()

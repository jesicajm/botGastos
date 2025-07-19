from dotenv import load_dotenv
from datetime import time, timedelta
import os
import asyncio
import pytz
import datetime
import matplotlib.pyplot as plt
from io import BytesIO
from dateutil import parser 
import re

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, 
    ChatMemberHandler, ConversationHandler
)

from google.cloud import firestore

# --- Configuración ---
load_dotenv()
import base64

firebase_key_base64 = os.getenv("FIREBASE_KEY_BASE64")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if firebase_key_base64:
    with open("firebase_key.json", "wb") as f:
        f.write(base64.b64decode(firebase_key_base64))
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "firebase_key.json"
else:
    raise ValueError("❌ La variable FIREBASE_KEY_BASE64 no está definida en el entorno.")

db = firestore.Client()

# --- Estados para la conversación de presupuesto ---
ESCOGER_CATEGORIA, ESPECIFICAR_LIMITE, PREGUNTAR_ACCION_POST_PRESUPUESTO, ESPERANDO_CATEGORIA_CONSULTA, ESPECIFICAR_CATEGORIA_PERSONALIZADA, CONFIRMAR_SOBREESCRITURA = range(6)
HANDLE_GASTO_CATEGORIA, HANDLE_GASTO_PERSONALIZADA = range(6, 8)
ESPECIFICAR_LIMITE_GASTO, PREGUNTAR_ACCION_POST_PRESUPUESTO_GASTO = range(8, 10)

CATEGORIAS_VALIDAS = [
    "comida", "transporte", "salud", "ocio", "educación", "hogar", "servicios"
]

# --- Utilidades ---

teclado_menu = ReplyKeyboardMarkup(
    [[KeyboardButton("📋 Menú")]],
    resize_keyboard=True
)

def obtener_teclado_principal():
    return ReplyKeyboardMarkup([
        ["📝 Registrar gasto", "📋 Menú"],
        ["📊 Resumen", "📈 Comparar"],
        ["💰 Total", "📌 Último"],
        ["🗑️ Eliminar", "📉 Gráfico"],
        ["💼 Presupuesto"]
    ], resize_keyboard=True)

def obtener_categorias_con_botones(user_id: str):
    categorias_ref = db.collection("usuarios").document(user_id).collection("categorias").stream()
    personalizadas = [doc.id for doc in categorias_ref]
    todas = list(dict.fromkeys(CATEGORIAS_VALIDAS + personalizadas))
    botones = [[InlineKeyboardButton(cat.capitalize(), callback_data=f"cat:{cat}")] for cat in todas]
    botones.append([InlineKeyboardButton("➕ Otra categoría", callback_data="catref:personalizada")])
    return InlineKeyboardMarkup(botones)

def extraer_monto_descripcion(texto):
    texto = texto.lower().strip()

    # Normaliza formatos como 5.000 o 5,000 → 5000
    texto = texto.replace(".", "").replace(",", "")

    # Caso: "5000 comida" o "5000comida"
    match = re.match(r"^(\d+)\s*([a-záéíóúñ ]+)$", texto)
    if match:
        return int(match.group(1)), match.group(2).strip()

    # Caso: "comida 5000"
    match = re.match(r"^([a-záéíóúñ ]+)\s*(\d+)$", texto)
    if match:
        return int(match.group(2)), match.group(1).strip()

    # Caso: "comida: 5000"
    match = re.match(r"^([a-záéíóúñ ]+):\s*(\d+)$", texto)
    if match:
        return int(match.group(2)), match.group(1).strip()

    # Caso: sin espacio entre palabra y número (ej: "banano2000")
    match = re.match(r"^([a-záéíóúñ]+)(\d+)$", texto)
    if match:
        return int(match.group(2)), match.group(1).strip()

    return None, None


def formatear_pesos(valor):
    return f"${valor:,.0f}".replace(",", ".")

def detectar_gasto_repetitivo(user_id, categoria, historial):
    promedio = sum(historial) / len(historial)
    if all(abs(g - promedio) / promedio < 0.1 for g in historial):
        return f"La categoría *{categoria}* muestra un patrón de gasto estable cada mes."
    return None

async def responder(update: Update, texto: str, **kwargs):
    if update.message:
        await update.message.reply_text(texto, **kwargs)
    elif update.callback_query:
        await update.callback_query.message.reply_text(texto, **kwargs)
    else:
        print("⚠️ No se pudo enviar mensaje: update sin message ni callback.")


async def responder_foto(update: Update, foto: BytesIO, **kwargs):
    if update.message:
        await update.message.reply_photo(foto, **kwargs)
    elif update.callback_query:
        await update.callback_query.message.reply_photo(foto, **kwargs)

def detectar_categoria_sin_limite(resumen, limites):
    sugerencias = []
    for cat in resumen:
        if cat not in limites:
            sugerencias.append(f"La categoría *{cat}* no tiene un límite definido.")
    return sugerencias

async def verificar_presupuesto(update: Update, user_id: str, categoria: str):
    presupuesto_ref = db.collection("usuarios").document(user_id).collection("presupuestos").document(categoria).get()
    if not presupuesto_ref.exists:
        return

    limite = presupuesto_ref.to_dict().get("limite", 0)

    inicio_mes = datetime.datetime.now(pytz.timezone("America/Bogota")).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    gastos = db.collection("usuarios").document(user_id).collection("gastos") \
        .where("categoria", "==", categoria) \
        .where("fecha", ">=", inicio_mes).stream()

    total_mes = sum(doc.to_dict().get("monto", 0) for doc in gastos)

    print(f"🧾 Total gastado en {categoria}: {formatear_pesos(total_mes)}")
    print(f"💸 Límite definido: {formatear_pesos(limite)}")

    if total_mes <= limite:
        return  # No se ha excedido el presupuesto

    exceso = total_mes - limite
    mensaje = (
        f"⚠️ *Atención:* Has superado tu presupuesto mensual para *{categoria}*.\n"
        f"• Límite: {formatear_pesos(limite)}\n"
        f"• Gastado: {formatear_pesos(total_mes)}\n"
        f"• Exceso: {formatear_pesos(exceso)}\n\n"
    )

    # Buscar sugerencias en otras categorías con saldo disponible
    sugerencias = []
    botones = []

    presupuestos = db.collection("usuarios").document(user_id).collection("presupuestos").stream()
    for doc in presupuestos:
        cat = doc.id
        if cat == categoria:
            continue  # omitimos la ya excedida

        data = doc.to_dict()
        limite_cat = data.get("limite", 0)
        gastos_cat = db.collection("usuarios").document(user_id).collection("gastos") \
            .where("categoria", "==", cat) \
            .where("fecha", ">=", inicio_mes).stream()
        total_cat = sum(g.to_dict().get("monto", 0) for g in gastos_cat)
        restante = limite_cat - total_cat

        if restante > 0:
            sugerencias.append({
                "categoria": cat,
                "limite": limite_cat,
                "gastado": total_cat,
                "restante": restante
            })

            botones.append([
                InlineKeyboardButton(
                    f"✏️ Ajustar {cat.capitalize()}",
                    callback_data=f"establecer_presupuesto:{cat}"
                )
            ])

    if sugerencias:
        mensaje += "💡 *Sugerencia:* Podrías ajustar el presupuesto en alguna de estas categorías:\n\n"
        for s in sugerencias:
            mensaje += (
                f"• {s['categoria'].capitalize()}:\n"
                f"  - Límite: {formatear_pesos(s['limite'])}\n"
                f"  - Gastado: {formatear_pesos(s['gastado'])}\n"
                f"  - Disponible: {formatear_pesos(s['restante'])}\n\n"
            )
    else:
        mensaje += "ℹ️ No hay otras categorías con presupuesto disponible actualmente."

    reply_markup = InlineKeyboardMarkup(botones) if botones else None

    if update.message:
        await update.message.reply_text(mensaje, parse_mode="Markdown", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(mensaje, parse_mode="Markdown", reply_markup=reply_markup)

# --- Funciones del bot ---

async def mostrar_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "📋 *Menú principal*\n\n"
        "Toca uno de los botones para usar el bot:\n\n"
        "📝 Registrar gasto — Registra un nuevo gasto (ej. 5000 comida)\n"
        "💼 Presupuesto — Establece un límite mensual por categoría\n"
        "📊 Resumen — Muestra lo que has gastado por categoría\n"
        "📈 Comparar — Compara tu gasto con el mes anterior\n"
        "💰 Total — Muestra cuánto llevas gastado este mes\n"
        "📌 Último — Te dice cuál fue tu último gasto\n"
        "🗑️ Eliminar — Elimina el último gasto que registraste\n"
        "📉 Gráfico — Muestra un gráfico circular de tus gastos"
    )

    botones = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Registrar gasto", callback_data="menu:registrar_gasto")],
        [InlineKeyboardButton("💼 Presupuesto", callback_data="menu:presupuesto")],
        [InlineKeyboardButton("📊 Resumen", callback_data="menu:resumen")],
        [InlineKeyboardButton("📈 Comparar", callback_data="menu:comparar")],
        [InlineKeyboardButton("💰 Total", callback_data="menu:total")],
        [InlineKeyboardButton("📌 Último", callback_data="menu:ultimo")],
        [InlineKeyboardButton("🗑️ Eliminar", callback_data="menu:eliminar")],
        [InlineKeyboardButton("📉 Gráfico", callback_data="menu:grafico")]
    ])

    if update.message:
        await update.message.reply_text(texto, parse_mode="Markdown", reply_markup=botones)
    elif update.callback_query:
        await update.callback_query.message.reply_text(texto, parse_mode="Markdown", reply_markup=botones)

async def manejar_menu_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Ejecutar la función correspondiente
    if data == "menu:registrar_gasto":
        await query.message.reply_text("✍️ Escribe el gasto en el formato: 5000 comida")
    elif data == "menu:resumen":
        await resumen(update, context)
    elif data == "menu:comparar":
        await comparar(update, context)
    elif data == "menu:total":
        await total(update, context)
    elif data == "menu:ultimo":
        await ultimo(update, context)
    elif data == "menu:eliminar":
        await eliminar(update, context)
    elif data == "menu:grafico":
        await grafico(update, context)
    else:
        await query.message.reply_text("❌ Opción no reconocida.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    now = datetime.datetime.now(pytz.timezone("America/Bogota"))
    user_ref = db.collection("usuarios").document(user_id)

    def verificar_y_crear():
        doc = user_ref.get()
        data = doc.to_dict() if doc.exists else {}
        if "fecha_inicio" not in data:
            user_ref.set({"fecha_inicio": now}, merge=True)

    await asyncio.to_thread(verificar_y_crear)

    await mostrar_menu(update, context)

# --- Flujo para establecer presupuesto ---
async def presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    botones_markup = obtener_categorias_con_botones(user_id)

    # Convertimos a lista de listas para modificar
    botones_lista = list(botones_markup.inline_keyboard)
    botones_lista.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_presupuesto")])

    reply_markup = InlineKeyboardMarkup(botones_lista)

    if update.message:
        await update.message.reply_text(
            "¿Para qué categoría deseas establecer un presupuesto mensual?", 
            reply_markup=reply_markup
        )
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            "¿Para qué categoría deseas establecer un presupuesto mensual?", 
            reply_markup=reply_markup
        )

    context.chat_data["conversation"] = "presupuesto"
    return ESCOGER_CATEGORIA

async def seleccionar_categoria_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✍️ Escribe el nombre de la nueva categoría personalizada:")
    return ESPECIFICAR_CATEGORIA_PERSONALIZADA

async def escoger_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("🎯 Entró al handler escoger_categoria")  # << Este sí debe aparecer
    query = update.callback_query
    await query.answer()

    categoria = query.data.split(":")[1]
    context.user_data['categoria_presupuesto'] = categoria

    await query.edit_message_text(
        f"¿Cuál es tu presupuesto mensual para *{categoria}*?",
        parse_mode="Markdown"
    )

    if context.chat_data.get("conversation") == "gasto":
        return ESPECIFICAR_LIMITE_GASTO
    else:
        return ESPECIFICAR_LIMITE

async def guardar_categoria_personalizada_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    categoria = update.message.text.strip().lower()
    context.user_data['categoria_presupuesto'] = categoria

    await update.message.reply_text(
        f"¿Cuál es tu presupuesto mensual para *{categoria}*?",
        parse_mode="Markdown"
    )
    return ESPECIFICAR_LIMITE

async def especificar_limite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = update.message.text.strip().replace(".", "").replace(",", "")
        print(f"🟢 especificar_limite: texto={texto}, user={update.effective_user.id}, estado={context.chat_data.get('conversation')}")

        if not texto.isdigit():
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔁 Intentar de nuevo", callback_data="reintentar_limite"),
                    InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_presupuesto")
                ]
            ])
            await update.message.reply_text(
                "❌ El valor debe ser numérico. Por ejemplo: `20000`",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            return ESPECIFICAR_LIMITE

        limite = int(texto)

        categoria = context.user_data['categoria_presupuesto']
        user_id = str(update.effective_user.id)

        # Verificar si ya existe
        presupuesto_doc = db.collection("usuarios").document(user_id).collection("presupuestos").document(categoria).get()

        if presupuesto_doc.exists:
            limite_actual = presupuesto_doc.to_dict().get("limite", 0)
            context.user_data["nuevo_limite"] = limite
            await update.message.reply_text(
                f"⚠️ Ya tienes un presupuesto para *{categoria}* de ${limite_actual:,}.\n"
                f"¿Quieres reemplazarlo por ${limite:,}?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Sí, reemplazar", callback_data="confirmar_reemplazo")],
                    [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_reemplazo")]
                ])
            )
            return CONFIRMAR_SOBREESCRITURA

        # Si no existía, guarda directamente
        await guardar_presupuesto(user_id, categoria, limite, update)
        
        if context.chat_data.get("conversation") == "gasto":
            return PREGUNTAR_ACCION_POST_PRESUPUESTO_GASTO
        else:
            return PREGUNTAR_ACCION_POST_PRESUPUESTO

    except ValueError:
        await update.message.reply_text("❌ El valor debe ser numérico. Usa: [monto]. Ej: 12.000")
        return ESPECIFICAR_LIMITE

async def reintentar_especificar_limite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    categoria = context.user_data.get('categoria_presupuesto', 'la categoría seleccionada')
    await query.edit_message_text(
        f"✍️ ¿Cuál es el *límite mensual* para la categoría *{categoria}*?\n\n"
        f"Por ejemplo: `50.000`",
        parse_mode="Markdown"
    )
    return ESPECIFICAR_LIMITE

async def guardar_presupuesto(user_id, categoria, limite, update):
    db.collection("usuarios").document(user_id).collection("presupuestos").document(categoria).set({
        "limite": limite,
        "actualizado": datetime.datetime.now(pytz.timezone("America/Bogota"))
    })

    db.collection("usuarios").document(user_id).collection("categorias").document(categoria).set({
        "nombre": categoria
    }, merge=True)

    await update.message.reply_text(
        rf"✅ Listo. Tu presupuesto para *{categoria}* es de ${limite:,} al mes.",
        parse_mode="Markdown"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Registrar otro presupuesto", callback_data="otro_presupuesto")],
        [InlineKeyboardButton("📝 Registrar un gasto", callback_data="registrar_gasto")],
        [InlineKeyboardButton("🚪 Salir", callback_data="salir")],
    ])

    await update.message.reply_text("¿Qué deseas hacer ahora?", reply_markup=keyboard)

async def confirmar_reemplazo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    categoria = context.user_data.get("categoria_presupuesto")
    limite = context.user_data.get("nuevo_limite")

    await guardar_presupuesto(user_id, categoria, limite, query)

    context.chat_data.pop("conversation", None)

    return PREGUNTAR_ACCION_POST_PRESUPUESTO

async def cancelar_reemplazo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("❌ Operación cancelada. El presupuesto anterior se mantuvo.")
    context.chat_data.pop("conversation", None)
    return ConversationHandler.END

async def manejar_accion_post_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    opcion = query.data
    print(f"🔄 manejar_accion_post_presupuesto: opción={opcion}, user={update.effective_user.id}")

    if opcion == "otro_presupuesto":
        origen = context.chat_data.get("conversation")
        print(origen)
        if origen not in ["presupuesto", "gasto"]:
        # Si no está seteado aún, por defecto asumimos "presupuesto"
            context.chat_data["conversation"] = "presupuesto"
        user_id = str(update.effective_user.id)
        botones = obtener_categorias_con_botones(user_id) 
        await query.edit_message_text("📝 ¿Para qué categoría deseas establecer otro presupuesto?")
        await query.message.reply_text("Selecciona una categoría:", reply_markup=botones)
        return ESCOGER_CATEGORIA

    elif opcion == "registrar_gasto":
        print("✅ Entró en 'registrar_gasto'")
        context.chat_data.pop("conversation", None)
        await query.edit_message_text("✍️ Escribe el gasto en el formato: 12000 transporte")
        return ConversationHandler.END

    elif opcion == "salir":
        context.chat_data.pop("conversation", None)
        await query.edit_message_text("🚪 ¡Listo! Puedes seguir usando otros comandos cuando quieras.")
        return ConversationHandler.END


async def cancelar_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()  # opcional, para cerrar el "loading" en Telegram
        await update.callback_query.message.reply_text("❌ Cancelado. No se guardó ningún presupuesto.")
    elif update.message:
        await update.message.reply_text("❌ Cancelado. No se guardó ningún presupuesto.")
    
    context.chat_data.pop("conversation", None)

    return ConversationHandler.END


async def handle_new_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = update.my_chat_member.new_chat_member.status
    if status == "member":
        await mostrar_menu(update, context)


async def consulta_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    presupuestos_ref = db.collection("usuarios").document(user_id).collection("presupuestos")
    docs = list(presupuestos_ref.stream())

    if not docs:
        await update.message.reply_text("📭 Aún no tienes categorías con presupuesto registrado.")
        return ConversationHandler.END  # Puedes usar END si no hay conversación que continuar

    # Crear lista de botones con categorías
    categorias = [doc.id for doc in docs]

    if not categorias:
        await update.message.reply_text("⚠️ No hay categorías disponibles.")
        return ConversationHandler.END

    botones = [
         [InlineKeyboardButton(text=cat, callback_data=f"consulta_categoria:{cat}")]
            for cat in categorias
    ]

    reply_markup = InlineKeyboardMarkup(botones)

    await update.message.reply_text(
        "📊 ¿De qué categoría deseas consultar el presupuesto?",
        reply_markup=reply_markup
    )

    return ESPERANDO_CATEGORIA_CONSULTA

async def responder_consulta_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    categoria = query.data.split("consulta_categoria:")[-1]
    user_id = str(update.effective_user.id)

    presupuesto_doc = db.collection("usuarios").document(user_id).collection("presupuestos").document(categoria).get()
    if not presupuesto_doc.exists:
        await query.edit_message_text("❌ Esa categoría no tiene presupuesto registrado.")
        return ConversationHandler.END

    presupuesto = presupuesto_doc.to_dict().get("limite", 0)

    ahora = datetime.datetime.now(pytz.timezone("America/Bogota"))
    inicio_mes = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    gastos = db.collection("usuarios").document(user_id).collection("gastos") \
        .where(field_path="categoria", op_string="==", value=categoria) \
        .where(field_path="fecha", op_string=">=", value=inicio_mes) \
        .stream()
        
    total_gastado = sum(doc.to_dict()["monto"] for doc in gastos)
    restante = presupuesto - total_gastado

    await query.edit_message_text(
        f"📋 *Presupuesto para {categoria}:*\n"
        f"• Límite mensual: ${presupuesto:,}\n"
        f"• Gastado: ${total_gastado:,}\n"
        f"• Disponible: ${restante:,}",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    print(f"📥 handle_message recibido: {update.message.text}")
    chat_id = update.effective_chat.id

    # Verifica si hay una conversación activa
    chat_data = context.chat_data
    conversation_state = chat_data.get('conversation')

    print(context.chat_data.get("conversation"))

    if conversation_state is not None:
        # El usuario está en una conversación activa (presupuesto, eliminar, etc.)
        return  # Ignora el mensaje

    try:
        texto = update.message.text.strip()
        monto, descripcion = extraer_monto_descripcion(texto)

        if monto is None or not descripcion:
            await update.message.reply_text(
                "❌ No entendí el formato. Prueba con ejemplos como:\n"
                "• `5000 comida`\n• `comida 5000`\n• `comida: 5.000`",
                parse_mode="Markdown"
            )
            return

        context.user_data["gasto"] = {
            "monto": monto,
            "descripcion": descripcion
        }

        # Mostrar botones con categorías
        user_id = str(update.effective_user.id)
        keyboard = obtener_categorias_con_botones(user_id)
        await update.message.reply_text("Selecciona la categoría del gasto:", reply_markup=keyboard)

        return HANDLE_GASTO_CATEGORIA

    except Exception as e:
        print(f"❌ Error en handle_message: {e}")
        await update.message.reply_text(
            "❌ Formato no válido. Usa: [monto] [descripción]. Ej: 12000 uber"
        )

async def seleccionar_categoria_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    print("👉 Callback capturado", query.data)

    user_id = str(query.from_user.id)
    data = query.data.replace("catref:", "").replace("cat:", "")

    if data == "personalizada":
        await query.edit_message_text("✍️ Escribe el nombre de la nueva categoría:")
        return HANDLE_GASTO_PERSONALIZADA

    categoria = data.strip().lower()

    if "gasto" not in context.user_data:
        context.user_data["gasto"] = {}

    context.user_data["gasto"]["categoria"] = categoria

    await guardar_gasto_con_categoria(update, context, message_id=query.message.message_id)
    return HANDLE_GASTO_CATEGORIA

async def handle_categoria_personalizada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    categoria = update.message.text.strip().lower()
    context.user_data["gasto"]["categoria"] = categoria
    await guardar_gasto_con_categoria(update, context)
    return HANDLE_GASTO_CATEGORIA

async def guardar_gasto_con_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id=None):
    user_id = str(update.effective_user.id)
    gasto_data = context.user_data.get("gasto", {})
    categoria = gasto_data.get("categoria")
    monto = gasto_data.get("monto")
    fecha = datetime.datetime.now(pytz.timezone("America/Bogota"))

    if not categoria or not monto:
        await update.message.reply_text("❌ Hubo un error guardando el gasto.")
        return ConversationHandler.END

    # Guardar el gasto en Firestore
    gasto = {
        "monto": monto,
        "categoria": categoria,
        "fecha": fecha
    }
    db.collection("usuarios").document(user_id).collection("gastos").add(gasto)

    if update.message:
        await update.message.reply_text(
            f"💾 Gasto registrado en la categoría *{categoria}* por *${monto:,.0f}*",
            parse_mode="Markdown"
        )
    elif update.callback_query:
        await update.callback_query.message.reply_text(
        f"💾 Gasto registrado en la categoría *{categoria}* por *${monto:,.0f}*",
            parse_mode="Markdown"
        )

    # Verificar si hay presupuesto
    presupuesto_doc = db.collection("usuarios").document(user_id).collection("presupuestos").document(categoria).get()

    if not presupuesto_doc.exists:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sí, establecer límite", callback_data=f"establecer_presupuesto:{categoria}")],
            [InlineKeyboardButton("❌ No, gracias", callback_data="ignorar_presupuesto")]
        ])
        texto = (
            f"🔎 Veo que *{categoria}* no tiene un presupuesto mensual definido.\n"
            f"¿Deseas establecer un límite?"
        )
        if update.message:
            await update.message.reply_text(texto, parse_mode="Markdown", reply_markup=keyboard)
        elif update.callback_query:
            await update.callback_query.message.reply_text(texto, parse_mode="Markdown", reply_markup=keyboard)
        return HANDLE_GASTO_CATEGORIA
     
    await verificar_presupuesto(update, user_id, categoria)  # ✅ Mostrar advertencia si excede presupuesto
    context.chat_data.pop("conversation", None)
    return ConversationHandler.END

async def iniciar_establecer_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    categoria = query.data.split(":")[1]
    context.user_data["categoria_presupuesto"] = categoria

    context.chat_data["conversation"] = "gasto"
    
    await query.message.reply_text(
        f"✍️ ¿Cuál es el *límite mensual* para la categoría *{categoria}*?\n\n"
        f"Por ejemplo: `250.000`",
        parse_mode="Markdown"
    )

    if context.chat_data.get("conversation") == "gasto":
        return ESPECIFICAR_LIMITE_GASTO
    else:
        return ESPECIFICAR_LIMITE

async def ignorar_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Entendido. Puedes establecer un presupuesto en cualquier momento con /presupuesto.")
    return ConversationHandler.END


async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    docs = db.collection("usuarios").document(user_id).collection("gastos").stream()
    resumen = {}
    for doc in docs:
        d = doc.to_dict()
        resumen[d["categoria"]] = resumen.get(d["categoria"], 0) + d["monto"]
    if not resumen:
        await responder(update, "📭 No tienes gastos registrados.")
        return
    mensaje = "🧾 *Resumen de gastos:*\n\n"
    for cat, total in resumen.items():
        mensaje += f"• {cat}: ${total:,.0f}".replace(",", ".") + "\n"
    await responder(update, mensaje, parse_mode="Markdown")

async def comparar_categorias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    tz = pytz.timezone("America/Bogota")
    now = datetime.datetime.now(tz)
    inicio_mes_actual = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    inicio_mes_anterior = (inicio_mes_actual - datetime.timedelta(days=1)).replace(day=1)

    gastos_actual = db.collection("usuarios").document(user_id).collection("gastos") \
        .where("fecha", ">=", inicio_mes_actual).stream()
    gastos_anterior = db.collection("usuarios").document(user_id).collection("gastos") \
        .where("fecha", ">=", inicio_mes_anterior).where("fecha", "<", inicio_mes_actual).stream()

    def acumular_por_categoria(stream):
        resumen = {}
        for doc in stream:
            d = doc.to_dict()
            resumen[d.get("categoria", "otros")] = resumen.get(d.get("categoria", "otros"), 0) + d.get("monto", 0)
        return resumen

    actual = acumular_por_categoria(gastos_actual)
    anterior = acumular_por_categoria(gastos_anterior)

    categorias = set(actual.keys()).union(anterior.keys())
    mensaje = "\ud83d\udcc8 *Comparativa mensual por categoría:*\n\n"

    for cat in categorias:
        gasto_actual = actual.get(cat, 0)
        gasto_anterior = anterior.get(cat, 0)
        if gasto_anterior == 0:
            variacion = "\ud83d\udd39 Sin dato anterior"
        else:
            cambio = ((gasto_actual - gasto_anterior) / gasto_anterior) * 100
            simbolo = "\ud83d\udd3a" if cambio > 0 else "\ud83d\udd3b"
            variacion = f"{simbolo} {abs(cambio):.1f}%"

        mensaje += f"• {cat}: ${gasto_anterior:,.0f} → ${gasto_actual:,.0f} ({variacion})\n".replace(",", ".")

    await update.message.reply_text(mensaje, parse_mode="Markdown")

async def comparar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    now = datetime.datetime.now(pytz.timezone("America/Bogota"))
    inicio_mes = now.replace(day=1)
    inicio_mes_anterior = (inicio_mes - datetime.timedelta(days=1)).replace(day=1)
    docs_actual = db.collection("usuarios").document(user_id).collection("gastos") \
        .where("fecha", ">=", inicio_mes).stream()
    docs_anterior = db.collection("usuarios").document(user_id).collection("gastos") \
        .where("fecha", ">=", inicio_mes_anterior).where("fecha", "<", inicio_mes).stream()
    suma_actual = sum(doc.to_dict()["monto"] for doc in docs_actual)
    suma_anterior = sum(doc.to_dict()["monto"] for doc in docs_anterior)
    variacion = ((suma_actual - suma_anterior) / suma_anterior * 100) if suma_anterior > 0 else 0
    signo = "🔺" if variacion > 0 else "🔻"
    actual_str = f"${suma_actual:,.0f}".replace(",", ".")
    anterior_str = f"${suma_anterior:,.0f}".replace(",", ".")
    await responder(update, f"📊 Gasto mensual:\nEste mes: {actual_str}\nMes anterior: {anterior_str}\nVariación: {signo} {abs(round(variacion, 1))}%")
    
async def total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    docs = db.collection("usuarios").document(user_id).collection("gastos").stream()
    total_gasto = sum(doc.to_dict()["monto"] for doc in docs)
    await responder(update, f"💰 Total gastado: ${total_gasto:,.0f}".replace(",", "."))

async def ultimo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    gastos = db.collection("usuarios").document(user_id).collection("gastos") \
        .order_by("fecha", direction=firestore.Query.DESCENDING).limit(1).stream()
    for g in gastos:
        d = g.to_dict()
        fecha_val = d["fecha"]
        if isinstance(fecha_val, str):
            fecha_val = parser.parse(fecha_val)  # convierte string a datetime

        fecha_str = fecha_val.strftime("%Y-%m-%d %H:%M")
        monto_formateado = formatear_pesos(d["monto"])
        await responder(update, f"📌 Último gasto:\n{monto_formateado} en {d['categoria']} el {fecha_str}")
        return
    await responder(update, "📭 Aún no has registrado gastos.")

async def eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    gastos = list(db.collection("usuarios").document(user_id).collection("gastos")
        .order_by("fecha", direction=firestore.Query.DESCENDING).limit(1).stream())
    if not gastos:
        await responder(update, "📭 No hay gastos para eliminar.")
        return
    gasto = gastos[0]
    d = gasto.to_dict()
    context.user_data["ultimo_id"] = gasto.id
    fecha_val = d["fecha"]
    if isinstance(fecha_val, str):
        fecha_val = parser.parse(fecha_val)

    fecha_str = fecha_val.strftime("%Y-%m-%d %H:%M")
    
    monto_formateado = formatear_pesos(d["monto"])

    msg = f"❗ ¿Deseas eliminar el último gasto?\n\n💸 {monto_formateado} en {d['categoria']} el {fecha_str}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sí", callback_data="confirmar_eliminar")],
        [InlineKeyboardButton("❌ No", callback_data="cancelar_eliminar")]
    ])
    await responder(update, msg, reply_markup=keyboard)


async def callback_confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    if query.data == "confirmar_eliminar":
        gasto_id = context.user_data.get("ultimo_id")
        if gasto_id:
            db.collection("usuarios").document(user_id).collection("gastos").document(gasto_id).delete()
            await query.edit_message_text("✅ Gasto eliminado correctamente.")
            context.user_data.pop("ultimo_id", None)
        else:
            await query.edit_message_text("⚠️ No se encontró el gasto a eliminar.")
    elif query.data == "cancelar_eliminar":
        await query.edit_message_text("❌ Eliminación cancelada.")
        context.user_data.pop("ultimo_id", None)

async def enviar_resumen_automatico(context: ContextTypes.DEFAULT_TYPE):   
    print("⌛ Ejecutando resumen automático...")

    application = context.application
    usuarios_ref = await asyncio.to_thread(lambda: list(db.collection("usuarios").stream()))
    now = datetime.datetime.now(pytz.timezone("America/Bogota"))

    for usuario in usuarios_ref:
        user_id = usuario.id
        datos_usuario = usuario.to_dict()
        fecha_inicio = datos_usuario.get("fecha_inicio")

        if not fecha_inicio:
            print(f"⚠️ Usuario {user_id} no tiene fecha de inicio registrada.")
            continue

        # Convertir a datetime si es timestamp
        if isinstance(fecha_inicio, float):
            fecha_inicio = datetime.datetime.fromtimestamp(fecha_inicio, tz=pytz.timezone("America/Bogota"))
        elif isinstance(fecha_inicio, datetime.datetime):
            fecha_inicio = fecha_inicio.astimezone(pytz.timezone("America/Bogota"))
        else:
            print(f"⚠️ Formato de fecha inválido para {user_id}")
            continue

        # Ejecutar solo el día 1 de cada trimestre contado desde la fecha de inicio
        if now.day == 1:
            meses_transcurridos = (now.year - fecha_inicio.year) * 12 + (now.month - fecha_inicio.month)
            if meses_transcurridos % 3 == 0:
                await enviar_reporte_trimestral(user_id, now, application.bot)

        # Obtener gastos
        try:
            docs = await asyncio.to_thread(
                lambda: list(db.collection("usuarios").document(user_id).collection("gastos").stream())
            )
        except Exception as e:
            print(f"❌ Error al obtener gastos de {user_id}: {e}")
            continue

        # Obtener límites desde /presupuestos/{categoria}
        try:
            limites_docs = await asyncio.to_thread(
                lambda: list(db.collection("usuarios").document(user_id).collection("presupuestos").stream())
            )
            limites = {}
            for doc in limites_docs:
                data = doc.to_dict()
                categoria = doc.id
                limite = data.get("limite", 0)
                if isinstance(limite, (int, float)):
                    limites[categoria] = limite
        except Exception as e:
            print(f"⚠️ No se pudieron obtener límites para {user_id}: {e}")
            limites = {}

        # Calcular gastos por categoría
        resumen = {}
        for doc in docs:
            d = doc.to_dict()
            categoria = d.get("categoria")
            monto = d.get("monto", 0)
            if categoria and isinstance(monto, (int, float)):
                resumen[categoria] = resumen.get(categoria, 0) + monto

        if not resumen:
            continue

        # Crear mensaje
        mensaje = "🧾 *Resumen semanal de tus gastos:*\n\n"
        for cat, total in resumen.items():
            limite = limites.get(cat)
            if limite is not None:
                restante = limite - total
                estado = f"(Te quedan ${restante:,.2f})" if restante >= 0 else f"(Excedido por ${-restante:,.2f})"
                mensaje += f"• {cat}: ${total:,.2f} / ${limite:,.2f} {estado}\n"
            else:
                mensaje += f"• {cat}: ${total:,.2f} (sin límite asignado)\n"

        # Añadir sugerencias de optimización si hay categorías sin límite
        sin_limite = detectar_categoria_sin_limite(resumen, limites)
        if sin_limite:
            mensaje += "\n\n🛠️ *Sugerencias de optimización:*\n"
            for alerta in sin_limite:
                mensaje += f"• {alerta}\n"

        # Enviar mensaje
        try:
            await application.bot.send_message(
                chat_id=int(user_id),
                text=mensaje,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"❌ No se pudo enviar resumen a {user_id}: {type(e).__name__} - {e}")


async def grafico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    gastos = db.collection("usuarios").document(user_id).collection("gastos").stream()
    resumen = {}
    for doc in gastos:
        d = doc.to_dict()
        resumen[d["categoria"]] = resumen.get(d["categoria"], 0) + d["monto"]
    if not resumen:
        await responder(update, "📭 No tienes datos suficientes para generar el gráfico.")
        return

    categorias = list(resumen.keys())
    valores = list(resumen.values())

    plt.figure(figsize=(6, 6))
    plt.pie(valores, labels=categorias, autopct="%1.1f%%", startangle=90)
    plt.title("Distribución de gastos por categoría")
    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)

    await responder_foto(update, buf)

def detectar_aumento_inusual(actual, anterior):
    alertas = []
    for cat in actual:
        monto_actual = actual.get(cat, 0)
        monto_anterior = anterior.get(cat, 0)
        if monto_anterior == 0:
            continue
        variacion = (monto_actual - monto_anterior) / monto_anterior
        if variacion >= 0.5:  # 50% o más
            alertas.append(f"{cat.capitalize()}: +{variacion*100:.1f}%")
    return alertas

def detectar_excesos_frecuentes(user_id: str, now: datetime.datetime, meses: int = 3):
    tz = pytz.timezone("America/Bogota")
    categoria_excesos = {}

    for i in range(1, meses + 1):
        inicio = (now.replace(day=1) - timedelta(days=30 * i)).replace(day=1)
        fin = (inicio + timedelta(days=32)).replace(day=1)

        gastos = db.collection("usuarios").document(user_id).collection("gastos") \
            .where("fecha", ">=", inicio).where("fecha", "<", fin).stream()

        gastos_por_categoria = {}
        for doc in gastos:
            d = doc.to_dict()
            cat = d.get("categoria")
            monto = d.get("monto", 0)
            if cat:
                gastos_por_categoria[cat] = gastos_por_categoria.get(cat, 0) + monto

        # Obtener límites de presupuesto
        presupuestos = db.collection("usuarios").document(user_id).collection("presupuestos").stream()
        limites = {doc.id: doc.to_dict().get("limite", 0) for doc in presupuestos}

        for cat, total in gastos_por_categoria.items():
            limite = limites.get(cat)
            if limite and total > limite:
                categoria_excesos.setdefault(cat, 0)
                categoria_excesos[cat] += 1

    # Devolver solo las categorías con al menos 2 excesos
    return [cat for cat, veces in categoria_excesos.items() if veces >= 2]

async def enviar_reporte_mensual(context: ContextTypes.DEFAULT_TYPE):
    print("📆 Ejecutando reporte mensual")

    application = context.application
    now = datetime.datetime.now(pytz.timezone("America/Bogota"))

    usuarios_ref = await asyncio.to_thread(lambda: list(db.collection("usuarios").stream()))

    for usuario in usuarios_ref:
        user_id = usuario.id

        try:
            inicio_mes_actual = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            inicio_mes_anterior = (inicio_mes_actual - timedelta(days=1)).replace(day=1)
            fin_mes_anterior = inicio_mes_actual

            gastos_actuales = db.collection("usuarios").document(user_id).collection("gastos") \
                .where("fecha", ">=", inicio_mes_actual).where("fecha", "<", now).stream()
            gastos_anteriores = db.collection("usuarios").document(user_id).collection("gastos") \
                .where("fecha", ">=", inicio_mes_anterior).where("fecha", "<", fin_mes_anterior).stream()

            resumen_actual = {}
            resumen_anterior = {}

            for doc in gastos_actuales:
                d = doc.to_dict()
                resumen_actual[d["categoria"]] = resumen_actual.get(d["categoria"], 0) + d.get("monto", 0)
            for doc in gastos_anteriores:
                d = doc.to_dict()
                resumen_anterior[d["categoria"]] = resumen_anterior.get(d["categoria"], 0) + d.get("monto", 0)

            alertas = detectar_aumento_inusual(resumen_actual, resumen_anterior)
            excesos_frecuentes = detectar_excesos_frecuentes(user_id, now)

            # Mostrar mensaje solo si hay algo relevante que notificar
            if alertas or excesos_frecuentes:
                mensaje = f"📈 *Resumen de gastos del {inicio_mes_actual.strftime('%d/%m')} al {now.strftime('%d/%m')}*\n\n"

                if alertas:
                    mensaje += "🚨 Detectamos aumentos inusuales en estas categorías:\n"
                    for alerta in alertas:
                        mensaje += f"• {alerta}\n"

                if excesos_frecuentes:
                    mensaje += "\n🔁 *Excesos frecuentes detectados en los últimos 3 meses:*\n"
                    for cat in excesos_frecuentes:
                        mensaje += f"• {cat.capitalize()}\n"

                await application.bot.send_message(
                    chat_id=int(user_id),
                    text=mensaje,
                    parse_mode="Markdown"
                )

        except Exception as e:
            print(f"❌ Error al generar reporte mensual para {user_id}: {e}")


async def enviar_reporte_trimestral(user_id, now, bot): 
    if now.month % 3 != 0 or now.day != 1:
        return

    usuario_doc = db.collection("usuarios").document(user_id).get()
    if not usuario_doc.exists:
        return

    fecha_inicio = usuario_doc.to_dict().get("fecha_inicio")
    if not fecha_inicio:
        return

    meses_transcurridos = (now.year - fecha_inicio.year) * 12 + (now.month - fecha_inicio.month)
    if meses_transcurridos % 3 != 0:
        return

    categoria_gastos = {}
    for i in range(3, 0, -1):
        inicio = (now.replace(day=1) - timedelta(days=30 * i)).replace(day=1)
        fin = (inicio + timedelta(days=32)).replace(day=1)
        gastos = db.collection("usuarios").document(user_id).collection("gastos") \
            .where("fecha", ">=", inicio).where("fecha", "<", fin).stream()

        for g in gastos:
            d = g.to_dict()
            cat = d.get("categoria")
            if cat:
                categoria_gastos.setdefault(cat, []).append(d.get("monto", 0))

    mensaje = "📊 *Revisión trimestral de hábitos de gasto*\n\n"
    for cat, historial in categoria_gastos.items():
        if len(historial) == 3:
            alerta = detectar_gasto_repetitivo(user_id, cat, historial)
            if alerta:
                mensaje += f"• {alerta}\n"

    if mensaje.strip() != "📊 *Revisión trimestral de hábitos de gasto*":
        await bot.send_message(
            chat_id=int(user_id),
            text=mensaje,
            parse_mode="Markdown"
        )


async def comando_desconocido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 
    await responder(update, "❌ Comando no reconocido. Usa los botones o escribe /menu para ver opciones.")
    await mostrar_menu(update, context)

# --- Main ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).job_queue_enabled(True).build()

    # Conversación para establecer presupuesto
    conv_presupuesto = ConversationHandler(
        entry_points=[
                CommandHandler("presupuesto", presupuesto),
                MessageHandler(filters.TEXT & filters.Regex("^💼 Presupuesto$"), presupuesto),
                CallbackQueryHandler(presupuesto, pattern="^menu:presupuesto$")
        ],
        states={
            ESCOGER_CATEGORIA: [
                CallbackQueryHandler(escoger_categoria, pattern=r"^cat:.*"),
                CallbackQueryHandler(seleccionar_categoria_presupuesto, pattern=r"^catref:personalizada$"), 
                CallbackQueryHandler(cancelar_presupuesto, pattern="^cancelar_presupuesto$")
            ],
            ESPECIFICAR_CATEGORIA_PERSONALIZADA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_categoria_personalizada_presupuesto)
            ],
            ESPECIFICAR_LIMITE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, especificar_limite),
                CallbackQueryHandler(reintentar_especificar_limite, pattern="^reintentar_limite$"),
                CallbackQueryHandler(cancelar_presupuesto, pattern="^cancelar_presupuesto$")
            ],
            PREGUNTAR_ACCION_POST_PRESUPUESTO: [
                CallbackQueryHandler(manejar_accion_post_presupuesto),
                CallbackQueryHandler(iniciar_establecer_presupuesto, pattern=r"^establecer_presupuesto:.+"),
                CallbackQueryHandler(ignorar_presupuesto, pattern=r"^ignorar_presupuesto$")
            ],
            CONFIRMAR_SOBREESCRITURA: [
                CallbackQueryHandler(confirmar_reemplazo, pattern="^confirmar_reemplazo$"),
                CallbackQueryHandler(cancelar_reemplazo, pattern="^cancelar_reemplazo$")
            ],
            ESPERANDO_CATEGORIA_CONSULTA: [
                CallbackQueryHandler(responder_consulta_presupuesto, pattern=r"^consulta_categoria:.+")
            ]    
        },
        fallbacks=[CommandHandler("menu", mostrar_menu),
                   CommandHandler("ultimo", ultimo),
                   CommandHandler("total", total),
                   CommandHandler("resumen", resumen),
                   CommandHandler("grafico", grafico),
                   CommandHandler("comparar", comparar),
                   CommandHandler("eliminar", eliminar),
                   CommandHandler("cancelar", cancelar_presupuesto), 
                   MessageHandler(filters.COMMAND, cancelar_presupuesto),
                   CallbackQueryHandler(cancelar_presupuesto, pattern=r"^cancelar_presupuesto$") ],
        per_chat=True
    )

    gasto_categoria_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message) # <- Aquí sí se activa bien el flujo

        ],
        states={
            HANDLE_GASTO_CATEGORIA: [
                CallbackQueryHandler(seleccionar_categoria_ref, pattern=r"^catref:.*"),
                CallbackQueryHandler(seleccionar_categoria_ref, pattern=r"^cat:.*"),CallbackQueryHandler(iniciar_establecer_presupuesto, pattern=r"^establecer_presupuesto:.+"), 
                CallbackQueryHandler(ignorar_presupuesto, pattern=r"^ignorar_presupuesto$"), 
            ],
            HANDLE_GASTO_PERSONALIZADA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_categoria_personalizada)
            ],
            ESPECIFICAR_LIMITE_GASTO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, especificar_limite),
                CallbackQueryHandler(reintentar_especificar_limite, pattern="^reintentar_limite$"),
                CallbackQueryHandler(cancelar_presupuesto, pattern="^cancelar_presupuesto$")
            ],
            ESPECIFICAR_LIMITE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, especificar_limite),
                CallbackQueryHandler(reintentar_especificar_limite, pattern="^reintentar_limite$"),
                CallbackQueryHandler(cancelar_presupuesto, pattern="^cancelar_presupuesto$")
            ],
            PREGUNTAR_ACCION_POST_PRESUPUESTO_GASTO: [
                CallbackQueryHandler(manejar_accion_post_presupuesto),
                CallbackQueryHandler(ignorar_presupuesto, pattern=r"^ignorar_presupuesto$"),
                CallbackQueryHandler(iniciar_establecer_presupuesto, pattern=r"^establecer_presupuesto:.+"),
                CallbackQueryHandler(ignorar_presupuesto, pattern=r"^ignorar_presupuesto$")
            ],
            PREGUNTAR_ACCION_POST_PRESUPUESTO: [
                CallbackQueryHandler(manejar_accion_post_presupuesto),
                CallbackQueryHandler(ignorar_presupuesto, pattern=r"^ignorar_presupuesto$"),
                CallbackQueryHandler(iniciar_establecer_presupuesto, pattern=r"^establecer_presupuesto:.+"),
            ],
            ESCOGER_CATEGORIA: [                      # <== AGREGA ESTE BLOQUE
                CallbackQueryHandler(escoger_categoria, pattern=r"^cat:.*"),
                CallbackQueryHandler(seleccionar_categoria_presupuesto, pattern=r"^catref:personalizada$"),
                CallbackQueryHandler(cancelar_presupuesto, pattern="^cancelar_presupuesto$")
            ],
            CONFIRMAR_SOBREESCRITURA: [
                CallbackQueryHandler(confirmar_reemplazo, pattern="^confirmar_reemplazo$"),
                CallbackQueryHandler(cancelar_reemplazo, pattern="^cancelar_reemplazo$")
            ],
        },
        fallbacks=[CommandHandler("menu", mostrar_menu),
                   CommandHandler("ultimo", ultimo),
                   CommandHandler("total", total),
                   CommandHandler("resumen", resumen),
                   CommandHandler("grafico", grafico),
                   CommandHandler("comparar", comparar),
                   CommandHandler("eliminar", eliminar),
                   CommandHandler("cancelar", cancelar_presupuesto),
                   MessageHandler(filters.COMMAND, cancelar_presupuesto)],
        map_to_parent={}
    )

    consultar_presupuesto_handler = ConversationHandler(
        entry_points=[CommandHandler("consultar", consulta_presupuesto)],
        states={
            ESPERANDO_CATEGORIA_CONSULTA: [
                CallbackQueryHandler(responder_consulta_presupuesto, pattern=r"^consulta_categoria:.+")
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_presupuesto)],
        per_chat=True
    )

    app.add_handler(conv_presupuesto)
    app.add_handler(consultar_presupuesto_handler)
    app.add_handler(gasto_categoria_handler)


    # Agrega handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", mostrar_menu))
    app.add_handler(CommandHandler("resumen", resumen))
    app.add_handler(CommandHandler("total", total))
    app.add_handler(CommandHandler("ultimo", ultimo))
    app.add_handler(CommandHandler("eliminar", eliminar))
    app.add_handler(CommandHandler("grafico", grafico))
    app.add_handler(CommandHandler("comparar", comparar))
    app.add_handler(CommandHandler("comparar_detalle", comparar_categorias))

    
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📋 Menú$"), mostrar_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📝 Registrar gasto$"), handle_message))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📊 Resumen$"), resumen))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📈 Comparar$"), comparar))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^💰 Total$"), total))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📌 Último$"), ultimo))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🗑️ Eliminar$"), eliminar))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📉 Gráfico$"), grafico))
    app.add_handler(MessageHandler(filters.COMMAND, comando_desconocido))  

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_handler(CallbackQueryHandler(iniciar_establecer_presupuesto, pattern=r"^establecer_presupuesto:.+"))

    app.add_handler(CallbackQueryHandler(manejar_menu_inline, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(callback_confirmar))# <= al final como respaldo

    # Inicia el scheduler (trabaja con asyncio)
    app.job_queue.run_daily(
        enviar_resumen_automatico,
        time=time(hour=11, minute=0, tzinfo=pytz.timezone("America/Bogota")),
        days=(6,)  # Solo domingos
    )

    app.job_queue.run_monthly(
        enviar_reporte_mensual,
        when=time(hour=10, minute=0, tzinfo=pytz.timezone("America/Bogota")),
        day=1
    )
    
    async def startup(app):
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("🤖 Webhook eliminado. Bot iniciado.")

    app.post_init = startup
    print("🤖 Bot y programador iniciados.")
    app.run_polling()

if __name__ == "__main__":
    main()

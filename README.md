# Bot de Gastos con Telegram y Firestore

Este bot de Telegram permite registrar gastos y guardarlos en Firebase Firestore.

## Configuración

### 1. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 2. Configurar Firebase

1. **Crear proyecto en Firebase Console:**
   - Ve a https://console.firebase.google.com/
   - Crea un nuevo proyecto o usa uno existente
   - Habilita Firestore Database

2. **Descargar clave de servicio:**
   - En Firebase Console, ve a Configuración del proyecto → Cuentas de servicio
   - Haz clic en "Generar nueva clave privada"
   - Descarga el archivo JSON
   - Renómbralo a `firebase_key.json` y colócalo en la raíz del proyecto

3. **Configurar reglas de Firestore:**
   En Firebase Console, ve a Firestore Database → Reglas y usa:
   ```
   rules_version = '2';
   service cloud.firestore {
     match /databases/{database}/documents {
       match /gastos/{document} {
         allow read, write: if true;
       }
     }
   }
   ```

### 3. Configurar bot de Telegram

1. Crea un bot con @BotFather en Telegram
2. Obtén el token del bot
3. Reemplaza el token en `bot.py` línea 89

### 4. Ejecutar el bot
```bash
python bot.py
```

## Comandos disponibles

- `/start` - Iniciar el bot
- `/resumen` - Ver resumen de gastos por categoría
- `/limpiar` - Eliminar todos los gastos del usuario

## Formato de entrada

Escribe los gastos así: `[monto] [categoría]`

Ejemplos:
- `20000 comida`
- `15000 transporte`
- `50000 servicios`

## Estructura de datos en Firestore

Los gastos se guardan en la colección `gastos` con la siguiente estructura:
```json
{
  "monto": 20000,
  "categoria": "comida",
  "fecha": "2024-01-15T10:30:00",
  "user_id": 123456789
}
``` 
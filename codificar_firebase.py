import base64

with open("firebase_key.json", "rb") as f:
    contenido = f.read()
    base64_str = base64.b64encode(contenido).decode("utf-8")

# Guardar el resultado en un archivo
with open("firebase_key_base64.txt", "w") as out:
    out.write(base64_str)

print("✅ Archivo firebase_key_base64.txt generado.")
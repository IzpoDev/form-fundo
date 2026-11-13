import os
import json
import time
import threading
import traceback
from collections import defaultdict
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel, Field
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# --- CONFIGURACIÓN DESDE ENTORNO ---
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
if not SPREADSHEET_ID:
    raise RuntimeError("Falta la variable de entorno SPREADSHEET_ID")

HOJA_DORSALES = os.getenv("HOJA_DORSALES", "Dorsales")
HOJA_REGISTROS = os.getenv("HOJA_REGISTROS", "Registro")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")

# Desactivamos /docs, /redoc y /openapi.json en producción
app = FastAPI(
    title="Registro de Dorsales Swift",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

lock = threading.Lock()

# Caché en memoria para proteger la cuota de lectura de Google Sheets
cache_dorsales = {"data": None, "timestamp": 0}
CACHE_TTL = 10  # segundos

# Control de spam por IP (máximo 6 intentos por minuto por IP)
intentos_ip = defaultdict(list)

# Conexión a Google Sheets (nube via variable JSON o local via archivo)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
if os.getenv("GOOGLE_CREDENTIALS_JSON"):
    info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
else:
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)

gc = gspread.authorize(creds)
sh = gc.open_by_key(SPREADSHEET_ID)


class RegistroRequest(BaseModel):
    id_upao: str = Field(..., min_length=1, max_length=15)
    nombre_completo: str = Field(..., min_length=2, max_length=80)
    celular: str = Field(..., min_length=6, max_length=15)
    apodo: str = Field(..., min_length=1, max_length=20)
    genero: str = Field(..., max_length=15)
    dorsal: int = Field(..., ge=1, le=999)
    talla: str = Field(..., max_length=10)
    short: str = Field(..., max_length=5)


def normalizar_id(raw_id: str) -> str:
    """Limpia espacios/letras y rellena con ceros a la izquierda hasta 9 dígitos."""
    solo_digitos = "".join(filter(str.isdigit, str(raw_id)))
    return solo_digitos.zfill(9) if solo_digitos else str(raw_id).strip()


def calcular_precio(talla: str, short: str) -> int:
    con_short = short.strip().lower() == "si"
    if not con_short:
        return 39 if talla == "XL" else (41 if talla == "2 XL" else 35)
    return 54 if talla == "XL" else (56 if talla == "2 XL" else 50)


def obtener_dorsales_disponibles(forzar_recarga: bool = False) -> list[int]:
    ahora = time.time()
    if (
        not forzar_recarga
        and cache_dorsales["data"] is not None
        and (ahora - cache_dorsales["timestamp"]) < CACHE_TTL
    ):
        return cache_dorsales["data"]

    ws = sh.worksheet(HOJA_DORSALES)
    valores = ws.col_values(1)[1:]
    dorsales = sorted([int(v.strip()) for v in valores if v.strip().isdigit()])

    cache_dorsales["data"] = dorsales
    cache_dorsales["timestamp"] = ahora
    return dorsales


def buscar_fila_dorsal(ws_dorsales, numero_dorsal: int) -> int | None:
    valores_col = ws_dorsales.col_values(1)
    for idx, val in enumerate(valores_col[1:], start=2):
        if val.strip() == str(numero_dorsal):
            return idx
    return None


@app.get("/", response_class=HTMLResponse)
def servir_formulario():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/camiseta.jpeg")
def servir_imagen():
    return FileResponse("camiseta.jpeg")


@app.get("/api/dorsales")
def listar_dorsales():
    try:
        return {"status": "ok", "dorsales": obtener_dorsales_disponibles()}
    except Exception:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error al consultar dorsales.")


@app.post("/api/registrar")
def registrar_participante(data: RegistroRequest, request: Request):
    # Extraer IP real detrás del proxy de Render o Cloudflare
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    else:
        client_ip = request.headers.get("CF-Connecting-IP") or request.client.host

    ahora = time.time()
    intentos_ip[client_ip] = [t for t in intentos_ip[client_ip] if ahora - t < 60]
    if len(intentos_ip[client_ip]) >= 6:
        raise HTTPException(
            status_code=429,
            detail="Demasiados intentos. Espera un minuto antes de volver a intentar.",
        )
    intentos_ip[client_ip].append(ahora)

    with lock:
        try:
            ws_dorsales = sh.worksheet(HOJA_DORSALES)
            ws_registros = sh.worksheet(HOJA_REGISTROS)

            id_limpio = normalizar_id(data.id_upao)

            # 1. Buscar si el ID UPAO ya existe en la pestaña "Registro"
            todos_registros = ws_registros.get_all_values()
            fila_existente = None
            dorsal_anterior = None

            for idx, fila in enumerate(todos_registros[1:], start=2):
                if len(fila) > 0 and normalizar_id(fila[0]) == id_limpio:
                    fila_existente = idx
                    if len(fila) > 3 and fila[3].strip().isdigit():
                        dorsal_anterior = int(fila[3].strip())
                    break

            # 2. Verificar disponibilidad del nuevo dorsal
            fila_nuevo_dorsal = buscar_fila_dorsal(ws_dorsales, data.dorsal)
            if not fila_nuevo_dorsal:
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "taken",
                        "message": f"El dorsal #{data.dorsal} acaba de ser ocupado. Elige otro.",
                        "dorsales": obtener_dorsales_disponibles(forzar_recarga=True),
                    },
                )

            # 3. Ocupar el nuevo dorsal (eliminarlo de "Dorsales")
            ws_dorsales.delete_rows(fila_nuevo_dorsal)

            # Orden exacto A:H -> id | nombre completo | apodo | dorsal | short | talla | sexo | celular
            fila_datos = [
                id_limpio,
                data.nombre_completo.strip(),
                data.apodo.strip().upper(),
                data.dorsal,
                data.short,
                data.talla,
                data.genero,
                data.celular.strip(),
            ]
            precio = calcular_precio(data.talla, data.short)

            # 4. Si el ID ya existía -> Actualizar fila y liberar el dorsal anterior
            if fila_existente:
                ws_registros.update(
                    range_name=f"A{fila_existente}:H{fila_existente}",
                    values=[fila_datos],
                    value_input_option="RAW",
                )

                msg_dorsal = f"Dorsal #{data.dorsal} asignado."
                if dorsal_anterior is not None and dorsal_anterior != data.dorsal:
                    ws_dorsales.append_row([dorsal_anterior], value_input_option="RAW")
                    msg_dorsal = f"Se cambió tu dorsal #{dorsal_anterior} por el #{data.dorsal} (el #{dorsal_anterior} volvió a estar disponible)."

                cache_dorsales["timestamp"] = 0

                return {
                    "status": "ok",
                    "message": f"¡Datos actualizados para el ID {id_limpio}! {msg_dorsal} Total: S/ {precio}.00.",
                }

            # 5. Si es un ID nuevo -> Insertar en la siguiente fila libre
            siguiente_fila = sum(1 for f in todos_registros if len(f) > 0 and f[0].strip() != "") + 1
            ws_registros.update(
                range_name=f"A{siguiente_fila}:H{siguiente_fila}",
                values=[fila_datos],
                value_input_option="RAW",
            )

            cache_dorsales["timestamp"] = 0

            return {
                "status": "ok",
                "message": f"¡Registro exitoso (ID {id_limpio})! Dorsal #{data.dorsal} reservado (Total: S/ {precio}.00).",
            }

        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")
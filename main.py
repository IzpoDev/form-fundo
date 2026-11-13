import os
import json
import threading
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
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

app = FastAPI(title="Registro de Dorsales Swift")
lock = threading.Lock()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
if os.getenv("GOOGLE_CREDENTIALS_JSON"):
    info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
else:
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)

gc = gspread.authorize(creds)
sh = gc.open_by_key(SPREADSHEET_ID)


class RegistroRequest(BaseModel):
    id_upao: str
    nombre_completo: str
    celular: str
    apodo: str
    genero: str
    dorsal: int
    talla: str
    short: str


def normalizar_id(raw_id: str) -> str:
    """Limpia espacios/letras y rellena con ceros a la izquierda hasta 9 dígitos."""
    solo_digitos = "".join(filter(str.isdigit, str(raw_id)))
    return solo_digitos.zfill(9) if solo_digitos else str(raw_id).strip()


def calcular_precio(talla: str, short: str) -> int:
    con_short = short.strip().lower() == "si"
    if not con_short:
        return 39 if talla == "XL" else (41 if talla == "2 XL" else 35)
    return 54 if talla == "XL" else (56 if talla == "2 XL" else 50)


def obtener_dorsales_disponibles() -> list[int]:
    ws = sh.worksheet(HOJA_DORSALES)
    valores = ws.col_values(1)[1:]
    return sorted([int(v.strip()) for v in valores if v.strip().isdigit()])


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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/registrar")
def registrar_participante(data: RegistroRequest):
    with lock:
        try:
            ws_dorsales = sh.worksheet(HOJA_DORSALES)
            ws_registros = sh.worksheet(HOJA_REGISTROS)

            id_limpio = normalizar_id(data.id_upao)

            # 1. Buscar si el ID UPAO ya existe (normalizando también lo leído en la hoja)
            todos_registros = ws_registros.get_all_values()
            fila_existente = None
            dorsal_anterior = None

            for idx, fila in enumerate(todos_registros[1:], start=2):
                if len(fila) > 0 and normalizar_id(fila[0]) == id_limpio:
                    fila_existente = idx
                    if len(fila) > 3 and fila[3].strip().isdigit():
                        dorsal_anterior = int(fila[3].strip())
                    break

            # 2. Verificar que el nuevo dorsal elegido esté disponible
            fila_nuevo_dorsal = buscar_fila_dorsal(ws_dorsales, data.dorsal)
            if not fila_nuevo_dorsal:
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "taken",
                        "message": f"El dorsal #{data.dorsal} acaba de ser ocupado. Elige otro.",
                        "dorsales": obtener_dorsales_disponibles(),
                    },
                )

            # 3. Ocupar el nuevo dorsal
            ws_dorsales.delete_rows(fila_nuevo_dorsal)

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

            # 4. Actualizar fila si el ID ya existía (RAW conserva los ceros iniciales)
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

                return {
                    "status": "ok",
                    "message": f"¡Datos actualizados para el ID {id_limpio}! {msg_dorsal} Total: S/ {precio}.00.",
                }

            # 5. Insertar nuevo registro
            siguiente_fila = len(ws_registros.col_values(1)) + 1
            ws_registros.update(
                range_name=f"A{siguiente_fila}:H{siguiente_fila}",
                values=[fila_datos],
                value_input_option="RAW",
            )

            return {
                "status": "ok",
                "message": f"¡Registro exitoso (ID {id_limpio})! Dorsal #{data.dorsal} reservado (Total: S/ {precio}.00).",
            }

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")
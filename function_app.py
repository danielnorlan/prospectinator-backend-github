# Test commit med push



from __future__ import annotations
import os
import re
import json
import time
import logging
import tempfile
import asyncio
import httpx
from datetime import datetime
from pathlib import Path
from collections import deque

import pandas as pd
import azure.functions as func
from azure.storage.blob import BlobServiceClient, ContainerClient
from azure.core.exceptions import ResourceNotFoundError, ResourceExistsError

# -------------------------------------------------------------------
# TOKEN‐BUCKET RATE‐LIMITER: maks 490 kall per 60 sek
# -------------------------------------------------------------------
class TokenBucketLimiter:
    def __init__(self, capacity: int, period: float):
        self.capacity   = capacity
        self.tokens     = capacity
        self.fill_rate  = capacity / period
        self.last_time  = time.monotonic()
        self.lock       = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            now   = time.monotonic()
            delta = now - self.last_time
            self.tokens = min(self.capacity, self.tokens + delta * self.fill_rate)
            self.last_time = now

            if self.tokens >= 1:
                self.tokens -= 1
                return
            wait = (1 - self.tokens) / self.fill_rate

        await asyncio.sleep(wait)
        return await self.acquire()

rate_limiter = TokenBucketLimiter(capacity=490, period=60.0)

# -------------------------------------------------------------------
# KONFIG & KONSTANTER
# -------------------------------------------------------------------
app = func.FunctionApp()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PPLX_KEY   = os.getenv("PPLX_API_KEY")
PPLX_MODEL = os.getenv("PPLX_MODEL", "sonar-pro")
if not PPLX_KEY:
    raise RuntimeError("Env var PPLX_API_KEY is missing")

HEADERS = {
    "Authorization": f"Bearer {PPLX_KEY}",
    "Content-Type": "application/json",
}

COMPANY_ALIASES = ["juridisk selskapsnavn","selskapsnavn","bedrift","company","firma","firmanavn"]
ORGNR_ALIASES   = ["organisasjonsnummer","orgnr","org.nr","org nr","organisasjons nr"]
PERSON_ALIASES  = ["kontaktperson","person","navn","name","kontakt person"]
PHONE_ALIASES   = ["telefon","telefonnummer","mobil","mobilnummer","tlf","kontaktperson telefon","proff telefon"]

TARGET_COL = "Prospectinator(TLF.NR)"
SOURCE_COL = "Prospectinator(KILDE)"

PHONE_RE = re.compile(r"(?:0047|\+47)?\D*(\d{8})\b")
URL_RE   = re.compile(r"(https?://[^\s\)]+|www\.[^\s\)]+)")

# Hev concurrency‐nivået
semaphore = asyncio.Semaphore(100)

# -------------------------------------------------------------------
# HJELPEFUNKSJONER
# -------------------------------------------------------------------
def find_column(cols: list[str], aliases: list[str]) -> str | None:
    low = [c.lower() for c in cols]
    for alias in aliases:
        for idx, name in enumerate(low):
            if alias in name:
                return cols[idx]
    return None

def extract_phone(text: str | None) -> str | None:
    if not text: return None
    m = PHONE_RE.search(str(text))
    return m.group(1) if m else None

def normalize_phone(phone: str | None) -> str:
    if not phone: return ""
    s = str(phone).strip()
    try:
        return str(int(float(s)))
    except:
        return s

# -------------------------------------------------------------------
# ASYNC API‐KALL m/ ROBUST KILDE‐HENTING & RATE‐LIMIT
# -------------------------------------------------------------------
async def async_ask_perplexity(company: str, person: str) -> tuple[str | None, str]:
    # 1) Rate‐limiter
    await rate_limiter.acquire()

    # 2) Bygg prompt
    prompt = (
        f"Hva er MOBILNUMMERET (8 sifre) til {person} som jobber i {company} i Norge?\n"
        "Svar på to linjer:\n"
        "1) Kun åtte sammenhengende sifre (ingen +47, ingen tekst)\n"
        "2) Én URL-kilde\n"
    )
    headers_req = HEADERS.copy()
    headers_req["Connection"] = "close"

    # 3) Send request med lengre timeout (60s)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0)
        ) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers=headers_req,
                json={
                    "model": PPLX_MODEL,
                    "temperature": 0.4,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
    except Exception as e:
        logging.warning("API‐feil for %s/%s: %s", company, person, e)
        return None, ""

    # 4) Parse svar
    js      = resp.json()
    content = js.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    lines   = [l.strip() for l in content.splitlines() if l.strip()]

    # 5) Nummer fra første linje
    number = extract_phone(lines[0]) if lines else None

    # 6) Kilde‐URL fra linje 2 dersom present, ellers fra citations, ellers regex
    src = ""
    if len(lines) >= 2 and lines[1].startswith(("http://","https://","www.")):
        src = lines[1]
    else:
        cites = js.get("citations") or []
        if cites:
            src = cites[0]
        else:
            m = URL_RE.search(content)
            if m:
                src = m.group(1)

    # 7) Dersom vi har nummer men ingen src, merk “Ingen kilde”
    if number and not src:
        src = "Ingen kilde"

    return number, src

# -------------------------------------------------------------------
# PROSESS ÉN RAD
# -------------------------------------------------------------------
async def process_row(idx: int, row: pd.Series,
                      comp_col: str, pers_col: str, fb_phone_col: str):
    company = str(row.get(comp_col, "")).strip()
    person  = str(row.get(pers_col, "")).strip()

    async with semaphore:
        try:
            num, src = await asyncio.wait_for(
                async_ask_perplexity(company, person), timeout=60
            )
        except asyncio.TimeoutError:
            logging.error("Timeout på rad %d", idx)
            num, src = None, ""

    if not num:
        fb = row.get(fb_phone_col, "")
        try:
            num = str(int(float(fb)))
        except:
            num = str(fb).strip()
        src = "Ingen kilde"

    return idx, normalize_phone(num), src or ""

# -------------------------------------------------------------------
# BLOB‐TRIGGER: PROCESS XLSX
# -------------------------------------------------------------------
@app.function_name(name="process_xlsx_v2")
@app.blob_trigger(arg_name="blob", path="uploads/{name}", connection="APP_STORAGE_CONNECTION")
async def process_xlsx_v2(blob: func.InputStream):
    logging.info("▶ Starter prosessering: %s", blob.name)

    # Les inn DataFrame
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        tmp.write(blob.read()); tmp.flush()
        df = pd.read_excel(tmp.name)
    cols = list(df.columns)

    # Finn kolonner
    comp_col     = find_column(cols, COMPANY_ALIASES)
    orgnr_col    = find_column(cols, ORGNR_ALIASES)
    pers_col     = find_column(cols, PERSON_ALIASES)
    fb_phone_col = find_column(cols, PHONE_ALIASES) or ""

    if not comp_col or not orgnr_col or not pers_col:
        raise ValueError(f"Mangler kolonner: firma={comp_col}, orgnr={orgnr_col}, kontakt={pers_col}")

    # Init resultat‐kolonner
    for c in (TARGET_COL, SOURCE_COL):
        if c not in df.columns:
            df[c] = ""

    # Filtrer blanke rader (stopp ved 3 på rad)
    rows, empty = [], 0
    for idx, row in df.iterrows():
        if pd.isna(row.get(comp_col)) and pd.isna(row.get(pers_col)):
            empty += 1
            if empty >= 3:
                break
        else:
            empty = 0
            rows.append((idx, row))

    # BlobService + sikre containere
    svc = BlobServiceClient.from_connection_string(os.getenv("APP_STORAGE_CONNECTION"))
    for container in ("uploads","progress","results"):
        cli = svc.get_container_client(container)
        try:   cli.create_container()
        except ResourceExistsError: pass

    # Initial progress‐blob
    prog_name = Path(blob.name).stem + "_progress.json"
    prog_cli  = svc.get_blob_client("progress", prog_name)
    prog_cli.upload_blob(json.dumps({
        "processed": 0,
        "total": len(rows),
        "percentage": 0,
        "lastUpdate": datetime.utcnow().isoformat()+"Z"
    }), overwrite=True)

    # Kjør alle rader parallelt
    results = {}
    tasks = [asyncio.create_task(process_row(i, r, comp_col, pers_col, fb_phone_col))
             for i, r in rows]
    for coro in asyncio.as_completed(tasks):
        idx, num, src = await coro
        results[idx] = (num, src)
        done = len(results)
        prog_cli.upload_blob(json.dumps({
            "processed": done,
            "total": len(rows),
            "percentage": round(done/len(rows)*100,2),
            "lastUpdate": datetime.utcnow().isoformat()+"Z"
        }), overwrite=True)

    # Skriv tilbake i DF
    for idx, (num, src) in results.items():
        df.at[idx, TARGET_COL] = num
        df.at[idx, SOURCE_COL] = src

    # Re‐rekkefølge kolonner
    fixed = [comp_col, orgnr_col, pers_col, TARGET_COL, SOURCE_COL]
    rest  = [c for c in df.columns if c not in fixed]
    df = df[fixed + rest]

    # Skriv ut ny Excel med auto‐bredde
    out_tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    out_name = Path(blob.name).stem + "_processed.xlsx"
    try:
        df.to_excel(out_tmp.name, index=False, sheet_name="Prospectinator")
        from openpyxl import load_workbook
        wb = load_workbook(out_tmp.name)
        ws = wb["Prospectinator"]
        for cols_cells in ws.columns:
            lengths = [len(str(cell.value or "")) for cell in cols_cells]
            ws.column_dimensions[cols_cells[0].column_letter].width = max(lengths) + 2
        wb.save(out_tmp.name)
        logging.info("✔ Excel lagret med auto‐bredde: %s", out_tmp.name)
    except Exception:
        logging.exception("❌ Feilet generering av Excel")
        raise

    # Last opp til results, slett gammel først
    try:
        res_cli = svc.get_blob_client("results", out_name)
        try:   res_cli.delete_blob()
        except ResourceNotFoundError: pass
        with open(out_tmp.name, "rb") as fh:
            res_cli.upload_blob(fh, overwrite=True)
        logging.info("✔ Lastet opp prosessert fil: results/%s", out_name)
    except Exception:
        logging.exception("❌ Feilet opplasting av prosessert fil")
        raise

# -------------------------------------------------------------------
# HTTP‐ENDPOINTS (UploadTest, DownloadFile, GetProgress)
# -------------------------------------------------------------------
@app.function_name(name="UploadTest")
@app.route(route="UploadTest", auth_level=func.AuthLevel.FUNCTION, methods=["POST"])
def UploadTest(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("⏳ UploadTest mottok forespørsel")
    try:
        file = req.files.get("file")
    except:
        file = None
    if not file:
        return func.HttpResponse("❌ No file received ('file'-feltet mangler).", status_code=400)
    try:
        tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx").name
        with open(tmp_path, "wb") as t: t.write(file.read())
        svc    = BlobServiceClient.from_connection_string(os.getenv("APP_STORAGE_CONNECTION"))
        up_cli = svc.get_blob_client("uploads", Path(file.filename).name)
        with open(tmp_path, "rb") as data:
            up_cli.upload_blob(data, overwrite=True)
        logging.info("✔ Lastet opp til 'uploads': %s", file.filename)
        old = Path(file.filename).stem + "_processed.xlsx"
        rm  = svc.get_blob_client("results", old)
        try: rm.delete_blob()
        except ResourceNotFoundError: pass
        return func.HttpResponse(f"✅ File {file.filename} uploaded!", status_code=200)
    except Exception:
        logging.exception("❌ Upload-feil")
        return func.HttpResponse("❌ Internal server error.", status_code=500)

@app.function_name(name="DownloadFile")
@app.route(route="DownloadFile", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def DownloadFile(req: func.HttpRequest) -> func.HttpResponse:
    filename = req.params.get("filename")
    if not filename:
        return func.HttpResponse("Missing filename parameter.", status_code=400)
    try:
        svc  = BlobServiceClient.from_connection_string(os.getenv("APP_STORAGE_CONNECTION"))
        blob = svc.get_blob_client("results", filename)
        if not blob.exists():
            return func.HttpResponse("File not found.", status_code=404)
        data = blob.download_blob().readall()
        headers = {
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        return func.HttpResponse(data, status_code=200, headers=headers)
    except Exception:
        logging.exception("❌ Download-feil")
        return func.HttpResponse("❌ Internal server error.", status_code=500)

@app.function_name(name="GetProgress")
@app.route(route="Progress", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def GetProgress(req: func.HttpRequest) -> func.HttpResponse:
    filename = req.params.get("filename")
    if not filename:
        return func.HttpResponse("Missing filename parameter.", status_code=400)
    try:
        svc       = BlobServiceClient.from_connection_string(os.getenv("APP_STORAGE_CONNECTION"))
        prog_name = Path(filename).stem + "_progress.json"
        blob_cli  = svc.get_blob_client("progress", prog_name)
        if not blob_cli.exists():
            return func.HttpResponse("Progress not started yet.", status_code=404)
        return func.HttpResponse(blob_cli.download_blob().readall(),
                                 status_code=200, mimetype="application/json")
    except Exception:
        logging.exception("❌ Progress-feil")
        return func.HttpResponse("❌ Internal server error.", status_code=500)

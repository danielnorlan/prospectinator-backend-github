from __future__ import annotations
import cgi
import io
import json
import logging
import os
import re
import tempfile
import asyncio
import httpx
from datetime import datetime
from pathlib import Path

import pandas as pd
import azure.functions as func
from azure.storage.blob import BlobServiceClient
# -------- NYTT: brukes for å ignorere «blob finnes ikke» ved sletting
from azure.core.exceptions import ResourceNotFoundError

# ---------------------------------------------------------------------------
#  KONFIG
# ---------------------------------------------------------------------------
app = func.FunctionApp()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PPLX_KEY   = os.getenv("PPLX_API_KEY")
PPLX_MODEL = os.getenv("PPLX_MODEL", "sonar-pro")
if not PPLX_KEY:
    raise RuntimeError("Env var PPLX_API_KEY is missing")

HEADERS = {"Authorization": f"Bearer {PPLX_KEY}", "Content-Type": "application/json"}

PHONE_RE   = re.compile(r"(?:0047|\+47)?\D*(\d{8})\b")
URL_RE     = re.compile(r"https?://[^\s>\]]+")
COMPANY_COLS = ["Juridisk selskapsnavn","Selskapsnavn","Bedrift","Company","Firma"]
PERSON_COLS  = ["Navn","Person","Kontaktperson","Name"]
TARGET_COL   = "Prospectinator(TLF.NR)"
SOURCE_COL   = "Prospectinator(KILDE)"
FALLBACK_COL = "Proff Telefon"

def extract_phone(text: str | None) -> str | None:
    if not text: return None
    m = PHONE_RE.search(str(text));  return m.group(1) if m else None

def normalize_phone(phone: str | None) -> str:
    if not phone: return ""
    phone_str = str(phone).strip()
    try:
        num = int(float(phone_str))
        return str(num)
    except Exception:
        return phone_str

# ---------------------------------------------------------------------------
#  AI-kall (uendret)
# ---------------------------------------------------------------------------
semaphore = asyncio.Semaphore(5)

async def async_ask_perplexity(company: str, person: str) -> tuple[str | None, str]:
    prompt = (
        f"Hva er MOBILNUMMERET (8 sifre) til {person} som jobber i {company} i Norge?\n"
        "Svar på to linjer:\n"
        "1) Kun åtte sammenhengende sifre (ingen +47, ingen tekst)\n"
        "2) Én URL-kilde\n"
    )
    headers_req = HEADERS.copy();  headers_req["Connection"] = "close"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0)) as client:
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
    except httpx.TimeoutException:
        logging.error("Timeout på API-kall for %s/%s", company, person)
        return None, ""
    except Exception as e:
        logging.warning("Feil i API-kall for %s/%s: %s", company, person, e)
        return None, ""

    js   = resp.json()
    text = js.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    first_line = text.splitlines()[0] if text else ""
    number     = extract_phone(first_line)
    cites      = js.get("citations") or []
    source     = cites[0] if cites else ""
    if not source:
        m = URL_RE.search(text)
        source = m.group(0) if m else ""
    return number, source

async def process_row(idx: int, row, comp_col: str, pers_col: str):
    company = str(row.get(comp_col, "")).strip()
    person  = str(row.get(pers_col, "")).strip()
    start   = asyncio.get_event_loop().time()
    async with semaphore:
        try:
            ai_num, ai_src = await asyncio.wait_for(
                async_ask_perplexity(company, person), timeout=30
            )
        except asyncio.TimeoutError:
            logging.error("Timeout rad %s", idx);  ai_num, ai_src = None, ""
    dur = asyncio.get_event_loop().time() - start

    if not ai_num or str(ai_num).strip().lower() == "nan":
        fallback_val = row.get(FALLBACK_COL, "")
        try:
            ai_num = str(int(float(fallback_val)))
        except Exception:
            ai_num = str(fallback_val).strip()
        if ai_num.lower() in ["proff telefon", "nan", ""]:
            ai_num = ""
        ai_src = "Ingen kilde"
    else:
        ai_num = normalize_phone(ai_num)

    if ai_num and str(ai_num).isdigit():
        ai_num = int(ai_num)

    return idx, ai_num, ai_src, dur

# ---------------------------------------------------------------------------
#  BLOB-TRIGGER  –  PROSESSERING
# ---------------------------------------------------------------------------
@app.function_name(name="process_xlsx_v1")
@app.blob_trigger(arg_name="blob", path="uploads/{name}", connection="AzureWebJobsStorage")
async def process_xlsx_v1(blob: func.InputStream):
    logging.info("▶ Processing %s", blob.name)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        tmp.write(blob.read());  tmp.flush()
        df = pd.read_excel(tmp.name)

    # ----- (kolonneopprydding som før) -----
    drop_cols = [c for c in df.columns if c.startswith("Unnamed") or (c.lower().startswith("kilder") and c!=SOURCE_COL)]
    if drop_cols: df.drop(columns=drop_cols, inplace=True)
    for col in (TARGET_COL, SOURCE_COL):
        if col not in df.columns: df[col] = ""
    comp_col = next((c for c in COMPANY_COLS if c in df.columns), None)
    pers_col = next((c for c in PERSON_COLS  if c in df.columns), None)
    if not comp_col or not pers_col:
        raise ValueError("Fant ingen company/person-kolonner.")

    # ----- filtrér rader -----
    rows_to_proc, empty = [], 0
    for idx, row in df.iterrows():
        if pd.isna(row.get(comp_col)) and pd.isna(row.get(pers_col)):
            empty += 1
            if empty >= 3: break
        else:
            empty = 0;  rows_to_proc.append((idx, row))

    # ----- opprett blob-klienter -----
    conn_str = os.getenv("AzureWebJobsStorage")
    svc      = BlobServiceClient.from_connection_string(conn_str)
    progress_blob = Path(blob.name).stem + "_progress.json"
    prog_cli = svc.get_blob_client("progress", progress_blob)
    prog_cli.upload_blob(json.dumps({"processed":0,"total":len(rows_to_proc),"percentage":0,
                                     "lastUpdate":datetime.utcnow().isoformat()+"Z"}), overwrite=True)

    # -----------------------------------------------------------------------
    #  KJØR AI-OPPGAVER
    # -----------------------------------------------------------------------
    results = {}
    for coro in asyncio.as_completed([asyncio.create_task(process_row(i,r,comp_col,pers_col))
                                      for i,r in rows_to_proc]):
        idx, num, src, _ = await coro
        results[idx] = (num, src)

        done = len(results)
        prog_cli.upload_blob(
            json.dumps({"processed":done,"total":len(rows_to_proc),
                        "percentage":round(done/len(rows_to_proc)*100,2) if rows_to_proc else 100,
                        "lastUpdate":datetime.utcnow().isoformat()+"Z"}),
            overwrite=True,
        )

    for i,(n,s) in results.items():
        df.at[i, TARGET_COL] = n if n else ""
        df.at[i, SOURCE_COL] = s or "Ingen kilde"

    try: df[TARGET_COL] = pd.to_numeric(df[TARGET_COL], errors="ignore")
    except Exception as e: logging.warning("Kunne ikke konvertere %s – %s", TARGET_COL, e)

    # ----- skrive fil -----
    out_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    df.to_excel(out_tmp.name, index=False)

    out_name = Path(blob.name).stem + "_processed.xlsx"
    res_cli  = svc.get_blob_client("results", out_name)

    # ----------  NYTT  ----------  
    #   1. Sørg for at eventuell gammel resultatfil SLETTES FØR ny prosessering.
    #      Dermed kan ikke frontend hente gammel fil mens ny beregnes.
    try:
        res_cli.delete_blob()
        logging.info("Slettet gammel results/%s", out_name)
    except ResourceNotFoundError:
        pass  # ingen gammel fil – helt greit

    #   2. Last alltid opp med SAMME navn (overwrite=True).  
    #      Ingen tidsstempel – siste kjøring «vinner».
    with open(out_tmp.name, "rb") as fh:
        res_cli.upload_blob(fh, overwrite=True)
    logging.info("✔ Lagret results/%s", out_name)

# ---------------------------------------------------------------------------
#  HTTP-ENDPOINT: UPLOAD
# ---------------------------------------------------------------------------
@app.function_name(name="UploadTest")
@app.route(route="UploadTest", auth_level=func.AuthLevel.FUNCTION, methods=["POST"])
def UploadTest(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("⏳ UploadTest mottok forespørsel")
    # ----- les fil -----
    try:
        file = req.files.get("file")
    except Exception:
        file = None
    if not file:
        return func.HttpResponse("❌ No file received ('file'-feltet mangler).", status_code=400)

    try:
        tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx").name
        with open(tmp_path, "wb") as tmp:
            tmp.write(file.read())

        conn_str = os.getenv("AzureWebJobsStorage")
        svc      = BlobServiceClient.from_connection_string(conn_str)

        blob_name = Path(file.filename).name
        upload_cli = svc.get_blob_client("uploads", blob_name)
        with open(tmp_path, "rb") as data:
            upload_cli.upload_blob(data, overwrite=True)
        logging.info("✔ %s lastet opp til 'uploads'", blob_name)

        # ----------  NYTT  ----------  
        #   Slett eventuell gammel _processed-fil med samme basisnavn
        processed_name = Path(blob_name).stem + "_processed.xlsx"
        res_cli = svc.get_blob_client("results", processed_name)
        try:
            res_cli.delete_blob()
            logging.info("Slettet gammel results/%s", processed_name)
        except ResourceNotFoundError:
            pass

        return func.HttpResponse(f"✅ File {blob_name} uploaded!", status_code=200)

    except Exception as e:
        logging.error("❌ Upload-feil: %s", e)
        return func.HttpResponse("❌ Internal server error.", status_code=500)

# ---------------------------------------------------------------------------
#  HTTP-ENDPOINT: DOWNLOAD
# ---------------------------------------------------------------------------
@app.function_name(name="DownloadFile")
@app.route(route="DownloadFile", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def DownloadFile(req: func.HttpRequest) -> func.HttpResponse:
    filename = req.params.get("filename")
    if not filename:
        return func.HttpResponse("Missing filename parameter.", status_code=400)
    try:
        svc = BlobServiceClient.from_connection_string(os.getenv("AzureWebJobsStorage"))
        blob = svc.get_blob_client("results", filename)
        if not blob.exists():
            return func.HttpResponse("File not found.", status_code=404)
        data = blob.download_blob().readall()
        headers = {
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        return func.HttpResponse(data, status_code=200, headers=headers)
    except Exception as e:
        logging.error("Download-feil: %s", e)
        return func.HttpResponse("Internal server error.", status_code=500)

# ---------------------------------------------------------------------------
#  HTTP-ENDPOINT: PROGRESS (uendret)
# ---------------------------------------------------------------------------
@app.function_name(name="GetProgress")
@app.route(route="Progress", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def GetProgress(req: func.HttpRequest) -> func.HttpResponse:
    filename = req.params.get("filename")
    if not filename:
        return func.HttpResponse("Missing filename parameter.", status_code=400)
    try:
        prog_blob = Path(filename).stem + "_progress.json"
        svc       = BlobServiceClient.from_connection_string(os.getenv("AzureWebJobsStorage"))
        blob_cli  = svc.get_blob_client("progress", prog_blob)
        if not blob_cli.exists():
            return func.HttpResponse("Progress not started yet.", status_code=404)
        return func.HttpResponse(blob_cli.download_blob().readall(),
                                 status_code=200, mimetype="application/json")
    except Exception as e:
        logging.error("Progress-feil: %s", e)
        return func.HttpResponse("Internal server error.", status_code=500)

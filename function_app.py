# ------------------------  AZURE FUNCTIONS APP  -------------------------
# Re-skrevet 2025-06-23 – håndterer nye Proff-lister, reordner kolonner
from __future__ import annotations
import asyncio, cgi, httpx, io, json, logging, os, re, tempfile
from datetime import datetime
from pathlib import Path


import pandas as pd
import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

# ------------------------  GLOBAL KONFIG  -------------------------------
app = func.FunctionApp()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PPLX_KEY   = os.getenv("PPLX_API_KEY")
PPLX_MODEL = os.getenv("PPLX_MODEL", "sonar-pro")
if not PPLX_KEY:
    raise RuntimeError("Env var PPLX_API_KEY is missing")

HEADERS = {"Authorization": f"Bearer {PPLX_KEY}", "Content-Type": "application/json"}

PHONE_RE = re.compile(r"(?:0047|\+47)?\D*(\d{8})\b")
URL_RE   = re.compile(r"https?://[^\s>\]]+")

# — kolonnenavn (regex-basert) —
REGEX_FIRMA = re.compile(r"^(firma|bedrift|selskapsnavn|company|navn)", re.I)
REGEX_ORGNR = re.compile(r"(org(\.|nr)?|organisasjons?nr)", re.I)
REGEX_TLF   = re.compile(r"(telefon|phone|tlf|mobil|proff ?telefon)", re.I)

PROSPECT_NR_COL  = "PROSPECTINATOR (TLF)"
PROSPECT_SRC_COL = "PROSPECTINATOR (KILDE)"
FALLBACK_COL     = "Proff Telefon"

# --------------------  HJELPER-FUNKSJONER (uendret) ---------------------
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

# ------------------------  AI-KALL (uendret) ----------------------------
semaphore = asyncio.Semaphore(5)

async def async_ask_perplexity(company: str, person: str) -> tuple[str | None, str]:
    prompt = (f"Hva er MOBILNUMMERET (8 sifre) til {person} som jobber i {company} i Norge?\n"
              "Svar på to linjer:\n1) Kun åtte sammenhengende sifre\n2) Én URL-kilde\n")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15)) as cli:
            r = await cli.post(
                "https://api.perplexity.ai/chat/completions",
                headers=HEADERS,
                json={"model": PPLX_MODEL, "temperature": 0.4,
                      "messages":[{"role":"user","content":prompt}]})
            r.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logging.error("AI-feil %s/%s: %s", company, person, e)
        return None, ""
    js = r.json()
    text   = js.get("choices", [{}])[0].get("message", {}).get("content", "")
    number = extract_phone(text.splitlines()[0] if text else "")
    cites  = js.get("citations") or []
    source = cites[0] if cites else (URL_RE.search(text).group(0) if URL_RE.search(text) else "")
    return number, source

async def process_row(idx: int, row, comp_col: str, pers_col: str):
    company, person = str(row.get(comp_col,"")).strip(), str(row.get(pers_col,"")).strip()
    async with semaphore:
        try:
            ai_num, ai_src = await asyncio.wait_for(async_ask_perplexity(company, person), timeout=30)
        except asyncio.TimeoutError:
            ai_num, ai_src = None, ""
    if not ai_num:
        fallback = normalize_phone(row.get(FALLBACK_COL, ""))
        ai_num = fallback if fallback.isdigit() else ""
        ai_src = "Ingen kilde"
    else:
        ai_num = normalize_phone(ai_num)
    return idx, ai_num, ai_src

# ------------------  BLOB-TRIGGER: XLSX-PROSESSERING --------------------
@app.function_name(name="process_xlsx_v1")
@app.blob_trigger(arg_name="blob",
                  path="uploads/{name}",
                  connection="AzureWebJobsStorage")
async def process_xlsx_v1(blob: func.InputStream):
    logging.info("▶ Processing %s", blob.name)

    # --- les inn i pandas ---
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    tmp.write(blob.read()); tmp.flush()
    df = pd.read_excel(tmp.name, engine="openpyxl")

    # --- finn kolonner dynamisk ---
    cols_lower = [c.lower().strip() for c in df.columns]
    def find(regex): 
        for i,c in enumerate(cols_lower):
            if regex.search(c): return df.columns[i]
        return None
    col_firma, col_orgnr, col_tlf = find(REGEX_FIRMA), find(REGEX_ORGNR), find(REGEX_TLF)
    missing = [n for n,v in [("Firmanavn",col_firma),("Org.nr",col_orgnr),("Telefon",col_tlf)] if v is None]
    if missing:
        raise ValueError("Mangler kolonner: " + ", ".join(missing))

    # --- sørg for prospector-kolonner ---
    for c in (PROSPECT_NR_COL, PROSPECT_SRC_COL):
        if c not in df.columns: df[c] = ""

    # --- reordne kolonne-rekkefølge ---
    new_order = [col_firma, col_orgnr, col_tlf, PROSPECT_NR_COL, PROSPECT_SRC_COL]
    new_order += [c for c in df.columns if c not in new_order]
    df = df.reindex(columns=new_order)

    # --- filtrér rader å kjøre AI på (stopp etter 3 tomme) ---
    work_rows, empty = [], 0
    for idx,row in df.iterrows():
        if pd.isna(row.get(col_firma)) and pd.isna(row.get(col_tlf)):
            empty += 1
            if empty >= 3: break
        else:
            empty = 0; work_rows.append((idx,row))

    # --- blob-klient for progress/results ---
    svc   = BlobServiceClient.from_connection_string(os.getenv("AzureWebJobsStorage"))
    prog  = svc.get_blob_client("progress", Path(blob.name).stem+"_progress.json")
    total = len(work_rows)
    prog.upload_blob(json.dumps({"processed":0,"total":total,"percentage":0,
                                 "lastUpdate":datetime.utcnow().isoformat()+"Z"}), overwrite=True)

    # --- kjør AI parallelt ---
    tasks = [asyncio.create_task(process_row(i,r,col_firma,col_tlf)) for i,r in work_rows]
    done = 0
    for coro in asyncio.as_completed(tasks):
        idx, num, src = await coro
        df.at[idx, PROSPECT_NR_COL]  = num
        df.at[idx, PROSPECT_SRC_COL] = src
        done += 1
        prog.upload_blob(json.dumps({"processed":done,"total":total,
                                     "percentage":round(done/total*100,2),
                                     "lastUpdate":datetime.utcnow().isoformat()+"Z"}),
                         overwrite=True)

    # --- skriv til results ---
    out_name = Path(blob.name).stem + "_processed.xlsx"
    res_cli  = svc.get_blob_client("results", out_name)
    try: res_cli.delete_blob()
    except ResourceNotFoundError: pass
    buffer = io.BytesIO()
    df.to_excel(buffer, index=False, engine="openpyxl"); buffer.seek(0)
    res_cli.upload_blob(buffer, overwrite=True)
    logging.info("✔ Lagret results/%s", out_name)

# ---------------------------  UPLOAD HTTP  ------------------------------
@app.function_name(name="UploadTest")
@app.route(route="UploadTest", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def upload_file(req: func.HttpRequest) -> func.HttpResponse:
    try:       file = req.files.get("file")
    except:    file = None
    if not file:
        return func.HttpResponse("no file", status_code=400)
    svc = BlobServiceClient.from_connection_string(os.getenv("AzureWebJobsStorage"))
    name = Path(file.filename).name
    svc.get_blob_client("uploads", name).upload_blob(file.stream.read(), overwrite=True)
    # slett gammel processed
    try: svc.get_blob_client("results", Path(name).stem+"_processed.xlsx").delete_blob()
    except ResourceNotFoundError: pass
    return func.HttpResponse("uploaded")

# --------------------------  DOWNLOAD HTTP  ----------------------------
@app.function_name(name="DownloadFile")
@app.route(route="DownloadFile", auth_level=func.AuthLevel.ANONYMOUS)
def download(req: func.HttpRequest) -> func.HttpResponse:
    fn = req.params.get("filename")
    if not fn: return func.HttpResponse("missing filename", 400)
    blob = BlobServiceClient.from_connection_string(os.getenv("AzureWebJobsStorage")).get_blob_client("results", fn)
    if not blob.exists(): return func.HttpResponse("not ready", 404)
    data = blob.download_blob().readall()
    return func.HttpResponse(data, headers={"Content-Disposition":f"attachment; filename={fn}",
                                            "Content-Type":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})

# ----------------------------  PROGRESS HTTP ---------------------------
@app.function_name(name="GetProgress")
@app.route(route="Progress", auth_level=func.AuthLevel.ANONYMOUS)
def get_progress(req: func.HttpRequest) -> func.HttpResponse:
    fn = req.params.get("filename")
    if not fn: return func.HttpResponse("missing", 400)
    blob = BlobServiceClient.from_connection_string(os.getenv("AzureWebJobsStorage")).get_blob_client("progress", Path(fn).stem+"_progress.json")
    if not blob.exists(): return func.HttpResponse("no prog", 404)
    return func.HttpResponse(blob.download_blob().readall(), mimetype="application/json")

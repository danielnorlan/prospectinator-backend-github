# ----------------------  Prospectinator – Azure Functions  ----------------------
from __future__ import annotations
import asyncio, io, json, logging, os, re, tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
import httpx

# ---------------------------  APP & GLOBALS  -----------------------------------
app = func.FunctionApp()
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

PPLX_KEY   = os.getenv("PPLX_API_KEY")
PPLX_MODEL = os.getenv("PPLX_MODEL", "sonar-pro")
if not PPLX_KEY:
    raise RuntimeError("Env var PPLX_API_KEY mangler")

HEADERS = {"Authorization": f"Bearer {PPLX_KEY}", "Content-Type": "application/json"}

# — regex til å finne de viktigste kolonnene —
REGEX_FIRMA = re.compile(r"^(firma|bedrift|selskapsnavn|company|navn)", re.I)
REGEX_ORGNR = re.compile(r"(org(\.|nr)?|organisasjons?nr)", re.I)
REGEX_TLF   = re.compile(r"(telefon|phone|tlf|mobil|proff ?telefon)", re.I)

# — lister for å finne selskap- og personkolonner til AI-kallet —
COMPANY_COLS = ["Juridisk selskapsnavn", "Selskapsnavn", "Bedrift", "Company", "Firma"]
PERSON_COLS  = ["Navn", "Person", "Kontaktperson", "Name"]

PROSPECT_NR_COL  = "PROSPECTINATOR (TLF)"
PROSPECT_SRC_COL = "PROSPECTINATOR (KILDE)"
FALLBACK_COL     = "Proff Telefon"

PHONE_RE = re.compile(r"(?:0047|\+47)?\D*(\d{8})\b")
URL_RE   = re.compile(r"https?://[^\s>\]]+")

# ---------------------------  HJELPER  -----------------------------------------
def extract_phone(text: str | None) -> str | None:
    if not text: return None
    m = PHONE_RE.search(str(text));  return m.group(1) if m else None

def normalize_phone(p: str | None) -> str:
    if not p: return ""
    try:  return str(int(float(str(p).strip())))
    except Exception: return str(p).strip()

# ---------------------------  AI-KALL  -----------------------------------------
sem = asyncio.Semaphore(5)

async def _ask_ai(company: str, person: str) -> tuple[str|None, str]:
    prompt = (f"Hva er MOBILNUMMERET (8 sifre) til {person} som jobber i {company} i Norge?\n"
              "Svar på to linjer:\n1) Kun åtte sifre\n2) Én URL-kilde\n")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15)) as cli:
            r = await cli.post(
                "https://api.perplexity.ai/chat/completions",
                headers=HEADERS,
                json={
                    "model": PPLX_MODEL,
                    "temperature": 0.4,
                    "messages":[{"role":"user","content":prompt}],
                },
            )
            r.raise_for_status()
    except Exception as e:
        logging.warning("AI-feil %s/%s: %s", company, person, e)
        return None, ""
    js = r.json()
    text = js.get("choices", [{}])[0].get("message", {}).get("content","").strip()
    num  = extract_phone(text.splitlines()[0] if text else "")
    cites = js.get("citations") or []
    src  = cites[0] if cites else (URL_RE.search(text).group(0) if URL_RE.search(text) else "")
    return num, src

async def _process_row(idx: int, row, comp_col: str, pers_col: str):
    company, person = str(row.get(comp_col,"")).strip(), str(row.get(pers_col,"")).strip()
    async with sem:
        try:
            ai_num, ai_src = await asyncio.wait_for(_ask_ai(company, person), timeout=30)
        except asyncio.TimeoutError:
            ai_num, ai_src = None, ""
    if not ai_num:
        fb = normalize_phone(row.get(FALLBACK_COL,""))
        ai_num = fb if fb.isdigit() else ""
        ai_src = "Ingen kilde"
    else:
        ai_num = normalize_phone(ai_num)
    return idx, ai_num, ai_src

# --------------------  BLOB-TRIGGER: XLSX-PROSESSERING ------------------------
@app.function_name(name="process_xlsx_v1")
@app.blob_trigger(arg_name="blob", path="uploads/{name}", connection="AzureWebJobsStorage")
async def process_xlsx_v1(blob: func.InputStream):
    logging.info("▶ Processing %s", blob.name)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    tmp.write(blob.read()); tmp.flush()
    df = pd.read_excel(tmp.name, engine="openpyxl")

    # ---- dynamisk deteksjon av Firmanavn / Org.nr / Telefon ----
    cols_lower = [c.lower().strip() for c in df.columns]
    def find(rgx):
        for i,c in enumerate(cols_lower):
            if rgx.search(c): return df.columns[i]
        return None
    col_firma, col_orgnr, col_tlf = find(REGEX_FIRMA), find(REGEX_ORGNR), find(REGEX_TLF)
    missing = [n for n,v in [("Firmanavn",col_firma),("Org.nr",col_orgnr),("Telefon",col_tlf)] if v is None]
    if missing:
        raise ValueError("Mangler kolonner: " + ", ".join(missing))

    # ---- dropp Unnamed / gamle kildekolonner ----
    drop_cols = [c for c in df.columns if c.startswith("Unnamed") or 
                 (c.lower().startswith("kilder") and c != PROSPECT_SRC_COL)]
    if drop_cols:
        df.drop(columns=drop_cols, inplace=True)

    # ---- sørg for PROSPECT-kolonner ----
    for c in (PROSPECT_NR_COL, PROSPECT_SRC_COL):
        if c not in df.columns:
            df[c] = ""

    # ---- reordne kolonnene ----
    new_cols = [col_firma, col_orgnr, col_tlf, PROSPECT_NR_COL, PROSPECT_SRC_COL]
    new_cols += [c for c in df.columns if c not in new_cols]
    df = df.reindex(columns=new_cols)

    # ---- finn kolonner til AI-oppslag ----
    comp_col = next((c for c in COMPANY_COLS if c in df.columns), col_firma)
    pers_col = next((c for c in PERSON_COLS  if c in df.columns), None)
    if not pers_col:
        raise ValueError("Fant ingen person-kolonne.")

    # -------------------------------------------------------------------------
    #  PROGRESS-BLOB & AI-KJØRING
    # -------------------------------------------------------------------------
    svc = BlobServiceClient.from_connection_string(os.getenv("AzureWebJobsStorage"))
    progress_blob = Path(blob.name).stem + "_progress.json"
    prog_cli = svc.get_blob_client("progress", progress_blob)

    work_rows, empty = [], 0
    for idx,row in df.iterrows():
        if pd.isna(row.get(comp_col)) and pd.isna(row.get(pers_col)):
            empty += 1
            if empty >= 3: break
        else:
            empty = 0
            work_rows.append((idx,row))
    total = len(work_rows)
    prog_cli.upload_blob(json.dumps({"processed":0,"total":total,"percentage":0,
                                     "lastUpdate":datetime.utcnow().isoformat()+"Z"}), overwrite=True)

    tasks = [asyncio.create_task(_process_row(i,r,comp_col,pers_col)) for i,r in work_rows]
    done = 0
    for coro in asyncio.as_completed(tasks):
        idx,num,src = await coro
        df.at[idx, PROSPECT_NR_COL]  = num
        df.at[idx, PROSPECT_SRC_COL] = src
        done += 1
        prog_cli.upload_blob(json.dumps({"processed":done,"total":total,
                                         "percentage":round(done/total*100,2),
                                         "lastUpdate":datetime.utcnow().isoformat()+"Z"}), overwrite=True)

    # ---- skriv resultat-blob ----
    out_name = Path(blob.name).stem + "_processed.xlsx"
    res_cli  = svc.get_blob_client("results", out_name)
    try: res_cli.delete_blob()
    except ResourceNotFoundError: pass

    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl"); buf.seek(0)
    res_cli.upload_blob(buf, overwrite=True)
    logging.info("✔ Lagret results/%s", out_name)

# ---------------------------  HTTP: UPLOAD  -----------------------------------
@app.function_name(name="UploadTest")
@app.route(route="UploadTest", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def upload(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("⏳ UploadTest mottok forespørsel")
    try: file = req.files.get("file")
    except Exception: file=None
    if not file:
        return func.HttpResponse("❌ Ingen fil mottatt.", status_code=400)

    svc = BlobServiceClient.from_connection_string(os.getenv("AzureWebJobsStorage"))
    blob_name = Path(file.filename).name
    svc.get_blob_client("uploads", blob_name).upload_blob(file.stream.read(), overwrite=True)
    try: svc.get_blob_client("results", Path(blob_name).stem+"_processed.xlsx").delete_blob()
    except ResourceNotFoundError: pass

    return func.HttpResponse(f"✅ Fila «{blob_name}» er lastet opp – prosessering starter!", status_code=200)

# ---------------------------  HTTP: DOWNLOAD  ---------------------------------
@app.function_name(name="DownloadFile")
@app.route(route="DownloadFile", auth_level=func.AuthLevel.ANONYMOUS)
def download(req: func.HttpRequest) -> func.HttpResponse:
    fn = req.params.get("filename")
    if not fn: return func.HttpResponse("Missing filename.", 400)
    blob = BlobServiceClient.from_connection_string(os.getenv("AzureWebJobsStorage"))\
           .get_blob_client("results", fn)
    if not blob.exists():
        return func.HttpResponse("File not ready yet.", 404)
    data = blob.download_blob().readall()
    headers = {"Content-Disposition":f"attachment; filename={fn}",
               "Content-Type":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    return func.HttpResponse(data, headers=headers)

# ---------------------------  HTTP: PROGRESS  ---------------------------------
@app.function_name(name="GetProgress")
@app.route(route="Progress", auth_level=func.AuthLevel.ANONYMOUS)
def progress(req: func.HttpRequest) -> func.HttpResponse:
    fn = req.params.get("filename")
    if not fn: return func.HttpResponse("Missing filename.", 400)
    blob = BlobServiceClient.from_connection_string(os.getenv("AzureWebJobsStorage"))\
           .get_blob_client("progress", Path(fn).stem+"_progress.json")
    if not blob.exists():
        return func.HttpResponse("Progress not started.", 404)
    return func.HttpResponse(blob.download_blob().readall(), mimetype="application/json")

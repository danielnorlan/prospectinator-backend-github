DEPRECATED. REPLACED BY ICP HUB
# Prospectinator – AI-powered Excel / CSV Enrichment  8/22-25
# Prospectinator – AI-powered Excel / CSV Enrichment  
Serverless on Azure Functions • Python 3.11 • React Front-end

<p align="center">
  <img src="docs/banner.png" alt="Prospectinator banner" width="760">
</p>

Prospectinator lets users drag-and-drop an Excel/CSV file that contains a list of Norwegian businesses, then automatically:

1. **Detects** which columns hold company name, organisation number & contact person  
2. **Calls** an AI model to fetch the CEO’s private **mobile phone number**  
3. **Logs** progress in real-time so the user always sees how many rows are done  
4. **Writes** enriched data back to a new Excel file (with auto-sized columns)  
5. **Stores** the result in Azure Blob Storage and gives the user a download link  

Everything happens in the background on a **serverless Azure Functions** backend written in Python, with progress and files persisted to Azure Blob Storage.  
No VMs, no manual work – just drop a file, wait a minute, and download your leads.

---

## Table of Contents

1. [Architecture](#architecture)  
2. [Tech Stack](#tech-stack)  
3. [Quick Start](#quick-start)  
4. [Local Development](#local-development)  
5. [Environment Variables](#environment-variables)  
6. [Deployment to Azure](#deployment-to-azure)  
7. [API Reference](#api-reference)  
8. [Project Structure](#project-structure)  
9. [Roadmap](#roadmap)  
10. [Contributing](#contributing)  
11. [License](#license)

---

## Architecture

```
┌──────────────┐     ① Upload file    ┌────────────────────┐
│  Front-end   ├──────────────────────▶  UploadTest (HTTP) │
│  React/HTML  │                     └────────────────────┘
└──────┬───────┘                             │
       │ ③ Poll progress    ② Blob trigger   │
       ▼                     (uploads/)      ▼
┌────────────────────┐      ┌────────────────────┐        ④ Store result
│  Progress (HTTP)   │◀─────┤ process_xlsx_v2    ├──────────────┐
└────────────────────┘      │  (Azure Function) │              │
                            └────────────────────┘              │
                                         │                     ▼
                                         │            ┌────────────────┐
                                         │            │  Blob Storage  │
                                         │            │ uploads/       │
                                         │            │ progress/      │
                                         └───────────▶│ results/       │
                                                      └────────────────┘
                                                             ▲
                                       ⑤ Download file       │
                                 ┌────────────────────┐      │
                                 │ DownloadFile (HTTP)│──────┘
                                 └────────────────────┘
```

* **Blob trigger** fires when a file arrives in **`uploads/`**.  
* Rows are processed concurrently (100 coroutines).  
* Each lookup is rate-limited (token bucket: 490 calls / 60 s).  
* Progress JSON is updated in **`progress/`** every time a row finishes.  
* The final Excel is written to **`results/`** and served for download.

---

## Tech Stack

| Layer            | Tech                                |
|------------------|-------------------------------------|
| Runtime          | **Azure Functions** (Python v2)     |
| Storage          | **Azure Blob Storage**              |
| AI Lookup        | HTTP requests to an external **LLM** |
| Async client     | **httpx + asyncio**                 |
| Data wrangling   | **pandas** + **openpyxl**           |
| Rate limiting    | Custom token-bucket (async)         |
| Front-end        | React 18 (Tailwind / shadcn/ui)     |

---

## Quick Start

```bash
# clone & enter repo
git clone https://github.com/<your-org>/prospectinator-backend.git
cd prospectinator-backend

# create virtualenv
python -m venv .venv
source .venv/bin/activate

# install deps
pip install -r requirements.txt

# copy env template and add your secrets
cp env.example .env
```

Run locally with the Azure Functions runtime:

```bash
func start
```

Open <http://localhost:7071/api/UploadTest> in Postman or cURL to test.

---

## Local Development

1. **Set secrets** in `.env` (see table below).  
2. Start Azure Functions emulator with `func start`.  
3. Use the provided HTML/React front-end (separate repo) **or** send a
   `multipart/form-data` POST to `http://localhost:7071/api/UploadTest` with a file field named `file`.

Example cURL:

```bash
curl -F "file=@my_leads.xlsx"      http://localhost:7071/api/UploadTest
```

---

## Environment Variables

| Variable              | Description                                         |
|-----------------------|-----------------------------------------------------|
| `AzureWebJobsStorage` | Connection string to your Blob Storage account      |
| `PPLX_API_KEY`        | API key for the AI service                           |
| `PPLX_MODEL`          | *(optional)* Model name (default: `sonar-pro`)       |

Make sure these **are set in Azure** (Function App → Configuration) and **never committed** to Git.

---

## Deployment to Azure

> Prerequisite: Azure CLI + Functions Core Tools installed, and a resource group + storage account ready.

```bash
# login + pick subscription
az login
az account set --subscription "<SUBSCRIPTION-ID>"

# create a Python Function App (consumption plan)
az functionapp create   --resource-group prospectinator-rg   --consumption-plan-location westeurope   --runtime python --runtime-version 3.11   --functions-version 4   --name prospectinator-funcapp   --storage-account <YOUR_STORAGE_ACCOUNT>

# deploy code
func azure functionapp publish prospectinator-funcapp --python

# set config
az functionapp config appsettings set -g prospectinator-rg -n prospectinator-funcapp   --settings AzureWebJobsStorage="<STR>" PPLX_API_KEY="<KEY>"
```

---

## API Reference

| Route                       | Method | Auth Level | Purpose                                      |
|-----------------------------|--------|------------|----------------------------------------------|
| `/api/UploadTest`           | POST   | **Function** | Upload Excel/CSV to `uploads/`               |
| `/api/Progress?filename=`   | GET    | **Anonymous** | Get JSON progress snapshot                   |
| `/api/DownloadFile?filename=`| GET   | **Anonymous** | Download processed file from `results/`      |

### UploadTest – Request

```
POST /api/UploadTest
Content-Type: multipart/form-data; boundary=...
Form field: file = <Excel or CSV>
```

**Response 200**  
`✅ File <name> uploaded!`

### Progress – Response

```json
{
  "processed": 57,
  "total": 100,
  "percentage": 57,
  "lastUpdate": "2025-06-15T17:48:23.815Z"
}
```

---

## Project Structure

```
.
├── function_app.py        # all Azure Functions (HTTP & blob trigger)
├── host.json              # host config
├── requirements.txt       # Python deps
├── .github/workflows/     # CI: lint + deploy
├── env.example            # template secrets
└── docs/
    └── banner.png         # repo banner / diagram assets
```

---

## Roadmap

- [ ] Web socket progress updates (remove polling)  
- [ ] Switch AI model to internal endpoint to avoid external rate limits  
- [ ] Add unit tests + GitHub Actions test matrix  
- [ ] Add support for CSV delimiter auto-detection  
- [ ] International phone normalisation (E.164)

See [issues](../../issues) for more.

---

## Contributing

Pull requests are welcome!  
1. Fork → feature branch → PR.  
2. Run `ruff`, `black` and `pytest` locally.  
3. Describe *what* you changed and *why*.

---

## License

MIT © 2025 Daniel Norlan. See [LICENSE](LICENSE) for details.

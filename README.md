# CarbonTrace

**AI-assisted validation of supplier emissions data, built on Microsoft Azure.**

Large companies must report their suppliers' emissions (Scope 3), for example under California's SB 253 (supplier reporting proposed from 2027) and the EU's CSRD. Supplier data arrives as messy PDFs, scans and spreadsheets in different units, languages and formats, and analysts check it by hand. Errors slip through, and auditors can't trace numbers back to their source.

CarbonTrace reads each supplier file, checks it with deterministic rules, uses an AI agent to investigate anything flagged, and sends it to a human reviewer, with every number traceable to its source page.

```
Supplier file → READ → CHECK → INVESTIGATE → REVIEW → AUDIT
              (Document     (Python    (AI agent +   (human)   (trail)
             Intelligence)   rules)      tools)
```

## Design principles
- **Code does the math, AI explains, humans decide.** The agent never calculates or approves anything.
- **Every number has a source.** Values carry the page or cell they came from (data lineage).
- **AI only where judgment is needed.** Duplicates, unit errors and security issues are decided by rules; the agent handles anomalies and missing data.
- **Defense in depth.** Hidden-text detection, input quarantine, a grounding check on every number the agent writes, a step limit, and Azure guardrails.

## What it catches (test set of 8 synthetic supplier files)
| File | Planted issue | Result |
|---|---|---|
| 01 Nordwind | none | auto-validated |
| 02 Atlas Metals | kWh values labeled MWh (1,000x) | unit error |
| 03 Rhein Components | 80% drop, one site missing | anomaly → agent asks supplier |
| 04 Nordwind FINAL | exact duplicate of 01 | duplicate (rule) |
| 05 Kessler | German language + "1.840,5" number format | parsed correctly, auto-validated |
| 06 Sunrise Textiles | hidden white-text prompt injection | security concern (rule) |
| 07 GreenLoop | 40% drop explained by solar install | anomaly → agent: likely real change |
| 08 Pacific Resin | spreadsheet with a missing site value | missing data → agent asks supplier |

Rule checks match the answer key on **8 of 8** files.

## Lessons from testing
- Table values came back split from their labels → rebuilt rows from line coordinates.
- The first real run missed the hidden prompt injection, because Document Intelligence reads the *rendered* page → added a PDF text-layer comparison; the agent only ever sees visible text.
- The agent labeled the solar case correctly without reading the supplier's note → required evidence-cited labels.
- Tightening the prompt regressed the security case → moved clear-cut decisions into deterministic rules.
- The grounding check caught the agent writing a percentage no tool produced and flagged it for the reviewer.

## Tech stack
- **Microsoft Foundry / Azure OpenAI** (gpt-4.1-mini) with function calling
- **Azure AI Document Intelligence** (prebuilt layout)
- **Python**: FastAPI, Jinja2, SQLite, pypdf, openpyxl
- **Azure App Service** for hosting

## Project structure
| File | Role |
|---|---|
| `extract.py` | READ: Document Intelligence / Excel → structured values + source + fingerprint |
| `checks.py` | CHECK: units, YoY, duplicates, missing data, hidden instructions, emissions math |
| `agent.py` | INVESTIGATE: tool-calling agent + rule overrides + grounding check |
| `run_pipeline.py` | Runs READ + CHECK on the test set and scores it against the answer key |
| `app.py` | Website: overview, upload, review queue, item review, audit trail, CSV export |
| `sample_data/` | Synthetic supplier files, prior-year baseline, illustrative emission factors, answer key |

## Run locally
```
python -m venv .venv
.venv\Scripts\activate          (Windows)   |   source .venv/bin/activate   (Mac/Linux)
pip install -r requirements.txt
copy .env.example .env           then paste your Azure endpoints and keys into .env
python run_pipeline.py
python agent.py
uvicorn app:app --reload         then open http://127.0.0.1:8000
```

All supplier data in this repository is synthetic. Emission factors are illustrative demo values.

Built by Lithika Jadav Malothu · [LinkedIn](https://www.linkedin.com/in/lithika-jadav-malothu-a890a1211) · [Portfolio](https://lithikajmalothu-16.github.io)

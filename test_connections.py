"""
CarbonTrace - connection test.
Checks that your laptop can talk to both Azure services before we build anything.
Run:  python test_connections.py
"""
import os
from dotenv import load_dotenv

load_dotenv()  # reads your keys from the .env file, so they never appear in code

def need(name):
    value = os.getenv(name)
    if not value or "paste" in value:
        raise SystemExit(f"Missing {name} in your .env file. Paste the real value and try again.")
    return value

# ---------- Test 1: the AI model (Foundry) ----------
from openai import OpenAI

print("Test 1: calling the AI model...")
client = OpenAI(base_url=need("OPENAI_ENDPOINT"), api_key=need("OPENAI_KEY"))
reply = client.chat.completions.create(
    model=need("OPENAI_DEPLOYMENT"),  # your deployment name, not the model's name
    messages=[{"role": "user", "content": "Reply with exactly: CarbonTrace model is connected."}],
    max_tokens=20,
)
print("  Model says:", reply.choices[0].message.content)

# ---------- Test 2: the document reader (Document Intelligence) ----------
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

print("Test 2: reading sample_supplier_bill.pdf...")
reader = DocumentIntelligenceClient(need("DOCINTEL_ENDPOINT"), AzureKeyCredential(need("DOCINTEL_KEY")))
with open("sample_supplier_bill.pdf", "rb") as f:
    poller = reader.begin_analyze_document("prebuilt-layout", AnalyzeDocumentRequest(bytes_source=f.read()))
result = poller.result()

for page in result.pages:
    for line in page.lines:
        if "Total" in line.content or "Reporting year" in line.content:
            print(f"  Found on page {page.page_number}: {line.content}")

print("\nBoth services are connected. Setup complete.")

"""
CarbonTrace - Step 3a: READ
Turns a supplier file (PDF or Excel) into clean, structured data,
and remembers WHERE each number came from (file + page/cell) for the audit trail.

The "clerk" here is Azure AI Document Intelligence (for PDFs/scans).
Excel files are read directly with openpyxl - no AI needed for a spreadsheet.
"""
import hashlib
import os
import re

import openpyxl

ROW_TOLERANCE = 0.1  # inches: words this close vertically are on the same row

COUNTRY_CODES = {
    "germany": "DE", "deutschland": "DE",
    "united states": "US", "usa": "US",
    "india": "IN",
    "singapore": "SG",
}
GERMAN_WORDS = ("berichtsjahr", "stromverbrauch", "gesamt", "standorte")


# ---------------------------------------------------------------- numbers
def parse_number(text, german=False):
    """Turn '1,220' or '1.840,5' (German) into a float. Returns None if not a number.

    Why this exists: in Germany '1.840,5' means 1840.5. A naive reader would get 1.84
    and the supplier's electricity would look 1000x too small.
    """
    if text is None:
        return None
    s = str(text).strip().replace(" ", "")
    if not re.fullmatch(r"[\d.,]+", s) or not re.search(r"\d", s):
        return None
    if german:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def file_fingerprint(path):
    """SHA-256 of the file's bytes. Two identical files always get the same fingerprint,
    even if they're renamed - that's how duplicates are caught."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---------------------------------------------------------------- PDFs
def read_pdf_lines_azure(path):
    """Send the PDF to Document Intelligence and get back every text line with its page and position."""
    from azure.core.credentials import AzureKeyCredential
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

    client = DocumentIntelligenceClient(
        os.environ["DOCINTEL_ENDPOINT"], AzureKeyCredential(os.environ["DOCINTEL_KEY"])
    )
    with open(path, "rb") as f:
        poller = client.begin_analyze_document("prebuilt-layout", AnalyzeDocumentRequest(bytes_source=f.read()))
    result = poller.result()

    lines = []
    for page in result.pages:
        for line in page.lines:
            xs, ys = line.polygon[0::2], line.polygon[1::2]  # polygon = corner points [x1,y1,x2,y2,...]
            lines.append({"text": line.content, "page": page.page_number, "x": min(xs), "y": sum(ys) / len(ys)})
    return lines, result.content


def lines_to_rows(lines):
    """Rebuild table rows: the reader returns 'Total' and '1,220' as separate lines,
    so we glue together lines that sit at the same height on the same page."""
    rows = []
    for line in sorted(lines, key=lambda l: (l["page"], l["y"], l["x"])):
        if rows and rows[-1]["page"] == line["page"] and abs(rows[-1]["y"] - line["y"]) <= ROW_TOLERANCE:
            rows[-1]["parts"].append(line)
        else:
            rows.append({"page": line["page"], "y": line["y"], "parts": [line]})
    return [
        {"page": r["page"], "text": " ".join(p["text"] for p in sorted(r["parts"], key=lambda p: p["x"]))}
        for r in rows
    ]


def parse_rows(rows, full_text):
    """Find the facts we need in the rows: supplier, year, country, sites, unit, and each site's value."""
    german = any(w in full_text.lower() for w in GERMAN_WORDS)
    data = {"supplier": None, "year": None, "country": None, "sites_listed": [], "unit": None,
            "site_values": [], "total": None, "language": "de" if german else "en"}

    if rows:
        data["supplier"] = rows[0]["text"].split(" - ")[0].strip()

    in_table = False
    for row in rows:
        t = row["text"].strip()
        src = f"page {row['page']}"

        if m := re.match(r"(?i)(reporting year|berichtsjahr)\s*:\s*(\d{4})", t):
            data["year"] = int(m.group(2))
        elif m := re.match(r"(?i)(country|land)\s*:\s*(.+)", t):
            data["country"] = COUNTRY_CODES.get(m.group(2).strip().lower(), m.group(2).strip())
        elif m := re.match(r"(?i)(sites included|standorte)\s*:\s*(.+)", t):
            data["sites_listed"] = [s.strip() for s in m.group(2).split(",")]
        elif m := re.search(r"\((MWh|kWh)\)", t, re.IGNORECASE):
            data["unit"] = m.group(1)
            in_table = True
        elif in_table:
            m = re.match(r"(.*?)\s+([\d.,]+)$", t)
            if not m:
                in_table = False
                continue
            label, raw = m.group(1).strip(), m.group(2)
            value = parse_number(raw, german)
            entry = {"label": label, "raw": raw, "value": value, "source": src}
            if label.lower() in ("total", "gesamt"):
                data["total"] = entry
            else:
                data["site_values"].append(entry)
    return data


# ---------------------------------------------------------------- Excel
def read_xlsx(path):
    """Spreadsheets are already structured, so we read cells directly and record the cell address."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    data = {"supplier": None, "year": None, "country": None, "sites_listed": [], "unit": None,
            "site_values": [], "total": None, "language": "en"}
    text_parts, in_table = [], False

    for row in ws.iter_rows():
        key = row[0].value
        val = row[1].value if len(row) > 1 else None
        val_cell = row[1].coordinate if len(row) > 1 else row[0].coordinate
        text_parts.append(f"{key} {val if val is not None else ''}".strip())
        if key is None:
            in_table = False
            continue
        k = str(key).strip()
        src = f"sheet '{ws.title}' cell {val_cell}"

        if k.lower() == "supplier":
            data["supplier"] = str(val)
        elif k.lower() == "reporting year":
            data["year"] = int(val)
        elif k.lower() == "country":
            data["country"] = COUNTRY_CODES.get(str(val).lower(), str(val))
        elif m := re.search(r"\((MWh|kWh)\)", str(val or ""), re.IGNORECASE):
            data["unit"] = m.group(1)
            in_table = True
        elif in_table:
            entry = {"label": k, "raw": val, "value": parse_number(val), "source": src}
            if k.lower() == "total":
                data["total"] = entry
            else:
                data["site_values"].append(entry)
                data["sites_listed"].append(k)
    return data, "\n".join(text_parts)


# ---------------------------------------------------------------- hidden text
def read_pdf_text_layer(path):
    """Read the PDF's raw text layer - everything typed into the file, visible or not.

    Why: Document Intelligence reads what a person would SEE on the page, so white-on-white
    text never shows up in its output. But the text is still inside the file, and any other
    tool (a chatbot, copy-paste, a search index) would read it. Comparing the two catches it.
    """
    from pypdf import PdfReader
    return "\n".join((page.extract_text() or "") for page in PdfReader(path).pages)


def find_hidden_text(visible_text, raw_text):
    """Return lines that exist in the file but were not visible on the page."""
    visible = set(re.findall(r"[a-z]{4,}", visible_text.lower()))
    hidden = []
    for line in raw_text.splitlines():
        words = re.findall(r"[a-z]{4,}", line.lower())
        unseen = [w for w in words if w not in visible]
        if len(words) >= 4 and len(unseen) / len(words) > 0.5:
            hidden.append(line.strip())
    return hidden


# ---------------------------------------------------------------- main entry
def extract(path, pdf_reader=read_pdf_lines_azure):
    """Read any supplier file and return structured data + full text + fingerprint."""
    if path.lower().endswith(".xlsx"):
        data, full_text = read_xlsx(path)
        data["hidden_text"] = []
        data["visible_text"] = full_text
    else:
        lines, full_text = pdf_reader(path)
        data = parse_rows(lines_to_rows(lines), full_text)
        raw_text = read_pdf_text_layer(path)
        data["hidden_text"] = find_hidden_text(full_text, raw_text)
        data["visible_text"] = full_text  # what a person sees - the only text the AI agent may read
        full_text = full_text + "\n" + raw_text  # scan BOTH for injection attempts
    data["file"] = os.path.basename(path)
    data["fingerprint"] = file_fingerprint(path)
    data["full_text"] = full_text
    return data

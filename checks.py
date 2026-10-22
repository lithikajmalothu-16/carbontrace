"""
CarbonTrace - Step 3b: CHECK
Plain Python rules that run the same way every time (deterministic).
No AI here: math and comparisons must be exact and repeatable for auditors.
"""
import csv
import difflib
import re
from datetime import datetime, timezone

YOY_THRESHOLD = 0.30        # a change bigger than 30% vs last year gets a human look
UNIT_ERROR_RATIO = 100      # 100x bigger/smaller than last year = almost certainly a unit mix-up
PROTECTED_EMPLOYEES = 1000  # EU value-chain cap: suppliers under 1,000 employees can limit data requests

INJECTION_PATTERNS = [
    r"ignore (all )?(previous|prior) instructions",
    r"system note",
    r"you are (an|the) ai",
    r"mark (this supplier )?as approved",
    r"do not flag",
]


def load_baseline(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["supplier"]: r for r in csv.DictReader(f)}


def load_factors(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["country"]: float(r["grid_factor_tco2e_per_mwh"]) for r in csv.DictReader(f)}


def to_mwh(value, unit):
    if value is None:
        return None
    return value / 1000 if (unit or "").lower() == "kwh" else value


def match_supplier(name, baseline):
    """Suppliers write their names differently ('Pacific Resin Co' vs 'Pacific Resin Co.').
    Fuzzy matching finds the closest known supplier instead of failing on a dot."""
    hit = difflib.get_close_matches(name or "", list(baseline), n=1, cutoff=0.6)
    return hit[0] if hit else None


def run_checks(data, baseline, factors, seen):
    """Run every rule on one extracted file. `seen` remembers earlier files (for duplicates)."""
    flags, tags = [], []
    unit = data.get("unit")

    # 1. Normalize everything to MWh
    for s in data["site_values"]:
        s["mwh"] = to_mwh(s["value"], unit)
    total = data.get("total")
    total_mwh = to_mwh(total["value"], unit) if total and total["value"] is not None else None

    # 2. Missing data
    missing = [s["label"] for s in data["site_values"] if s["value"] is None]
    if missing or total_mwh is None:
        flags.append({"type": "missing_data",
                      "detail": f"No value for: {', '.join(missing) or 'Total'}",
                      "facts": {"missing_sites": missing}})

    # 3. Sites add up to the total?
    site_sum = sum(s["mwh"] for s in data["site_values"] if s["mwh"] is not None)
    if total_mwh and not missing and abs(site_sum - total_mwh) > 0.01 * total_mwh:
        flags.append({"type": "total_mismatch",
                      "detail": f"Sites add to {site_sum:,.1f} MWh but total says {total_mwh:,.1f} MWh",
                      "facts": {"site_sum_mwh": site_sum, "total_mwh": total_mwh}})

    # 4. Compare with last year
    match = match_supplier(data.get("supplier"), baseline)
    base = baseline.get(match) if match else None
    yoy = None
    if base is None:
        flags.append({"type": "unknown_supplier", "detail": "No prior-year record found", "facts": {}})
    else:
        base_mwh = float(base["electricity_mwh_2025"])
        base_sites = [s.strip() for s in base["sites_2025"].split(";")]
        now_sites = [s.lower() for s in data.get("sites_listed", [])]
        dropped_sites = [s for s in base_sites if s.lower() not in now_sites]
        if int(base["employees"]) < PROTECTED_EMPLOYEES:
            tags.append("protected_supplier")

        if total_mwh:
            ratio = total_mwh / base_mwh
            yoy = ratio - 1
            facts = {"this_year_mwh": total_mwh, "last_year_mwh": base_mwh, "change_pct": round(yoy * 100, 1),
                     "sites_last_year": base_sites, "sites_this_year": data.get("sites_listed", []),
                     "sites_dropped": dropped_sites}
            if ratio >= UNIT_ERROR_RATIO or ratio <= 1 / UNIT_ERROR_RATIO:
                flags.append({"type": "unit_error",
                              "detail": f"{ratio:,.0f}x last year - looks like kWh reported as {unit} (or vice versa)",
                              "facts": facts})
            elif abs(yoy) >= YOY_THRESHOLD:
                flags.append({"type": "anomaly",
                              "detail": f"{yoy*100:+.0f}% vs last year",
                              "facts": facts})

    # 5. Duplicates: same bytes, or same supplier + year + total
    key = (match or data.get("supplier"), data.get("year"), total_mwh)
    if data["fingerprint"] in seen["fingerprints"] or key in seen["keys"]:
        first = seen["fingerprints"].get(data["fingerprint"]) or seen["keys"].get(key)
        flags.append({"type": "duplicate", "detail": f"Same data as {first}", "facts": {"original_file": first}})
    else:
        seen["fingerprints"][data["fingerprint"]] = data["file"]
        seen["keys"][key] = data["file"]

    # 6. Hidden instructions aimed at the AI (prompt injection)
    hits = [p for p in INJECTION_PATTERNS if re.search(p, data["full_text"], re.IGNORECASE)]
    hidden = data.get("hidden_text", [])
    if hits or hidden:
        detail = "Document contains text that tries to instruct an AI reviewer" if hits \
            else "Document contains text that is not visible on the page"
        if hidden:
            detail += " (hidden/invisible text found)"
        flags.append({"type": "suspicious_content",
                      "detail": detail,
                      "facts": {"patterns_matched": hits, "hidden_text": hidden}})

    # 7. Emissions = electricity x grid factor (code does the math, never the AI)
    country = data.get("country") or (base["country"] if base else None)
    factor = factors.get(country)
    emissions = round(total_mwh * factor, 1) if total_mwh and factor else None

    return {
        "file": data["file"],
        "supplier": data.get("supplier"),
        "matched_supplier": match,
        "year": data.get("year"),
        "country": country,
        "unit_reported": unit,
        "language": data.get("language"),
        "total_mwh": total_mwh,
        "emissions_tco2e": emissions,
        "emission_factor": factor,
        "yoy_change_pct": round(yoy * 100, 1) if yoy is not None else None,
        "site_values": data["site_values"],
        "sites_listed": data.get("sites_listed", []),
        "visible_text": data.get("visible_text", "")[:4000],
        "total_source": total["source"] if total else None,
        "flags": flags,
        "tags": tags,
        "status": "needs_review" if flags else "auto_validated",
        "audit": {
            "fingerprint_sha256": data["fingerprint"],
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "checks_run": ["missing_data", "total_mismatch", "yoy_and_units", "duplicate",
                           "prompt_injection", "emissions_calc"],
        },
    }

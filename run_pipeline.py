"""
CarbonTrace - run READ + CHECK on every file in sample_data/ and score it against the answer key.
Run:  python run_pipeline.py
Output: a summary in the terminal + results.json (the data the agent and web app will use next).
"""
import glob
import json
import os

from dotenv import load_dotenv

from checks import load_baseline, load_factors, run_checks
from extract import extract

load_dotenv()

DATA = "sample_data"

# What each planted file SHOULD trigger (from answer_key.csv) - used to measure the catch rate
EXPECTED = {
    "01_nordwind_plastics_2026.pdf": set(),
    "02_atlas_metals_2026.pdf": {"unit_error"},
    "03_rhein_components_2026.pdf": {"anomaly"},
    "04_nordwind_plastics_2026_FINAL.pdf": {"duplicate"},
    "05_kessler_verpackung_2026.pdf": set(),
    "06_sunrise_textiles_2026.pdf": {"suspicious_content"},
    "07_greenloop_logistics_2026.pdf": {"anomaly"},
    "08_pacific_resin_2026.xlsx": {"missing_data"},
}


def main(pdf_reader=None):
    baseline = load_baseline(os.path.join(DATA, "baseline_2025.csv"))
    factors = load_factors(os.path.join(DATA, "emission_factors.csv"))
    seen = {"fingerprints": {}, "keys": {}}
    results = []

    files = sorted(glob.glob(os.path.join(DATA, "*.pdf")) + glob.glob(os.path.join(DATA, "*.xlsx")))
    for path in files:
        print(f"Reading {os.path.basename(path)}...")
        data = extract(path, pdf_reader) if pdf_reader else extract(path)
        results.append(run_checks(data, baseline, factors, seen))

    # ---- summary
    print("\n" + "=" * 90)
    print(f"{'FILE':38} {'MWh':>12} {'tCO2e':>12}  RESULT")
    print("-" * 90)
    caught = 0
    for r in results:
        found = {f["type"] for f in r["flags"]}
        expected = EXPECTED.get(r["file"], set())
        # OK = every expected flag was raised (and clean files stayed clean)
        ok = expected.issubset(found) if expected else not found
        caught += ok
        mwh = f"{r['total_mwh']:,.1f}" if r["total_mwh"] else "-"
        co2 = f"{r['emissions_tco2e']:,.1f}" if r["emissions_tco2e"] else "-"
        label = ", ".join(sorted(found)) or "auto-validated"
        print(f"{r['file']:38} {mwh:>12} {co2:>12}  {'OK ' if ok else 'MISS'} {label}")
    print("-" * 90)
    print(f"Matched the answer key on {caught} of {len(results)} files.")

    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print("Saved full results (with sources for every number) to results.json")
    return results


if __name__ == "__main__":
    main()

"""Usage: python smoke_test.py <base_url>   e.g. https://bup-hackathon-18a5.onrender.com"""
import json, sys, urllib.request

base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
print("health:", urllib.request.urlopen(base + "/health", timeout=60).read().decode())

hours = [{"hour": h, "demand_kwh": 100, "solar_kwh": 80 if 8 <= h <= 16 else 0,
          "tariff_bdt_per_kwh": 12 if 17 <= h <= 21 else 6} for h in range(24)]
payload = {
    "scenario_id": "smoke-1",
    "operator_notes": ["Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window.",
                       "Cafeteria menu changes tomorrow."],
    "hours": hours,
    "battery": {"capacity_kwh": 500, "initial_energy_kwh": 200, "minimum_energy_kwh": 50,
                "max_charge_kwh_per_hour": 100, "max_discharge_kwh_per_hour": 100},
}
req = urllib.request.Request(base + "/optimize-energy", json.dumps(payload).encode(),
                             {"Content-Type": "application/json"}, method="POST")
out = json.loads(urllib.request.urlopen(req, timeout=90).read())
print(json.dumps(out["directive_interpretation"], indent=2))
print("total_cost_bdt:", out["total_cost_bdt"], "| peak_grid_kwh:", out["peak_grid_kwh"])
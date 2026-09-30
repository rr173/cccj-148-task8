"""Generate sample_import.json: a small feeder vessel, 36 containers.

Layout: bays 2/4/6/8 (40ft), rows 1-6, each bay-row has a hold stack
(4 tiers, 100t) and a deck stack (2 tiers, 60t). Reefer plugs on selected
deck stacks. Port rotation SHA -> SIN -> HAM.
"""
import json
import random

random.seed(7)

BAYS = [2, 4, 6, 8]
ROWS = [1, 2, 3, 4, 5, 6]

stacks = []
for bay in BAYS:
    for row in ROWS:
        stacks.append({"bay": bay, "row": row, "max_tiers": 4,
                       "max_weight": 100.0, "on_deck": False})
        plugs = 2 if (bay in (4, 6) and row in (2, 3, 4, 5)) else 0
        stacks.append({"bay": bay, "row": row, "max_tiers": 2,
                       "max_weight": 60.0, "on_deck": True,
                       "reefer_plugs": plugs})

containers = []
n = 0


def add(pod, weight, hazard=None, reefer=False):
    global n
    n += 1
    c = {"id": f"MSKU{n:06d}", "pod": pod, "weight": round(weight, 1)}
    if hazard:
        c["hazard_class"] = hazard
    if reefer:
        c["reefer"] = True
    containers.append(c)


# dangerous goods (must be segregated)
add("HAM", 18.0, hazard="3")
add("SIN", 15.0, hazard="3")
add("HAM", 21.0, hazard="3")
add("HAM", 12.0, hazard="5.1")
add("SIN", 14.0, hazard="5.1")
add("HAM", 16.0, hazard="8")
add("SIN", 11.0, hazard="8")
add("HAM", 19.0, hazard="4.1")
# reefers
for pod in ("SIN", "SIN", "HAM", "HAM", "HAM", "HAM"):
    add(pod, random.uniform(14, 24), reefer=True)
# general cargo
for _ in range(22):
    pod = "SIN" if random.random() < 0.4 else "HAM"
    add(pod, random.uniform(8, 28))

payload = {
    "voyage": {"name": "FEEDER-001 SHA-SIN-HAM",
               "port_sequence": ["SHA", "SIN", "HAM"]},
    "ship": {"lightship_weight": 9000.0, "lightship_lcg": 55.0,
             "lightship_vcg": 8.5, "min_gm": 1.0, "max_gm": 5.5,
             "max_trim": 2.0},
    "stacks": stacks,
    "containers": containers,
}

print(json.dumps(payload, indent=1))

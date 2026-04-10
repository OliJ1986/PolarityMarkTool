"""Quick regression test for ZCO board diode polarity detection."""
from collections import Counter
from core.odb_parser import ODBParser, parse_odb_raw

odb_path = "ODB++/ZCO_201767141_DE_V1_11189848_0x_ODB++.tgz"
print("Parsing ZCO board …")
comps, scale = parse_odb_raw(odb_path)
print(f"Total components: {len(comps)}")

diodes = [c for c in comps if c.comp_type in ("diode", "led")]
print(f"Diodes/LEDs: {len(diodes)}")

# Check specific previously-wrong diodes
problem_refs = ["D12", "D42", "D65", "D58", "D60", "D62", "D64", "D74"]
print()
print("--- Problematic diodes ---")
for ref in problem_refs:
    c = next((x for x in comps if x.ref == ref), None)
    if c:
        method = c._detection_method or "(not set)"
        cat_pin = c._cathode_pin_name or str(c._cathode_pin_num)
        pp = c.polarity_pin
        pp_name = pp.name if pp else "None"
        print(f"  {ref}: method={method}, cathode_pin={cat_pin}, polarity_pin.name={pp_name}")
    else:
        print(f"  {ref}: NOT FOUND")

print()
print("--- Detection method summary (diodes/LEDs) ---")
method_counts = Counter(c._detection_method or "fallback" for c in diodes)
for meth, cnt in sorted(method_counts.items()):
    print(f"  {meth}: {cnt}")

# Build results
results = ODBParser._build_results(comps, scale)
needs_review = [r for r in results if r.polarity_status == "needs_review"]
marked = [r for r in results if r.polarity_status == "marked"]
print()
print(f"Results: {len(marked)} marked, {len(needs_review)} needs_review")
if needs_review:
    print("needs_review components:")
    for r in needs_review[:15]:
        m = r.markers[0] if r.markers else None
        dm = m.detection_method if m else "?"
        print(f"  {r.component.ref}: detection_method={dm}")


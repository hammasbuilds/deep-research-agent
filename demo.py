"""Five sources on one question. Two are the same article. They disagree.

    python demo.py

Two failures that a research agent has to survive, shown together:

  1. Republished copies inflate corroboration. The same wire story on three
     sites is one source, not three, and counting it as three turns a single
     claim into a consensus.
  2. Sources disagree. An agent that picks one and reports it confidently has
     destroyed the only information the user needed.

No network, no model: the fetched pages are written here (not real publications,
on reserved example domains) so deduplication
and corroboration can be watched on their own.
"""

import sys

sys.path.insert(0, "src")

from research.claims import corroborate, extract
from research.sources import Source, deduplicate, independence

WIRE = (
    "Global installed solar capacity reached 2100 GW in 2024. "
    "Analysts expect continued growth through the decade."
)
WIRE_REPRINT = (
    "Global installed solar capacity reached 2100 GW in 2024. "
    "Analysts expect continued growth through the decade. (Reuters)"
)
AGENCY = "Global installed solar capacity reached 2050 GW in 2024. Growth was concentrated in Asia."
DISSENT = (
    "Global installed solar capacity reached 1600 GW in 2024. "
    "Earlier estimates overstated commissioning dates."
)
INDEPENDENT = (
    "Global installed solar capacity reached 2080 GW in 2024. "
    "Figures are provisional pending audit."
)

SOURCES = [
    Source(url="https://news-a.example/solar", title="Solar record", text=WIRE),
    Source(url="https://news-b.example/solar", title="Solar record", text=WIRE_REPRINT),
    Source(url="https://energy-agency.example.org/solar", title="Agency", text=AGENCY),
    Source(url="https://contrarian.example/solar", title="Revision", text=DISSENT),
    Source(url="https://renewables-institute.example.org/solar", title="Institute", text=INDEPENDENT),
]

print("INPUT")
print("   question: how much solar capacity was installed globally by 2024?")
print(f"   {len(SOURCES)} fetched pages:")
for s in SOURCES:
    print(f"      {s.domain:34} {s.text[:54]}...")
print()

groups = deduplicate(SOURCES)
ind = independence(groups)

print("OUTPUT")
print(f"   {len(SOURCES)} pages -> {len(groups)} independent sources")
for g in groups:
    if g.duplicates:
        copies = ", ".join([g.representative.domain] + [d.domain for d in g.duplicates])
        print(f"      merged as one: {copies}  (same text, republished)")
print(
    f"   independence ratio {ind['independence_ratio']:.0%}"
    f"  ({ind['independent_sources']} of {ind['documents']} documents)"
)
print()

claims = []
for s in SOURCES:
    claims.extend(extract(s.text, source_url=s.url))
findings = corroborate(claims)

for f in findings:
    if len(f.claims) < 2:
        continue  # a single-source assertion is not a corroboration result
    flag = "DISAGREEMENT" if f.contradiction or (f.spread or 0) > 0.10 else "agreed"
    print(f"   {flag:14} {f.subject}")
    if f.consensus_value is not None:
        print(
            f"      consensus   {f.consensus_value:g}   spread {f.spread:.1%}"
            if f.spread is not None
            else f"      consensus   {f.consensus_value:g}"
        )
    for c in f.claims:
        if c.value is not None:
            print(f"      {c.value:>8g} {c.unit:4} {c.source_url}")
print()
print("   The 1600 GW figure is not discarded for being the minority. It is")
print("   reported as a disagreement, because a user asking this question")
print("   needs to know the sources do not agree far more than they need a")
print("   confident single number.")

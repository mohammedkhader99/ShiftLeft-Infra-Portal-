"""Verify the architecture slide's layout arithmetically.

There is no LibreOffice in this environment, so the usual render-and-look QA is
unavailable. The defects that QA catches are mostly geometric — text past the
slide edge, boxes overlapping, margins too tight — and those are checkable
without rendering.

What this CANNOT check is text fit: whether a string at 11.5pt actually fits the
box it was given. That is reported as a risk estimate, not a pass.
"""

from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
DATA = json.loads((HERE / "_slide_geometry.json").read_text(encoding="utf-8"))
SLIDE_W, SLIDE_H = DATA["slide"]
SHAPES = [(n, l, t, w, h) for n, l, t, w, h in DATA["shapes"]]

MARGIN = 0.5
problems: list[str] = []

# --- 1. nothing off the slide, nothing inside the margin --------------------
for name, l, t, w, h in SHAPES:
    r, b = l + w, t + h
    if l < -0.001 or t < -0.001 or r > SLIDE_W + 0.001 or b > SLIDE_H + 0.001:
        problems.append(f"OFF-SLIDE  {name}: ({l:.2f},{t:.2f})-({r:.2f},{b:.2f})")
    elif name not in ("asof",) and (l < MARGIN - 0.001 or r > SLIDE_W - MARGIN + 0.001):
        problems.append(f"MARGIN     {name}: x {l:.2f}..{r:.2f} "
                        f"(want {MARGIN}..{SLIDE_W - MARGIN})")

# --- 2. cards must not overlap each other ------------------------------------
CARDS = [s for s in SHAPES if s[0].startswith(("zone_", "chip_", "authority_band",
                                               "ai_card"))]


def overlap(a, b) -> float:
    _, al, at, aw, ah = a
    _, bl, bt, bw, bh = b
    dx = min(al + aw, bl + bw) - max(al, bl)
    dy = min(at + ah, bt + bh) - max(at, bt)
    return dx * dy if dx > 0 and dy > 0 else 0.0


def contains(outer, inner) -> bool:
    """Nesting is intentional (chips sit inside their zone); only PARTIAL
    overlap is a defect. Treating containment as overlap was a checker bug."""
    _, ol, ot, ow, oh = outer
    _, il, it, iw, ih = inner
    return (il >= ol - 0.01 and it >= ot - 0.01
            and il + iw <= ol + ow + 0.01 and it + ih <= ot + oh + 0.01)


for i, a in enumerate(CARDS):
    for b in CARDS[i + 1:]:
        area = overlap(a, b)
        if area > 0.0005 and not (contains(a, b) or contains(b, a)):
            problems.append(f"OVERLAP    {a[0]} x {b[0]}  ({area:.3f} sq in)")

# --- 3. text must stay inside the card it belongs to -------------------------
ZONE_OF = {"users": "zone_users", "portal": "zone_portal", "approval": "zone_approval",
           "orch": "zone_orch", "clouds": "zone_clouds"}
zones = {n: (l, t, w, h) for n, l, t, w, h in SHAPES if n.startswith("zone_")}

for name, l, t, w, h in SHAPES:
    key = None
    if name.startswith("hdr_"):
        key = ZONE_OF.get(name[4:])
    elif name.startswith("itm_"):
        key = ZONE_OF.get(name.split("_")[1])
    if not key or key not in zones:
        continue
    zl, zt, zw, zh = zones[key]
    if l < zl - 0.001 or t < zt - 0.001 or l + w > zl + zw + 0.001 or t + h > zt + zh + 0.001:
        problems.append(f"ESCAPES    {name} is outside {key}")

# --- 4. gaps between neighbouring zones --------------------------------------
zone_order = sorted(((n, l, w) for n, l, t, w, h in SHAPES if n.startswith("zone_")),
                    key=lambda z: z[1])
for (n1, l1, w1), (n2, l2, _) in zip(zone_order, zone_order[1:]):
    gap = l2 - (l1 + w1)
    if gap < 0.25:
        problems.append(f"TIGHT GAP  {n1} -> {n2}: {gap:.2f}in (want >= 0.25)")

# --- 5. text-fit RISK (cannot be proven without rendering) -------------------
# Average Calibri advance width per point of size, measured empirically as
# ~0.0058 in/pt for mixed-case prose. Deliberately PESSIMISTIC (real Calibri is
# narrower) so a pass here has margin in hand.
def char_width(size: float) -> float:
    return 0.0060 * size


def line_height(size: float) -> float:
    return 0.0175 * size          # ~1.22x the point size, in inches


risky: list[str] = []
by_box: dict[str, list] = {}
for name, size, chars, width, height in DATA.get("texts", []):
    by_box.setdefault(name.split("[")[0], []).append((size, chars, width, height))

for box, runs in by_box.items():
    width = runs[0][2]
    height = runs[0][3]
    used = 0.0
    detail = []
    for size, chars, _w, _h in runs:
        per_line = max(width / char_width(size), 1)
        lines = max(1, -(-chars // int(per_line)))       # ceil
        used += lines * line_height(size)
        detail.append(f"{chars}ch@{size}pt->{lines}ln")
    if used > height + 0.001:
        risky.append(f"OVERFLOW  {box}: needs ~{used:.2f}in, has {height:.2f}in "
                     f"({', '.join(detail)})")

problems.extend(risky)
risky = []

print(f"shapes checked: {len(SHAPES)}")
if problems:
    print(f"\n{len(problems)} PROBLEM(S):")
    for p in problems:
        print("  " + p)
else:
    print("no geometry problems: nothing off-slide, no overlaps, margins respected")
if risky:
    print("\ntext-fit risks (estimate only — not rendered):")
    for r in risky:
        print("  " + r)
sys.exit(1 if problems else 0)

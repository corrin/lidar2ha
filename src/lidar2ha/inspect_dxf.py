#!/usr/bin/env python3
"""Report the structure of a floor-plan DXF.

The question this answers: are walls, doors and windows separate, identifiable
objects that can be mapped onto Sweet Home 3D -- or just anonymous line soup?
For Polycam they are separate, on `Poly-Walls`, `Poly-Rooms`, `Poly-Doors` and
`Poly-RoomLabels`, which is the whole reason `polycam` can read it.

Run this first against an export from any other scanner. If the layers here do
not look like the ones `polycam` queries, that is the work, and it is better
found now than as an empty model.

Usage:
    python -m lidar2ha.inspect_dxf plan.dxf
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

import ezdxf


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dxf")
    args = ap.parse_args()

    doc = ezdxf.readfile(args.dxf)
    msp = doc.modelspace()

    print(f"DXF version : {doc.dxfversion}  ({doc.acad_release})")
    # polycam assumes metres ($INSUNITS = 6) and multiplies by 100.
    print(f"units       : {doc.header.get('$INSUNITS')}  (1=in 4=mm 6=m)")
    print()

    print("LAYERS")
    for layer in sorted(doc.layers, key=lambda lyr: lyr.dxf.name):
        print(f"  {layer.dxf.name}")
    print()

    by_layer: defaultdict[str, Counter] = defaultdict(Counter)
    for e in msp:
        by_layer[e.dxf.layer][e.dxftype()] += 1

    print("ENTITIES BY LAYER")
    for name in sorted(by_layer):
        counts = by_layer[name]
        total = sum(counts.values())
        kinds = ", ".join(f"{k}={v}" for k, v in counts.most_common())
        print(f"  {name:<32} {total:>5}   {kinds}")
    print()

    print(f"blocks      : {[b.name for b in doc.blocks if not b.name.startswith('*')]}")

    texts = [e.dxf.text for e in msp if e.dxftype() in ("TEXT", "MTEXT")]
    if texts:
        print(f"text labels : {texts[:20]}")

    # Polycam holds every wall twice, as identical polylines; polycam.py
    # deduplicates by exact point sequence. Worth knowing for another scanner.
    walls = by_layer.get("Poly-Walls", Counter()).get("LWPOLYLINE", 0)
    if walls:
        print(f"\nPoly-Walls carries {walls} polylines "
              f"({walls // 2} walls if this export duplicates them as Polycam does)")


if __name__ == "__main__":
    main()

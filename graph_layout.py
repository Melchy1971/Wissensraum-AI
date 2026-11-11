"""Layout of the graph view without Tk (step 8B.4), shared by the desktop window and the web page (phase 7).

The focus (or the best connected entity) in the middle, its neighbours on an inner ring, all others on an
outer ring.
"""
from __future__ import annotations

import math

TYPE_COLOURS = ('#2f6db5', '#b5512f', '#2f8f5b', '#8a4fb0', '#b58f2f', '#4f9fb0', '#b04f7a', '#6b6b6b')


def layout(nodes, edges, width, height, focus=None):
    """Positions {id: (x, y)} inside width x height with a margin of 60 pixels."""
    if not nodes:
        return {}
    centre = focus if focus in {n['id'] for n in nodes} else nodes[0]['id']
    neighbours = ({e['target_id'] for e in edges if e['source_id'] == centre}
                  | {e['source_id'] for e in edges if e['target_id'] == centre}) - {centre}
    inner = [n['id'] for n in nodes if n['id'] in neighbours]
    outer = [n['id'] for n in nodes if n['id'] != centre and n['id'] not in neighbours]
    cx, cy = width / 2, height / 2
    radius = max(40.0, min(width, height) / 2 - 60)
    positions = {centre: (cx, cy)}
    for ring, share in ((inner, 0.45 if outer else 0.8), (outer, 1.0)):
        for index, node_id in enumerate(ring):
            angle = 2 * math.pi * index / len(ring) - math.pi / 2
            positions[node_id] = (cx + share * radius * math.cos(angle), cy + share * radius * math.sin(angle))
    return positions

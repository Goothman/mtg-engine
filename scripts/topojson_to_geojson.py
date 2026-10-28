"""Convert one object of a TopoJSON file to compact GeoJSON (used to bundle map shapes).

    python scripts/topojson_to_geojson.py es-atlas/es/provinces.json provinces out.geojson
"""
from __future__ import annotations

import json
import sys


def decode_arcs(topo: dict) -> list[list[list[float]]]:
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append([round(x * sx + tx, 4), round(y * sy + ty, 4)])
        arcs.append(pts)
    return arcs


def ring(indices: list[int], arcs: list) -> list:
    out: list = []
    for i in indices:
        pts = arcs[i] if i >= 0 else arcs[~i][::-1]
        out.extend(pts if not out else pts[1:])
    return out


def geometry(g: dict, arcs: list) -> dict:
    if g["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [ring(r, arcs) for r in g["arcs"]]}
    if g["type"] == "MultiPolygon":
        return {"type": "MultiPolygon", "coordinates": [[ring(r, arcs) for r in p] for p in g["arcs"]]}
    if g["type"] == "MultiLineString":
        return {"type": "MultiLineString", "coordinates": [ring(r, arcs) for r in g["arcs"]]}
    if g["type"] == "LineString":
        return {"type": "LineString", "coordinates": ring(g["arcs"], arcs)}
    raise ValueError(g["type"])


def main() -> None:
    src, obj, dst = sys.argv[1:4]
    topo = json.load(open(src))
    arcs = decode_arcs(topo)
    feats = [{"type": "Feature", "properties": g.get("properties", {}), "geometry": geometry(g, arcs)}
             for g in topo["objects"][obj]["geometries"]]
    json.dump({"type": "FeatureCollection", "features": feats}, open(dst, "w"), separators=(",", ":"))
    print(f"{len(feats)} features -> {dst}")


if __name__ == "__main__":
    main()

"""Run under `uv run --with klayout python gds_probe.py <file.gds>`; prints one JSON object."""
import json
import sys

import klayout.db as db

path = sys.argv[1]
ly = db.Layout()
ly.read(path)
out = {"dbu": ly.dbu, "tops": [c.name for c in ly.top_cells()], "ncells": ly.cells()}
top = ly.top_cells()[0] if ly.top_cells() else None
if top is not None:
    bb = top.dbbox()
    out["bbox"] = [bb.left, bb.bottom, bb.right, bb.top]
infos = sorted({(i.layer, i.datatype) for i in ly.layer_infos()})
out["layers"] = infos
empty, cellnames, zero, scanned = [], [], 0, 0
cells_with_geom = 0
for c in ly.each_cell():
    cellnames.append(c.name)
    ns = 0
    for li in ly.layer_indexes():
        sh = c.shapes(li)
        n = sh.size()
        ns += n
        if n and scanned < 4_000_000:
            for s in sh.each():
                scanned += 1
                if s.is_box():
                    b = s.box
                    if b.width() == 0 or b.height() == 0:
                        zero += 1
                elif s.is_polygon() or s.is_simple_polygon():
                    if s.polygon.area() == 0:
                        zero += 1
                elif s.is_path():
                    if s.path.length() == 0 and s.path.width == 0:
                        zero += 1
    if ns == 0 and c.child_instances() == 0:
        empty.append(c.name)
out["empty_cells"] = empty
out["cell_names"] = cellnames
out["zero_area"] = zero
out["shapes_scanned"] = scanned
out["top_children"] = sorted({ly.cell(i.cell_index).name for i in top.each_inst()}) if top else []
print(json.dumps(out))

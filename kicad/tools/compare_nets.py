"""Compare schematic connectivity (kicad-cli netlist) with the board nets: pin groups must match.

    kicad-cli sch export netlist --format kicadsexpr -o kicad/net.txt kicad/PALHASERVO.kicad_sch
    <KiCad python> kicad/tools/compare_nets.py kicad/net.txt kicad/PALHASERVO.kicad_pcb
"""
import re
import sys

import wx

app = wx.App(False)
import pcbnew  # noqa: E402

net_txt = open(sys.argv[1], encoding="utf-8").read()
sch = {}
for blk in net_txt[net_txt.index("(nets"):].split("(net\n")[1:]:
    name = re.search(r'\(name "([^"]*)"\)', blk).group(1).split("/")[-1]
    pins = frozenset(re.findall(r'\(ref "([^"]+)"\)\s*\(pin "([^"]+)"\)', blk))
    sch.setdefault(name, set()).update(pins)
sch = {k: frozenset(v) for k, v in sch.items()}

b = pcbnew.LoadBoard(sys.argv[2])
pcb = {}
for fp in b.GetFootprints():
    for pad in fp.Pads():
        n = pad.GetNetname()
        if n:
            pcb.setdefault(n, set()).add((fp.GetReference(), pad.GetNumber()))
pcb = {k: frozenset(v) for k, v in pcb.items()}

sg = {v for v in sch.values() if len(v) > 1}
pg = {v for v in pcb.values() if len(v) > 1}
print("schematic nets with >1 pin:", len(sg), " board nets:", len(pg))
print("only in schematic:")
for g in sg - pg:
    print("  ", sorted(g), [k for k, v in sch.items() if v == g])
print("only on board:")
for g in pg - sg:
    print("  ", sorted(g), [k for k, v in pcb.items() if v == g])
if sg == pg:
    print("CONNECTIVITY MATCH")

"""Import the frozen Altium PCB (altium/PALHASERVO.PcbDoc) with KiCad's pcbnew Altium plugin.

Run with KiCad's bundled python from the repo root:
    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 kicad/tools/import_pcb.py

Writes kicad/PALHASERVO.kicad_pcb and the native footprint library kicad/chico_servo.pretty
(all footprints used on the board; the Altium footprint libraries are not needed).
"""
import os
import shutil

import wx

app = wx.App(False)
import pcbnew  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
LIB = "chico_servo"
pretty = os.path.join(ROOT, "kicad", LIB + ".pretty")

b = pcbnew.LoadBoard(os.path.join(ROOT, "altium", "PALHASERVO.PcbDoc"), pcbnew.PCB_IO_MGR.ALTIUM_DESIGNER)
if os.path.isdir(pretty):
    shutil.rmtree(pretty)
os.makedirs(pretty)
io = pcbnew.PCB_IO_KICAD_SEXPR()

seen = {}
for fp in b.GetFootprints():
    name = str(fp.GetFPID().GetLibItemName())
    if name not in seen:
        lib_fp = pcbnew.FOOTPRINT(fp)
        lib_fp.SetReference("REF**")
        lib_fp.SetValue(name)
        lib_fp.SetParentGroup(None)
        for pad in lib_fp.Pads():
            pad.SetNetCode(0)
        lib_fp.SetPosition(pcbnew.VECTOR2I(0, 0))
        lib_fp.SetOrientationDegrees(0)
        if lib_fp.IsFlipped():
            lib_fp.Flip(lib_fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
        io.FootprintSave(pretty, lib_fp)
        seen[name] = True
    fp.SetFPID(pcbnew.LIB_ID(LIB, name))

out = os.path.join(ROOT, "kicad", "PALHASERVO.kicad_pcb")
b.Save(out)
bb = b.GetBoardEdgesBoundingBox()
print("footprints", len(b.GetFootprints()), "unique", len(seen), "nets", b.GetNetCount() - 1,
      "tracks", len(b.GetTracks()), "zones", len(b.Zones()), "copper layers", b.GetCopperLayerCount(),
      "outline mm %.2f x %.2f" % (bb.GetWidth() / 1e6, bb.GetHeight() / 1e6))

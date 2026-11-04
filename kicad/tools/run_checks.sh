#!/bin/sh
# Rebuild nothing; just export the netlist and compare connectivity against the board.
set -e
cd "$(dirname "$0")/../.."
K=/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli
P=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
$K sch export netlist --format kicadsexpr -o kicad/net.txt kicad/PALHASERVO.kicad_sch 2>&1 | grep -v Fontconfig || true
$P kicad/tools/compare_nets.py kicad/net.txt kicad/PALHASERVO.kicad_pcb 2>&1 | grep -v "Adding dup"

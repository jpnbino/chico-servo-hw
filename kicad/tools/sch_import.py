"""Convert the frozen Altium schematic sheets (altium/*.SchDoc) into KiCad .kicad_sch files.

Usage (system python3 with olefile, or KiCad's python with kicad/tools/_vendor on PYTHONPATH):
    python3 kicad/tools/sch_import.py

KiCad cannot import SchDoc files headlessly (kicad-cli has no import command and the GUI cannot be
scripted without accessibility rights), so the binary records are decoded here. Components carry
their symbols inside the SchDoc, so the native symbol library kicad/chico_servo.kicad_sym is
built from those embedded symbols. Run import_pcb.py first (footprint names are read from the board).
"""
import base64
import collections
import os
import re
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "_vendor"))
import altium_sch  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
ALT = os.path.join(ROOT, "altium")
OUT = os.path.join(ROOT, "kicad")
PROJECT = "PALHASERVO"
LIB = "chico_servo"
MM = 0.254  # one Altium schematic unit is 10 mil
NS = uuid.UUID("5d3f3c4e-0c0b-4c61-9d7e-2f4b5e1a7a01")

# (file, kicad file, sheet name)
SHEETS = [
    ("[01] - COVER PAGE.SchDoc", "01_cover_page.kicad_sch", "COVER PAGE"),
    ("[02] - BLOCK DIAGRAM.SchDoc", "02_block_diagram.kicad_sch", "BLOCK DIAGRAM"),
    ("[03] - CIRCUIT.SchDoc", "03_circuit.kicad_sch", "CIRCUIT"),
    ("[04] - MECHANICAL.SchDoc", "04_mechanical.kicad_sch", "MECHANICAL"),
    ("[05] - REVISION HISTORY.SchDoc", "05_revision_history.kicad_sch", "REVISION HISTORY"),
]
ROOT_UUID = str(uuid.uuid5(NS, "root"))
ROT_SIGN = 1
# Pins deliberately left open in the Altium design (they raise no ERC marker there); KiCad needs a no-connect flag.
NC_PINS = {("U1", "9"), ("U1", "11"), ("U2", "4")}
# Nets driven by passive/regulator pins only; a PWR_FLAG satisfies KiCad's "power input not driven" check.
FLAG_NETS = {"VCC5V0", "VCC3V3", "GND", "HC_05_VCC5V0"}
MIRROR_TOKEN = "x"


def uid(*a):
    return str(uuid.uuid5(NS, "/".join(map(str, a))))


class Q(str):
    """Quoted string atom."""


def fmt(v):
    if isinstance(v, Q):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(v, float):
        s = ("%.4f" % v).rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    return str(v)


def sx(node, ind=0):
    pad = "\t" * ind
    head = [fmt(a) for a in node if not isinstance(a, list)]
    kids = [a for a in node if isinstance(a, list)]
    if not kids:
        return pad + "(" + " ".join(head) + ")"
    if all(len(k) <= 4 and not any(isinstance(x, list) for x in k) for k in kids) and len(kids) < 6:
        return pad + "(" + " ".join(head) + " " + " ".join(sx(k) for k in kids) + ")"
    return pad + "(" + " ".join(head) + "\n" + "\n".join(sx(k, ind + 1) for k in kids) + "\n" + pad + ")"


def num(r, k, d=0.0):
    if k not in r:
        return d
    return float(r[k]) + float(r.get(k + "_FRAC", 0)) / 100000.0


def ob(text):
    """Altium overbar (trailing backslash after each char) -> KiCad ~{...}."""
    if "\\" not in text:
        return text
    out, bar, i = "", "", 0
    chars = []
    while i < len(text):
        c = text[i]
        if i + 1 < len(text) and text[i + 1] == "\\":
            chars.append((c, True))
            i += 2
        else:
            chars.append((c, False))
            i += 1
    for c, b in chars:
        if b and not bar:
            out += "~{"
            bar = True
        elif not b and bar:
            out += "}"
            bar = False
        out += c
    if bar:
        out += "}"
    return out


def color(v):
    """Altium BGR integer -> (r, g, b)."""
    v = int(v)
    return (v & 255, (v >> 8) & 255, (v >> 16) & 255)


def effects(size, hide=False, bold=False, italic=False, col=None, justify=None):
    font = ["font", ["size", size, size]]
    if bold:
        font.append(["bold", "yes"])
    if italic:
        font.append(["italic", "yes"])
    if col:
        font.append(["color", col[0], col[1], col[2], 1])
    e = ["effects", font]
    if justify:
        e.append(["justify"] + justify)
    if hide:
        e.append(["hide", "yes"])
    return e


STROKE_W = [0.1, 0.15, 0.25, 0.4]
RESERVED = {"Reference", "Value", "Footprint", "Datasheet", "Description"}
PINTYPE = {0: "input", 1: "bidirectional", 2: "output", 3: "open_collector", 4: "passive",
           5: "tri_state", 6: "open_emitter", 7: "power_in"}
PIN_DIR = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}
PIN_KICAD_ANGLE = {0: 180, 1: 270, 2: 0, 3: 90}


def sym_name(libref):
    return re.sub(r"[^A-Za-z0-9_.+\-]", "_", libref)


def footprints_from_pcb():
    """{reference: 'lib:name'} read from the converted KiCad board."""
    txt = open(os.path.join(OUT, "PALHASERVO.kicad_pcb"), encoding="utf-8").read()
    out = {}
    for m in re.finditer(r'\(footprint "([^"]+)"(.*?)\(property "Reference" "([^"]+)"', txt, re.S):
        out[m.group(3)] = m.group(1)
    return out


class Sheet:
    def __init__(self, path, name, page):
        self.path, self.name, self.page = path, name, page
        self.recs = altium_sch.load(path)
        self.images = altium_sch.load_images(path)
        s = self.recs[0]
        self.H = int(s.get("CUSTOMY", 1110))
        self.sheet = s
        self.children = collections.defaultdict(list)
        for i, r in enumerate(self.recs):
            if "OWNERINDEX" in r:
                self.children[int(r["OWNERINDEX"])].append(i)

    def X(self, x):
        return round(x * MM, 4)

    def Y(self, y):
        return round((self.H - y) * MM, 4)

    def font(self, fid):
        s = self.sheet
        n = str(fid)
        size = int(s.get("SIZE" + n, 10))
        return round(max(size * 0.2, 1.0), 3), s.get("BOLD" + n) == "T", s.get("ITALIC" + n) == "T"


def lib_graphics(sh, comp_idx, part):
    """Graphic items of a component's symbol, in symbol-local mm (y up), for one unit."""
    c = sh.recs[comp_idx]
    lx, ly = num(c, "LOCATION.X"), num(c, "LOCATION.Y")
    items, pins = [], []

    orient = int(c.get("ORIENTATION", 0))
    mirrored = c.get("ISMIRRORED") == "T"

    def inv(dx, dy):
        """Undo the placement transform (SchDoc stores primitives already rotated/mirrored)."""
        for _ in range(orient):
            dx, dy = dy, -dx
        return (-dx, dy) if mirrored else (dx, dy)

    def P(x, y):
        a, b = inv(x - lx, y - ly)
        return (round(a * MM, 4), round(b * MM, 4))

    stroke = ["stroke", ["width", 0.1524], ["type", "default"]]
    for j in sh.children[comp_idx]:
        r = sh.recs[j]
        if r.get("OWNERPARTID", "-1") not in (str(part), "-1") or r["RECORD"] in ("34", "41", "44"):
            continue
        if r.get("DISPLAYMODE", "0") != "0":
            continue
        t = r["RECORD"]
        solid = r.get("ISSOLID") == "T"
        fill = ["fill", ["type", "background" if solid else "none"]]
        if t == "2":
            o = int(r.get("PINCONGLOMERATE", 0))
            ori = o & 3
            L = num(r, "PINLENGTH")
            bx, by = P(num(r, "LOCATION.X"), num(r, "LOCATION.Y"))
            dx, dy = inv(*PIN_DIR[ori])
            ori = {v: k for k, v in PIN_DIR.items()}[(dx, dy)]
            hx, hy = bx + dx * L * MM, by + dy * L * MM
            name = ob(r.get("NAME", ""))
            show_name = bool(o & 8) and name != ""
            show_num = bool(o & 16)
            hidden = bool(o & 4)
            pins.append(["pin", PINTYPE.get(int(r.get("ELECTRICAL", 4)), "passive"), "line",
                         ["at", round(hx, 4), round(hy, 4), PIN_KICAD_ANGLE[ori]],
                         ["length", round(L * MM, 4)]]
                        + ([["hide", "yes"]] if hidden else [])
                        + [["name", Q(name if name else "~"), effects(1.27, hide=not show_name)],
                           ["number", Q(r["DESIGNATOR"]), effects(1.27, hide=not show_num)]])
        elif t == "13":
            items.append(["polyline", ["pts", ["xy"] + list(P(num(r, "LOCATION.X"), num(r, "LOCATION.Y"))),
                                        ["xy"] + list(P(num(r, "CORNER.X"), num(r, "CORNER.Y")))],
                          stroke, ["fill", ["type", "none"]]])
        elif t in ("6", "7"):
            n = int(r["LOCATIONCOUNT"])
            pts = [P(num(r, "X%d" % k), num(r, "Y%d" % k)) for k in range(1, n + 1)]
            if t == "7":
                pts.append(pts[0])
            items.append(["polyline", ["pts"] + [["xy", a, b] for a, b in pts], stroke,
                          fill if t == "7" else ["fill", ["type", "none"]]])
        elif t == "14":
            a = P(num(r, "LOCATION.X"), num(r, "LOCATION.Y"))
            b = P(num(r, "CORNER.X"), num(r, "CORNER.Y"))
            items.append(["rectangle", ["start", a[0], a[1]], ["end", b[0], b[1]], stroke, fill])
        elif t == "12":
            cx, cy = P(num(r, "LOCATION.X"), num(r, "LOCATION.Y"))
            rad = num(r, "RADIUS") * MM
            sa, ea = num(r, "STARTANGLE"), num(r, "ENDANGLE")
            if abs(((ea - sa) % 360)) < 1e-6 or (sa == 0 and ea >= 359.999):
                items.append(["circle", ["center", cx, cy], ["radius", round(rad, 4)], stroke,
                              ["fill", ["type", "none"]]])
            else:
                import math
                def pt(a):
                    return (round(cx + rad * math.cos(math.radians(a)), 4),
                            round(cy + rad * math.sin(math.radians(a)), 4))
                s, m, e = pt(sa), pt(sa + ((ea - sa) % 360) / 2), pt(ea)
                items.append(["arc", ["start"] + list(s), ["mid"] + list(m), ["end"] + list(e), stroke,
                              ["fill", ["type", "none"]]])
        elif t == "4":
            x, y = P(num(r, "LOCATION.X"), num(r, "LOCATION.Y"))
            size, bold, ital = sh.font(r.get("FONTID", 1))
            items.append(["text", Q(r.get("TEXT", "")), ["at", x, y, 0], effects(min(size, 1.27))])
        elif t in ("30", "15", "16", "17"):
            pass
        else:
            print("  ! unhandled symbol record", t, "in component", comp_idx)
    return items, pins


def collect_component(sh, ci, fps):
    c = sh.recs[ci]
    params, des, desrec, impl = {}, None, None, None
    for j in sh.children[ci]:
        r = sh.recs[j]
        if r["RECORD"] == "34":
            des, desrec = r["TEXT"], r
        elif r["RECORD"] == "41":
            params[r["NAME"]] = r
    return c, params, des, desrec


def resolve(params, name):
    low = {k.lower(): v for k, v in params.items()}
    n = name.lstrip("=").strip('"')
    return low[n.lower()].get("TEXT", "") if n.lower() in low else None


def build_lib_symbol(sh, ci, params, name, prefix, value, fp, ds, desc, nunits):
    props = []

    def prop(k, v, x=0.0, y=0.0, hide=True):
        props.append(["property", Q(k), Q(v), ["at", x, y, 0], effects(1.27, hide=hide)])

    prop("Reference", prefix, 0, 3.81, hide=False)
    prop("Value", value, 0, -3.81, hide=False)
    prop("Footprint", fp)
    prop("Datasheet", ds)
    prop("Description", desc)
    for k, r in params.items():
        if k in RESERVED or k in ("Comment", "Value", "Designator") or r.get("TEXT") in (None, "", "*"):
            continue
        if k == "ComponentLink1URL":
            continue
        prop(k, r["TEXT"])
    node = ["symbol", Q(name), ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"]] + props
    pin_numbers_hidden = False
    for u in range(1, nunits + 1):
        items, pins = lib_graphics(sh, ci, u)
        node.append(["symbol", Q("%s_%d_1" % (name, u))] + items + pins)
    return node


def convert():
    fps = footprints_from_pcb()
    libsyms = collections.OrderedDict()  # name -> node (without lib prefix)
    sheets = []
    for n, (src, dst, title) in enumerate(SHEETS, start=2):
        sheets.append(Sheet(os.path.join(ALT, src), title, n))
    top = Sheet(os.path.join(ALT, "PALHASERVO.SchDoc"), "PALHASERVO", 1)
    report = {}

    def build(sh, sheet_uuid_path, is_root):
        recs = sh.recs
        items, used_syms = [], collections.OrderedDict()
        pwr_n = [0]
        path = "/" + ROOT_UUID + ("" if is_root else "/" + sheet_uuid_path)
        if is_root:
            path = "/" + ROOT_UUID
        for i, r in enumerate(recs):
            t = r["RECORD"]
            own = int(r.get("OWNERINDEX", -1))
            if own == 1 or t in ("31", "39", "41", "34", "44", "45", "46", "48", "2"):
                continue  # template graphics, parameters and component children
            if own >= 0 and recs[own]["RECORD"] in ("1", "15", "44", "34") and t != "30":
                continue
            col = {}
            if t == "1":
                c, params, des, desrec = collect_component(sh, i, fps)
                libref = c["LIBREFERENCE"]
                name = sym_name(libref)
                nunits = max(int(c.get("PARTCOUNT", 2)) - 1, 1)
                prefix = re.sub(r"[0-9]+$", "", des)
                comment = params.get("Comment", {}).get("TEXT", "")
                valp = params.get("Value")
                value = None
                vis_val = None
                if valp is not None and valp.get("TEXT"):
                    value, vis_val = valp["TEXT"], valp
                else:
                    value = comment
                    if comment.startswith("="):
                        rv = resolve(params, comment)
                        value = rv if rv is not None else libref
                    vis_val = params.get("Comment")
                fp = fps.get(des, "")
                ds = params.get("ComponentLink1URL", {}).get("TEXT", "")
                desc = c.get("COMPONENTDESCRIPTION", "")
                if name not in libsyms:
                    libsyms[name] = build_lib_symbol(sh, i, params, name, prefix, value, fp, ds, desc, nunits)
                used_syms[name] = libsyms[name]
                ang = (int(c.get("ORIENTATION", 0)) * 90 * ROT_SIGN) % 360
                at = ["at", sh.X(num(c, "LOCATION.X")), sh.Y(num(c, "LOCATION.Y")), ang]
                part = int(c.get("CURRENTPARTID", 1))
                node = ["symbol", ["lib_id", Q("%s:%s" % (LIB, name))], at]
                if c.get("ISMIRRORED") == "T":
                    node.append(["mirror", MIRROR_TOKEN])
                on_board = fp != ""
                node += [["unit", part], ["exclude_from_sim", "no"], ["in_bom", "yes" if on_board else "no"],
                         ["on_board", "yes" if on_board else "no"], ["dnp", "no"],
                         ["fields_autoplaced", "no"], ["uuid", Q(uid(sh.name, i))]]

                def fld(k, v, x, y, hide, a=0):
                    node.append(["property", Q(k), Q(v), ["at", x, y, a], effects(1.27, hide=hide)])

                cx, cy = sh.X(num(c, "LOCATION.X")), sh.Y(num(c, "LOCATION.Y"))
                fld("Reference", des, sh.X(num(desrec, "LOCATION.X")), sh.Y(num(desrec, "LOCATION.Y")),
                    desrec.get("ISHIDDEN") == "T", 90 if int(desrec.get("ORIENTATION", 0)) % 2 else 0)
                vh = vis_val is None or vis_val.get("ISHIDDEN") == "T"
                vx = sh.X(num(vis_val, "LOCATION.X")) if vis_val else cx
                vy = sh.Y(num(vis_val, "LOCATION.Y")) if vis_val else cy
                fld("Value", value, vx, vy, vh, 90 if vis_val and int(vis_val.get("ORIENTATION", 0)) % 2 else 0)
                fld("Footprint", fp, cx, cy, True)
                fld("Datasheet", ds, cx, cy, True)
                fld("Description", desc, cx, cy, True)
                for k, r2 in params.items():
                    if k in RESERVED or k in ("Comment", "Value", "Designator") or r2.get("TEXT") in (None, "", "*"):
                        continue
                    fld(k, r2["TEXT"], cx, cy, True)
                npins = len([1 for j in sh.children[i] if recs[j]["RECORD"] == "2" and recs[j].get("OWNERPARTID") == str(part)])
                for j in sh.children[i]:
                    if recs[j]["RECORD"] == "2" and recs[j].get("OWNERPARTID") == str(part):
                        node.append(["pin", Q(recs[j]["DESIGNATOR"]), ["uuid", Q(uid(sh.name, j))]])
                node.append(["instances", ["project", Q(PROJECT), ["path", Q(path), ["reference", Q(des)], ["unit", part]]]])
                items.append(node)
                for j in sh.children[i]:
                    pr = recs[j]
                    if pr["RECORD"] == "2" and (des, pr["DESIGNATOR"]) in NC_PINS:
                        d = PIN_DIR[int(pr.get("PINCONGLOMERATE", 0)) & 3]
                        L = num(pr, "PINLENGTH")
                        items.append(["no_connect", ["at", sh.X(num(pr, "LOCATION.X") + d[0] * L),
                                                     sh.Y(num(pr, "LOCATION.Y") + d[1] * L)],
                                      ["uuid", Q(uid(sh.name, j, "nc"))]])
                report[des] = (libref, value, fp)
            elif t == "27":
                n = int(r["LOCATIONCOUNT"])
                pts = [(sh.X(num(r, "X%d" % k)), sh.Y(num(r, "Y%d" % k))) for k in range(1, n + 1)]
                for k in range(n - 1):
                    items.append(["wire", ["pts", ["xy"] + list(pts[k]), ["xy"] + list(pts[k + 1])],
                                  ["stroke", ["width", 0], ["type", "default"]], ["uuid", Q(uid(sh.name, i, k))]])
            elif t == "29":
                items.append(["junction", ["at", sh.X(num(r, "LOCATION.X")), sh.Y(num(r, "LOCATION.Y"))],
                              ["diameter", 0], ["color", 0, 0, 0, 0], ["uuid", Q(uid(sh.name, i))]])
            elif t == "22":
                items.append(["no_connect", ["at", sh.X(num(r, "LOCATION.X")), sh.Y(num(r, "LOCATION.Y"))],
                              ["uuid", Q(uid(sh.name, i))]])
            elif t == "25":
                o = int(r.get("ORIENTATION", 0))
                just = {0: ["left", "bottom"], 1: ["left", "bottom"], 2: ["right", "bottom"], 3: ["right", "bottom"]}[o]
                items.append(["label", Q(ob(r["TEXT"])),
                              ["at", sh.X(num(r, "LOCATION.X")), sh.Y(num(r, "LOCATION.Y")), (0, 90, 180, 270)[o]],
                              effects(1.27, justify=just), ["uuid", Q(uid(sh.name, i))]])
            elif t == "17":
                net = r["TEXT"]
                gnd = net.upper().startswith(("GND", "VSS", "AGND", "PGND"))
                lname = sym_name(net)
                if lname not in libsyms:
                    libsyms[lname] = power_symbol(lname, net, gnd)
                used_syms[lname] = libsyms[lname]
                o = int(r.get("ORIENTATION", 0))
                base = 3 if gnd else 1
                delta = ((o - base) % 4) * 90 * ROT_SIGN % 360
                pwr_n[0] += 1
                x, y = sh.X(num(r, "LOCATION.X")), sh.Y(num(r, "LOCATION.Y"))
                import math
                vx, vy = (0, 3.81) if gnd else (0, -3.175)
                th = math.radians(delta)
                rx = vx * math.cos(th) + vy * math.sin(th)
                ry = -vx * math.sin(th) + vy * math.cos(th)
                ref = "#PWR%02d" % (len([1 for x in items if x[0] == "symbol"]) + 1)
                if net in FLAG_NETS:
                    FLAG_NETS.discard(net)
                    fx = round(x + 5.08, 4)
                    if "PWR_FLAG" not in libsyms:
                        libsyms["PWR_FLAG"] = pwr_flag_symbol()
                    used_syms["PWR_FLAG"] = libsyms["PWR_FLAG"]
                    items.append(["wire", ["pts", ["xy", x, y], ["xy", fx, y]],
                                  ["stroke", ["width", 0], ["type", "default"]], ["uuid", Q(uid(sh.name, i, "fw"))]])
                    items.append(["symbol", ["lib_id", Q(LIB + ":PWR_FLAG")], ["at", fx, y, 0], ["unit", 1],
                                  ["exclude_from_sim", "no"], ["in_bom", "no"], ["on_board", "no"], ["dnp", "no"],
                                  ["fields_autoplaced", "no"], ["uuid", Q(uid(sh.name, i, "fs"))],
                                  ["property", Q("Reference"), Q("#FLG%02d" % (len(FLAG_NETS) + 1)), ["at", fx, y, 0], effects(1.27, hide=True)],
                                  ["property", Q("Value"), Q("PWR_FLAG"), ["at", fx, round(y - 3.81, 4), 0], effects(1.27, hide=True)],
                                  ["property", Q("Footprint"), Q(""), ["at", fx, y, 0], effects(1.27, hide=True)],
                                  ["property", Q("Datasheet"), Q("~"), ["at", fx, y, 0], effects(1.27, hide=True)],
                                  ["pin", Q("1"), ["uuid", Q(uid(sh.name, i, "fp"))]],
                                  ["instances", ["project", Q(PROJECT), ["path", Q(path), ["reference", Q("#FLG%02d" % (len(FLAG_NETS) + 1))], ["unit", 1]]]]])
                items.append(["symbol", ["lib_id", Q("%s:%s" % (LIB, lname))], ["at", x, y, delta],
                              ["unit", 1], ["exclude_from_sim", "no"], ["in_bom", "no"], ["on_board", "no"],
                              ["dnp", "no"], ["fields_autoplaced", "no"], ["uuid", Q(uid(sh.name, i))],
                              ["property", Q("Reference"), Q(ref), ["at", x, y, 0], effects(1.27, hide=True)],
                              ["property", Q("Value"), Q(net), ["at", round(x + rx, 4), round(y + ry, 4), 0],
                               effects(1.27, hide=r.get("SHOWNETNAME") != "T")],
                              ["property", Q("Footprint"), Q(""), ["at", x, y, 0], effects(1.27, hide=True)],
                              ["property", Q("Datasheet"), Q(""), ["at", x, y, 0], effects(1.27, hide=True)],
                              ["pin", Q("1"), ["uuid", Q(uid(sh.name, i, "p"))]],
                              ["instances", ["project", Q(PROJECT), ["path", Q(path), ["reference", Q(ref)], ["unit", 1]]]]])
            elif t == "4":
                if "TEXT" not in r:
                    continue
                size, bold, ital = sh.font(r.get("FONTID", 1))
                o = int(r.get("ORIENTATION", 0))
                j = int(r.get("JUSTIFICATION", 0))
                jh = ["left", "center", "right"][j % 3]
                jv = ["bottom", "center", "top"][j // 3]
                items.append(["text", Q(r["TEXT"].replace("~1", "\n")), ["exclude_from_sim", "no"],
                              ["at", sh.X(num(r, "LOCATION.X")), sh.Y(num(r, "LOCATION.Y")), 90 if o % 2 else 0],
                              effects(size, bold=bold, italic=ital, col=color(r["COLOR"]) if "COLOR" in r else None,
                                      justify=[x for x in (jh if jh != "center" else None, jv if jv != "center" else None) if x]),
                              ["uuid", Q(uid(sh.name, i))]])
            elif t == "28":
                size, bold, ital = sh.font(r.get("FONTID", 1))
                x1, y1 = sh.X(num(r, "LOCATION.X")), sh.Y(num(r, "LOCATION.Y"))
                x2, y2 = sh.X(num(r, "CORNER.X")), sh.Y(num(r, "CORNER.Y"))
                items.append(["text_box", Q(r.get("TEXT", "").replace("~1", "\n")), ["exclude_from_sim", "no"],
                              ["at", min(x1, x2), min(y1, y2), 0], ["size", round(abs(x2 - x1), 4), round(abs(y2 - y1), 4)],
                              ["margins", 0.9525, 0.9525, 0.9525, 0.9525],
                              ["stroke", ["width", 0.1], ["type", "solid"]], ["fill", ["type", "none"]],
                              effects(size, bold=bold, italic=ital, justify=["left", "top"]), ["uuid", Q(uid(sh.name, i))]])
            elif t == "14":
                x1, y1 = sh.X(num(r, "LOCATION.X")), sh.Y(num(r, "LOCATION.Y"))
                x2, y2 = sh.X(num(r, "CORNER.X")), sh.Y(num(r, "CORNER.Y"))
                fill = ["fill", ["type", "none"]]
                if r.get("ISSOLID") == "T" and "AREACOLOR" in r:
                    c3 = color(r["AREACOLOR"])
                    fill = ["fill", ["type", "color"], ["color", c3[0], c3[1], c3[2], 1]]
                items.append(["rectangle", ["start", x1, y1], ["end", x2, y2],
                              ["stroke", ["width", STROKE_W[min(int(r.get("LINEWIDTH", 1)), 3)]], ["type", "default"],
                                         ["color"] + list(color(r.get("COLOR", 0))) + [1]],
                              fill, ["uuid", Q(uid(sh.name, i))]])
            elif t in ("6", "7"):
                n = int(r["LOCATIONCOUNT"])
                pts = [(sh.X(num(r, "X%d" % k)), sh.Y(num(r, "Y%d" % k))) for k in range(1, n + 1)]
                if t == "7":
                    pts.append(pts[0])
                items.append(["polyline", ["pts"] + [["xy", a, b] for a, b in pts],
                              ["stroke", ["width", STROKE_W[min(int(r.get("LINEWIDTH", 1)), 3)]], ["type", "default"],
                                         ["color"] + list(color(r.get("COLOR", 0))) + [1]],
                              ["fill", ["type", "none"]], ["uuid", Q(uid(sh.name, i))]])
            elif t == "13":
                items.append(["polyline", ["pts", ["xy", sh.X(num(r, "LOCATION.X")), sh.Y(num(r, "LOCATION.Y"))],
                                           ["xy", sh.X(num(r, "CORNER.X")), sh.Y(num(r, "CORNER.Y"))]],
                              ["stroke", ["width", 0.15], ["type", "default"]], ["fill", ["type", "none"]],
                              ["uuid", Q(uid(sh.name, i))]])
            elif t == "30":
                fn = r.get("FILENAME", "")
                if own == 1 or fn not in sh.images:
                    continue
                bmp = sh.images[fn]
                import struct
                w, h = struct.unpack_from("<ii", bmp, 18)
                shrink = max(1, w // 700)
                png = altium_sch.bmp_to_png(bmp, shrink)
                pw, ph = w // shrink, abs(h) // shrink
                x1, y1 = sh.X(num(r, "LOCATION.X")), sh.Y(num(r, "LOCATION.Y"))
                x2, y2 = sh.X(num(r, "CORNER.X")), sh.Y(num(r, "CORNER.Y"))
                width_mm = abs(x2 - x1)
                scale = width_mm / (pw * 25.4 / 300.0)
                items.append(["image", ["at", round((x1 + x2) / 2, 4), round((y1 + y2) / 2, 4)],
                              ["scale", round(scale, 5)], ["uuid", Q(uid(sh.name, i))],
                              ["data", base64.b64encode(png).decode()]])
            elif t == "15" and is_root:
                fname = [recs[j] for j in sh.children[i] if recs[j]["RECORD"] == "33"][0]["TEXT"]
                match = [s for s in sheets if s.path.endswith(fname)][0]
                dst = [d for (s_, d, t_) in SHEETS if s_ == fname][0]
                x, y = sh.X(num(r, "LOCATION.X")), sh.Y(num(r, "LOCATION.Y"))
                w, h = num(r, "XSIZE") * MM, num(r, "YSIZE") * MM
                items.append(["sheet", ["at", x, y], ["size", round(w, 4), round(h, 4)],
                              ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"], ["dnp", "no"],
                              ["fields_autoplaced", "yes"],
                              ["stroke", ["width", 0.1524], ["type", "solid"]],
                              ["fill", ["color", 0, 0, 0, 0.0]], ["uuid", Q(sheet_uuid(match))],
                              ["property", Q("Sheetname"), Q(match.name), ["at", x, round(y - 0.7, 4), 0],
                               effects(1.27, justify=["left", "bottom"])],
                              ["property", Q("Sheetfile"), Q(dst), ["at", x, round(y + h + 0.7, 4), 0],
                               effects(1.27, justify=["left", "top"])],
                              ["instances", ["project", Q(PROJECT), ["path", Q("/" + ROOT_UUID), ["page", Q(str(match.page))]]]]])
            elif t in ("15", "32", "33", "12", "5", "8", "10", "11", "3", "9", "26", "18", "16", "35", "37", "43"):
                if t in ("15", "32", "33"):
                    continue
                if own < 0 or recs[own]["RECORD"] not in ("1",):
                    print("  ! unhandled sheet record", t, "in", sh.name)
            else:
                print("  ! unhandled record", t, "in", sh.name)
        return items, used_syms

    def sheet_uuid(sh):
        return uid("sheet", sh.name)

    outputs = {}
    for sh, (src, dst, title) in zip(sheets, SHEETS):
        items, used = build(sh, sheet_uuid(sh), False)
        outputs[dst] = write_sch(sh, items, used, sheet_uuid(sh), title)
    items, used = build(top, "", True)
    outputs["PALHASERVO.kicad_sch"] = write_sch(top, items, used, ROOT_UUID, "PALHASERVO", root=True)
    for dst, text in outputs.items():
        with open(os.path.join(OUT, dst), "w", encoding="utf-8") as f:
            f.write(text)
        print("wrote", dst, len(text))
    write_symbol_lib(libsyms)
    patch_board_values(report)
    return report


def patch_board_values(report):
    """Altium footprints carry the Comment text as value ('approved sub OK'); use the schematic values."""
    path = os.path.join(OUT, "PALHASERVO.kicad_pcb")
    txt = open(path, encoding="utf-8").read()

    def sub(m):
        ref = m.group(2)
        v = report[ref][1] if ref in report else None
        return m.group(1) + '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"') if v else m.group(0)

    txt = re.sub(r'(\(property "Reference" "([^"]+)".*?\(property "Value" )"[^"]*"', sub, txt, flags=re.S)
    open(path, "w", encoding="utf-8").write(txt)


def power_symbol(lname, net, gnd):
    props = [["property", Q("Reference"), Q("#PWR"), ["at", 0, 0, 0], effects(1.27, hide=True)],
             ["property", Q("Value"), Q(net), ["at", 0, -3.81 if gnd else 3.81, 0], effects(1.27)],
             ["property", Q("Footprint"), Q(""), ["at", 0, 0, 0], effects(1.27, hide=True)],
             ["property", Q("Datasheet"), Q(""), ["at", 0, 0, 0], effects(1.27, hide=True)],
             ["property", Q("Description"), Q('Power symbol creates a global label with name "%s"' % net),
              ["at", 0, 0, 0], effects(1.27, hide=True)]]
    stroke = ["stroke", ["width", 0], ["type", "default"]]
    nofill = ["fill", ["type", "none"]]
    if gnd:
        g = [["polyline", ["pts", ["xy", 0, 0], ["xy", 0, -1.27], ["xy", 1.27, -1.27], ["xy", 0, -2.54],
                           ["xy", -1.27, -1.27], ["xy", 0, -1.27]], stroke, nofill]]
        ang = 270
    else:
        g = [["polyline", ["pts", ["xy", 0, 0], ["xy", 0, 1.905]], stroke, nofill],
             ["polyline", ["pts", ["xy", -1.27, 1.905], ["xy", 1.27, 1.905]], stroke, nofill]]
        ang = 90
    return (["symbol", Q(lname), ["power"], ["pin_numbers", ["hide", "yes"]],
             ["pin_names", ["offset", 0], ["hide", "yes"]], ["exclude_from_sim", "no"], ["in_bom", "no"],
             ["on_board", "no"]] + props
            + [["symbol", Q(lname + "_0_1")] + g,
               ["symbol", Q(lname + "_1_1"), ["pin", "power_in", "line", ["at", 0, 0, ang], ["length", 0],
                                              ["name", Q(net), effects(1.27)], ["number", Q("1"), effects(1.27)]]]])


def pwr_flag_symbol():
    stroke = ["stroke", ["width", 0], ["type", "default"]]
    props = [["property", Q("Reference"), Q("#FLG"), ["at", 0, 0, 0], effects(1.27, hide=True)],
             ["property", Q("Value"), Q("PWR_FLAG"), ["at", 0, 3.81, 0], effects(1.27)],
             ["property", Q("Footprint"), Q(""), ["at", 0, 0, 0], effects(1.27, hide=True)],
             ["property", Q("Datasheet"), Q(""), ["at", 0, 0, 0], effects(1.27, hide=True)],
             ["property", Q("Description"), Q("Special symbol for telling ERC where power comes from"),
              ["at", 0, 0, 0], effects(1.27, hide=True)]]
    return (["symbol", Q("PWR_FLAG"), ["power"], ["pin_numbers", ["hide", "yes"]],
             ["pin_names", ["offset", 0], ["hide", "yes"]], ["exclude_from_sim", "no"], ["in_bom", "no"],
             ["on_board", "no"]] + props
            + [["symbol", Q("PWR_FLAG_0_1"),
                ["polyline", ["pts", ["xy", 0, 0], ["xy", 0, 1.27], ["xy", -1.016, 1.905], ["xy", 0, 2.54],
                              ["xy", 1.016, 1.905], ["xy", 0, 1.27]], stroke, ["fill", ["type", "none"]]]],
               ["symbol", Q("PWR_FLAG_1_1"), ["pin", "power_out", "line", ["at", 0, 0, 90], ["length", 0],
                                              ["name", Q("pwr"), effects(1.27)], ["number", Q("1"), effects(1.27)]]]])


def renamed(node, prefix):
    n = list(node)
    n[1] = Q(prefix + str(n[1]))
    return n


def write_symbol_lib(libsyms):
    body = [["version", 20251024], ["generator", Q("sch_import.py")], ["generator_version", Q("10.0")]]
    body += [list(n) for n in libsyms.values()]
    with open(os.path.join(OUT, LIB + ".kicad_sym"), "w", encoding="utf-8") as f:
        f.write(sx(["kicad_symbol_lib"] + body) + "\n")
    print("wrote", LIB + ".kicad_sym", len(libsyms), "symbols")


def write_sch(sh, items, used, my_uuid, title, root=False):
    tb = ["title_block", ["title", Q(title)], ["rev", Q("V1I1")],
          ["comment", 1, Q("Imported from the Altium Designer project (see altium/)")]]
    paper = "A3"
    body = [["version", 20250114], ["generator", Q("eeschema")], ["generator_version", Q("9.0")],
            ["uuid", Q(my_uuid)], ["paper", Q(paper)], tb,
            ["lib_symbols"] + [renamed(n, LIB + ":") for n in used.values()]]
    body += items
    if root:
        body.append(["sheet_instances", ["path", Q("/"), ["page", Q("1")]]])
    else:
        body.append(["sheet_instances", ["path", Q("/"), ["page", Q(str(sh.page))]]])
    body.append(["embedded_fonts", "no"])
    return sx(["kicad_sch"] + body) + "\n"


if __name__ == "__main__":
    rep = convert()
    for k in sorted(rep):
        print(k, rep[k])

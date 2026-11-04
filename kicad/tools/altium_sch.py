"""Minimal reader for Altium .SchDoc files (OLE container, pipe-delimited records)."""
import struct
import zlib

import olefile


def _fix(v):
    try:
        return v.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return v


def load(path):
    """Return the records of a SchDoc as dicts, indexed like OWNERINDEX (header dropped)."""
    o = olefile.OleFileIO(path)
    d = o.openstream("FileHeader").read()
    recs, i = [], 0
    while i + 4 <= len(d):
        n = int.from_bytes(d[i:i + 3], "little")
        i += 4
        s = d[i:i + n].rstrip(b"\0").decode("latin-1")
        i += n
        r = {}
        for kv in s.split("|"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                r[k.upper()] = v
        for k in [k for k in r if k.startswith("%UTF8%")]:
            r[k[6:]] = _fix(r[k])
        recs.append(r)
    return recs[1:]


def load_images(path):
    """Return {original file name: BMP bytes} for embedded images."""
    o = olefile.OleFileIO(path)
    d = o.openstream("Storage").read()
    out = {}
    i = 4 + int.from_bytes(d[:3], "little")
    while i + 4 <= len(d):
        ln = int.from_bytes(d[i:i + 3], "little")
        rec = d[i + 4:i + 4 + ln]
        i += 4 + ln
        nl = rec[1]
        name = rec[2:2 + nl].decode("latin-1")
        dl = int.from_bytes(rec[2 + nl:6 + nl], "little")
        out[name] = zlib.decompress(rec[6 + nl:6 + nl + dl])
    return out


def bmp_to_png(b, shrink=1):
    """Convert an uncompressed 24/32-bit BMP to PNG bytes (optionally box-downscaled)."""
    off = struct.unpack_from("<I", b, 10)[0]
    w, h, _, bpp = struct.unpack_from("<iiHH", b, 18)
    flip = h > 0
    h = abs(h)
    bpp //= 8
    stride = (w * bpp + 3) & ~3
    rows = []
    for y in range(h):
        sy = h - 1 - y if flip else y
        row = b[off + sy * stride: off + sy * stride + w * bpp]
        rows.append([(row[x * bpp + 2], row[x * bpp + 1], row[x * bpp]) for x in range(w)])
    if shrink > 1:
        nw, nh = w // shrink, h // shrink
        nr = []
        for y in range(nh):
            r = []
            for x in range(nw):
                px = [rows[y * shrink + j][x * shrink + k] for j in range(shrink) for k in range(shrink)]
                r.append(tuple(sum(c[i] for c in px) // len(px) for i in range(3)))
            nr.append(r)
        rows, w, h = nr, nw, nh
    raw = b"".join(b"\0" + bytes(c for p in r for c in p) for r in rows)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

"""
SLD Parser — Single-Line Diagram (vector PDF) → network topology
================================================================

Parses a vector-PDF single-line diagram and extracts the electrical network
topology (buses, lines, generators, loads, transformers) so it can be rebuilt
in DIgSILENT PowerFactory.

Pipeline
--------
    extract_elements(pdf_path)   raw vector paths + text spans (pymupdf)
        -> classify_elements()   geometry heuristics -> typed dataclasses
        -> build_topology()       connectivity (NetworkX) + impedance defaults

The public entry point is ``parse_sld()`` which runs all three stages and
optionally applies user corrections from an ``sld_overrides.json`` file.

This module has NO dependency on PowerFactory; it produces a plain ``dict``
topology that ``pf_network_builder.py`` consumes.

Dependencies: pymupdf (imported as ``fitz``), networkx, shapely.

Author
------
  Andrea Pomarico
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, asdict
from typing import Optional


# ══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════

@dataclass
class ParseConfig:
    """Tunable thresholds for the geometric heuristics (PDF point units)."""

    page_index: int = 0

    # Bus detection: long, near-horizontal strokes.
    bus_min_length: float = 40.0
    bus_horizontal_tol: float = 5.0      # max |dy| for a stroke to count as horizontal

    # Branch detection: strokes connecting two distinct buses.
    branch_min_length: float = 8.0

    # Symbol detection: compact, roughly square drawings.
    symbol_max_size: float = 80.0        # max bbox side for a symbol
    symbol_aspect_tol: float = 0.45      # |w-h|/max(w,h) below this == "square-ish"

    # Proximity tolerances.
    snap_tol: float = 12.0               # endpoint-to-bus snapping distance
    label_max_dist: float = 60.0         # max distance from element to claim a label
    transformer_pair_dist: float = 30.0  # max centre distance for stacked circles

    # Default per-unit / rating values when not readable from the PDF.
    default_voltage_kv: float = 110.0
    default_line_r_ohm: float = 0.1
    default_line_x_ohm: float = 0.4
    default_line_length_km: float = 1.0
    default_gen_mw: float = 100.0
    default_gen_mva: float = 120.0
    default_load_mw: float = 50.0
    default_load_mvar: float = 15.0
    default_trafo_mva: float = 100.0


# ══════════════════════════════════════════════════════════════════
# GEOMETRY PRIMITIVES
# ══════════════════════════════════════════════════════════════════

@dataclass
class Segment:
    """A straight stroke between two points."""
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    @property
    def dx(self) -> float:
        return abs(self.x2 - self.x1)

    @property
    def dy(self) -> float:
        return abs(self.y2 - self.y1)

    @property
    def midpoint(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def endpoints(self) -> list[tuple[float, float]]:
        return [(self.x1, self.y1), (self.x2, self.y2)]


@dataclass
class Symbol:
    """A compact drawing that likely represents a device (gen/load/trafo)."""
    cx: float
    cy: float
    w: float
    h: float
    n_curves: int = 0
    n_lines: int = 0

    @property
    def center(self) -> tuple[float, float]:
        return (self.cx, self.cy)

    @property
    def size(self) -> float:
        return max(self.w, self.h)


@dataclass
class TextSpan:
    text: str
    cx: float
    cy: float


# ══════════════════════════════════════════════════════════════════
# TYPED NETWORK ELEMENTS (parser output)
# ══════════════════════════════════════════════════════════════════

@dataclass
class Bus:
    loc_name: str
    voltage_kv: float
    cx: float
    cy: float
    # geometry of the underlying horizontal stroke (for connectivity)
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0


@dataclass
class Line:
    loc_name: str
    bus1: str
    bus2: str
    r_ohm: float
    x_ohm: float
    length_km: float
    b_us_per_km: float = 0.0  # positive-sequence shunt susceptance (µS/km)


@dataclass
class Generator:
    loc_name: str
    bus: str
    p_mw: float
    s_mva: float
    v0_pu: float = 1.0
    bus_type: str = "PV"     # "slack", "PV", or "sync_cond"
    q_mvar: float = 0.0


@dataclass
class Load:
    loc_name: str
    bus: str
    p_mw: float
    q_mvar: float


@dataclass
class Transformer:
    loc_name: str
    bus_hv: str
    bus_lv: str
    s_mva: float
    x_pu: float = 0.1        # leakage reactance, per-unit on 100 MVA system base
    tap_ratio: float = 1.0   # off-nominal tap ratio (HV/LV)
    kv_hv: float = 69.0
    kv_lv: float = 13.8


@dataclass
class Shunt:
    loc_name: str
    bus: str
    q_mvar: float            # capacitive reactive power (Mvar)


# ══════════════════════════════════════════════════════════════════
# STAGE 1 — RAW EXTRACTION
# ══════════════════════════════════════════════════════════════════

def extract_elements(pdf_path: str, cfg: Optional[ParseConfig] = None) -> dict:
    """Extract raw vector strokes, compact symbols, and text spans from a PDF.

    Returns a dict with keys: ``segments`` (list[Segment]),
    ``symbols`` (list[Symbol]), ``texts`` (list[TextSpan]),
    and ``page_size`` (tuple[w, h]).
    """
    cfg = cfg or ParseConfig()

    try:
        import fitz  # pymupdf
    except ImportError as e:  # pragma: no cover - dependency guard
        raise ImportError(
            "pymupdf is required for SLD parsing. Install with `pip install pymupdf`."
        ) from e

    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(pdf_path)
    try:
        if cfg.page_index >= len(doc):
            raise IndexError(
                f"page_index {cfg.page_index} out of range (PDF has {len(doc)} pages)"
            )
        page = doc[cfg.page_index]
        page_size = (page.rect.width, page.rect.height)

        segments: list[Segment] = []
        symbols: list[Symbol] = []

        for drawing in page.get_drawings():
            items = drawing.get("items", [])
            line_items: list[Segment] = []
            n_curves = 0

            for it in items:
                kind = it[0]
                if kind == "l":  # line: ("l", p1, p2)
                    p1, p2 = it[1], it[2]
                    line_items.append(Segment(p1.x, p1.y, p2.x, p2.y))
                elif kind == "re":  # rectangle: ("re", Rect, ...)
                    r = it[1]
                    # decompose rectangle edges into segments
                    line_items.extend([
                        Segment(r.x0, r.y0, r.x1, r.y0),
                        Segment(r.x1, r.y0, r.x1, r.y1),
                        Segment(r.x1, r.y1, r.x0, r.y1),
                        Segment(r.x0, r.y1, r.x0, r.y0),
                    ])
                elif kind in ("c", "qu"):  # bezier curve / quad => part of a symbol
                    n_curves += 1

            # Compute the drawing's bounding box.
            rect = drawing.get("rect")
            if rect is not None:
                w, h = rect.width, rect.height
                cx, cy = (rect.x0 + rect.x1) / 2.0, (rect.y0 + rect.y1) / 2.0
            else:
                xs, ys = [], []
                for s in line_items:
                    xs.extend([s.x1, s.x2])
                    ys.extend([s.y1, s.y2])
                if not xs:
                    continue
                w, h = max(xs) - min(xs), max(ys) - min(ys)
                cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)

            is_symbolic = n_curves > 0 and max(w, h) <= cfg.symbol_max_size
            if is_symbolic:
                symbols.append(Symbol(cx, cy, w, h, n_curves, len(line_items)))

            # Keep individual strokes for bus/branch detection regardless.
            segments.extend(line_items)

        texts: list[TextSpan] = []
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    raw = (span.get("text") or "").strip()
                    if not raw:
                        continue
                    bbox = span.get("bbox", (0, 0, 0, 0))
                    cx = (bbox[0] + bbox[2]) / 2.0
                    cy = (bbox[1] + bbox[3]) / 2.0
                    texts.append(TextSpan(raw, cx, cy))

        return {
            "segments": segments,
            "symbols": symbols,
            "texts": texts,
            "page_size": page_size,
        }
    finally:
        doc.close()


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

_VOLTAGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*k?v", re.IGNORECASE)
_MW_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mw", re.IGNORECASE)
_MVA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mva", re.IGNORECASE)
_MVAR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mvar", re.IGNORECASE)


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _nearest_text(point: tuple[float, float], texts: list[TextSpan],
                  max_dist: float) -> Optional[TextSpan]:
    best, best_d = None, max_dist
    for t in texts:
        d = _dist(point, (t.cx, t.cy))
        if d <= best_d:
            best, best_d = t, d
    return best


def _parse_voltage_kv(text: str, default: float) -> float:
    m = _VOLTAGE_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return default


def _sanitize_name(raw: str, fallback: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip()
    name = name.replace("/", "_").replace("\\", "_")
    return name or fallback


def _point_near_bus(point: tuple[float, float], bus: Bus, tol: float) -> bool:
    """True if a point lies near a bus's horizontal stroke (within tol)."""
    # distance from point to the (near-horizontal) bus segment
    x, y = point
    x_lo, x_hi = min(bus.x1, bus.x2) - tol, max(bus.x1, bus.x2) + tol
    bus_y = (bus.y1 + bus.y2) / 2.0
    return (x_lo <= x <= x_hi) and (abs(y - bus_y) <= tol)


# ══════════════════════════════════════════════════════════════════
# STAGE 2 — CLASSIFICATION
# ══════════════════════════════════════════════════════════════════

def classify_elements(raw: dict, cfg: Optional[ParseConfig] = None) -> dict:
    """Classify raw primitives into buses, branches, and device symbols.

    Returns a dict with keys: ``buses`` (list[Bus]),
    ``branches`` (list[Segment]), and ``symbols`` (list[Symbol]).
    """
    cfg = cfg or ParseConfig()
    segments: list[Segment] = raw["segments"]
    symbols: list[Symbol] = raw["symbols"]
    texts: list[TextSpan] = raw["texts"]

    # -- Buses: long, near-horizontal strokes -------------------------
    bus_segments = [
        s for s in segments
        if s.dy <= cfg.bus_horizontal_tol and s.length >= cfg.bus_min_length
    ]
    bus_segments.sort(key=lambda s: s.length, reverse=True)

    buses: list[Bus] = []
    used_names: set[str] = set()
    for idx, seg in enumerate(bus_segments):
        mid = seg.midpoint
        label = _nearest_text(mid, texts, cfg.label_max_dist)
        raw_name = label.text if label else f"Bus_{idx + 1}"
        name = _sanitize_name(raw_name, f"Bus_{idx + 1}")
        # ensure uniqueness
        base, n = name, 2
        while name in used_names:
            name = f"{base}_{n}"
            n += 1
        used_names.add(name)

        voltage = _parse_voltage_kv(label.text, cfg.default_voltage_kv) if label \
            else cfg.default_voltage_kv

        buses.append(Bus(
            loc_name=name, voltage_kv=voltage,
            cx=mid[0], cy=mid[1],
            x1=seg.x1, y1=seg.y1, x2=seg.x2, y2=seg.y2,
        ))

    # -- Branches: connecting strokes that are NOT buses --------------
    branches = [
        s for s in segments
        if not (s.dy <= cfg.bus_horizontal_tol and s.length >= cfg.bus_min_length)
        and s.length >= cfg.branch_min_length
    ]

    return {"buses": buses, "branches": branches, "symbols": symbols, "texts": texts}


def _bus_at_point(point: tuple[float, float], buses: list[Bus],
                  tol: float) -> Optional[Bus]:
    for bus in buses:
        if _point_near_bus(point, bus, tol):
            return bus
    # fallback: nearest bus centre within tol
    best, best_d = None, tol
    for bus in buses:
        d = _dist(point, (bus.cx, bus.cy))
        if d <= best_d:
            best, best_d = bus, d
    return best


# ══════════════════════════════════════════════════════════════════
# STAGE 3 — TOPOLOGY
# ══════════════════════════════════════════════════════════════════

def build_topology(classified: dict, cfg: Optional[ParseConfig] = None) -> dict:
    """Resolve connectivity into a typed topology dict.

    Returns a dict with keys ``buses``, ``lines``, ``generators``,
    ``loads``, ``transformers`` (each a list of dataclass instances) and a
    NetworkX ``graph`` describing bus-to-bus connectivity.
    """
    try:
        import networkx as nx
    except ImportError as e:  # pragma: no cover - dependency guard
        raise ImportError(
            "networkx is required for SLD parsing. Install with `pip install networkx`."
        ) from e

    cfg = cfg or ParseConfig()
    buses: list[Bus] = classified["buses"]
    branches: list[Segment] = classified["branches"]
    symbols: list[Symbol] = classified["symbols"]
    texts: list[TextSpan] = classified["texts"]

    graph = nx.Graph()
    for bus in buses:
        graph.add_node(bus.loc_name, type="bus", kv=bus.voltage_kv)

    lines: list[Line] = []
    transformers: list[Transformer] = []
    line_idx = trafo_idx = 0

    # Identify transformer symbols (stacked circles) up front so the branch
    # passing through them becomes a transformer rather than a line.
    trafo_symbols = _detect_transformer_symbols(symbols, cfg)

    used_branch_names: set[str] = set()
    for seg in branches:
        e1 = _bus_at_point((seg.x1, seg.y1), buses, cfg.snap_tol)
        e2 = _bus_at_point((seg.x2, seg.y2), buses, cfg.snap_tol)
        if e1 is None or e2 is None or e1.loc_name == e2.loc_name:
            continue

        # If a transformer symbol sits on this branch, classify as transformer.
        passes_trafo = any(
            _segment_passes_point(seg, sym.center, cfg.transformer_pair_dist)
            for sym in trafo_symbols
        )

        if passes_trafo:
            trafo_idx += 1
            # HV = higher nominal voltage bus
            hv, lv = (e1, e2) if e1.voltage_kv >= e2.voltage_kv else (e2, e1)
            name = _unique(f"Trafo_{trafo_idx}", used_branch_names)
            transformers.append(Transformer(
                loc_name=name, bus_hv=hv.loc_name, bus_lv=lv.loc_name,
                s_mva=cfg.default_trafo_mva,
            ))
            graph.add_edge(e1.loc_name, e2.loc_name, type="transformer", name=name)
        else:
            line_idx += 1
            mid = seg.midpoint
            label = _nearest_text(mid, texts, cfg.label_max_dist)
            raw_name = label.text if label else f"Line_{line_idx}"
            name = _unique(_sanitize_name(raw_name, f"Line_{line_idx}"),
                           used_branch_names)
            lines.append(Line(
                loc_name=name, bus1=e1.loc_name, bus2=e2.loc_name,
                r_ohm=cfg.default_line_r_ohm, x_ohm=cfg.default_line_x_ohm,
                length_km=cfg.default_line_length_km,
            ))
            graph.add_edge(e1.loc_name, e2.loc_name, type="line", name=name)

    # -- Device symbols attached to a single bus (gen / load) ---------
    generators: list[Generator] = []
    loads: list[Load] = []
    gen_idx = load_idx = 0
    used_dev_names: set[str] = set()

    trafo_centers = {(round(s.cx, 1), round(s.cy, 1)) for s in trafo_symbols}

    for sym in symbols:
        if (round(sym.cx, 1), round(sym.cy, 1)) in trafo_centers:
            continue  # already consumed as a transformer

        host_bus = _bus_at_point(sym.center, buses, cfg.snap_tol * 3)
        if host_bus is None:
            continue

        label = _nearest_text(sym.center, texts, cfg.label_max_dist)
        label_text = label.text if label else ""
        kind = _classify_symbol_kind(sym, label_text)

        if kind == "generator":
            gen_idx += 1
            name = _unique(_sanitize_name(label_text or f"Gen_{gen_idx}",
                                          f"Gen_{gen_idx}"), used_dev_names)
            generators.append(Generator(
                loc_name=name, bus=host_bus.loc_name,
                p_mw=_first_match(_MW_RE, label_text, cfg.default_gen_mw),
                s_mva=_first_match(_MVA_RE, label_text, cfg.default_gen_mva),
            ))
            graph.add_node(name, type="generator")
            graph.add_edge(name, host_bus.loc_name, type="gen_connection")
        elif kind == "load":
            load_idx += 1
            name = _unique(_sanitize_name(label_text or f"Load_{load_idx}",
                                          f"Load_{load_idx}"), used_dev_names)
            loads.append(Load(
                loc_name=name, bus=host_bus.loc_name,
                p_mw=_first_match(_MW_RE, label_text, cfg.default_load_mw),
                q_mvar=_first_match(_MVAR_RE, label_text, cfg.default_load_mvar),
            ))
            graph.add_node(name, type="load")
            graph.add_edge(name, host_bus.loc_name, type="load_connection")

    return {
        "buses": buses,
        "lines": lines,
        "generators": generators,
        "loads": loads,
        "transformers": transformers,
        "graph": graph,
    }


# ── classification helpers ────────────────────────────────────────

def _unique(name: str, used: set[str]) -> str:
    base, n, out = name, 2, name
    while out in used:
        out = f"{base}_{n}"
        n += 1
    used.add(out)
    return out


def _first_match(regex: re.Pattern, text: str, default: float) -> float:
    m = regex.search(text or "")
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return default


def _segment_passes_point(seg: Segment, point: tuple[float, float],
                          tol: float) -> bool:
    """Distance from a point to a segment, within tol."""
    x, y = point
    x1, y1, x2, y2 = seg.x1, seg.y1, seg.x2, seg.y2
    dx, dy = x2 - x1, y2 - y1
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return _dist(point, (x1, y1)) <= tol
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / seg_len_sq))
    proj = (x1 + t * dx, y1 + t * dy)
    return _dist(point, proj) <= tol


def _detect_transformer_symbols(symbols: list[Symbol],
                                cfg: ParseConfig) -> list[Symbol]:
    """A transformer is two circle-like symbols stacked close together."""
    trafos: list[Symbol] = []
    candidates = [s for s in symbols if s.n_curves >= 1]
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            if _dist(a.center, b.center) <= cfg.transformer_pair_dist:
                # represent the transformer by the midpoint of the pair
                trafos.append(Symbol(
                    cx=(a.cx + b.cx) / 2.0, cy=(a.cy + b.cy) / 2.0,
                    w=max(a.w, b.w), h=a.h + b.h,
                    n_curves=a.n_curves + b.n_curves,
                ))
    return trafos


def _classify_symbol_kind(sym: Symbol, label_text: str) -> str:
    """Return 'generator', 'load', or 'unknown' for a device symbol."""
    t = label_text.lower()
    if any(k in t for k in ("gen", "generator", "~", "sync")):
        return "generator"
    if any(k in t for k in ("load", "lod", "mw", "mvar")):
        return "load"
    # geometry fallback: circle-ish (square bbox, curves) => generator;
    # otherwise treat as load.
    if sym.n_curves >= 2 and abs(sym.w - sym.h) / max(sym.w, sym.h, 1) <= 0.45:
        return "generator"
    return "load"


# ══════════════════════════════════════════════════════════════════
# OVERRIDES
# ══════════════════════════════════════════════════════════════════

def apply_overrides(topology: dict, overrides_path: str) -> dict:
    """Apply user corrections from an sld_overrides.json file (if present).

    Supported keys in the JSON file:
      - ``rename``:   {"OldName": "NewName", ...}
      - ``voltages``: {"BusName": kv, ...}
      - ``remove``:   ["ElementName", ...]
      - ``add_lines``: [{"loc_name","bus1","bus2","r_ohm","x_ohm","length_km"}]
      - ``reclassify``: {"ElementName": "generator"|"load"} (gen<->load swap)
    """
    if not overrides_path or not os.path.isfile(overrides_path):
        return topology

    with open(overrides_path, "r", encoding="utf-8") as fh:
        ov = json.load(fh)

    rename: dict = ov.get("rename", {})
    voltages: dict = ov.get("voltages", {})
    remove = set(ov.get("remove", []))
    reclassify: dict = ov.get("reclassify", {})

    def _rename(n: str) -> str:
        return rename.get(n, n)

    # rename + remove across all collections
    for bus in topology["buses"]:
        bus.loc_name = _rename(bus.loc_name)
        if bus.loc_name in voltages:
            bus.voltage_kv = float(voltages[bus.loc_name])
    topology["buses"] = [b for b in topology["buses"] if b.loc_name not in remove]

    for ln in topology["lines"]:
        ln.loc_name = _rename(ln.loc_name)
        ln.bus1, ln.bus2 = _rename(ln.bus1), _rename(ln.bus2)
    topology["lines"] = [x for x in topology["lines"] if x.loc_name not in remove]

    for g in topology["generators"]:
        g.loc_name = _rename(g.loc_name)
        g.bus = _rename(g.bus)
    for ld in topology["loads"]:
        ld.loc_name = _rename(ld.loc_name)
        ld.bus = _rename(ld.bus)

    # reclassify generator <-> load
    for name, target in reclassify.items():
        name = _rename(name)
        if target == "load":
            moved = [g for g in topology["generators"] if g.loc_name == name]
            topology["generators"] = [g for g in topology["generators"]
                                      if g.loc_name != name]
            for g in moved:
                topology["loads"].append(Load(g.loc_name, g.bus, g.p_mw, 0.0))
        elif target == "generator":
            moved = [ld for ld in topology["loads"] if ld.loc_name == name]
            topology["loads"] = [ld for ld in topology["loads"]
                                 if ld.loc_name != name]
            for ld in moved:
                topology["generators"].append(
                    Generator(ld.loc_name, ld.bus, ld.p_mw, ld.p_mw * 1.2))

    topology["generators"] = [g for g in topology["generators"]
                              if g.loc_name not in remove]
    topology["loads"] = [ld for ld in topology["loads"]
                         if ld.loc_name not in remove]
    for tr in topology["transformers"]:
        tr.loc_name = _rename(tr.loc_name)
        tr.bus_hv, tr.bus_lv = _rename(tr.bus_hv), _rename(tr.bus_lv)
    topology["transformers"] = [t for t in topology["transformers"]
                                if t.loc_name not in remove]

    # add manual lines
    for spec in ov.get("add_lines", []):
        topology["lines"].append(Line(
            loc_name=spec["loc_name"], bus1=spec["bus1"], bus2=spec["bus2"],
            r_ohm=float(spec.get("r_ohm", 0.1)),
            x_ohm=float(spec.get("x_ohm", 0.4)),
            length_km=float(spec.get("length_km", 1.0)),
        ))

    return topology


# ══════════════════════════════════════════════════════════════════
# TABLE PARSER — embedded IEEE-style datasheet text
# ══════════════════════════════════════════════════════════════════
#
# Many "reference" SLD PDFs (IEEE test cases, textbook one-lines, ETAP/
# PowerFactory exports) embed the full numeric dataset as text tables next to
# the drawing. When present, these tables are FAR more reliable than geometric
# heuristics on the vector strokes — they give the exact bus list, line R/X/B,
# transformer ratings, and load/generator data. ``parse_sld`` tries this path
# first and only falls back to geometry when no tables are found.

# Section headers that delimit the datasheet tables (order matters). Each entry
# maps a section key to one or more candidate header strings (different IEEE
# datasheets word them slightly differently).
_TBL_SECTIONS = [
    ("bus",   ("Bus Data",)),
    ("gen",   ("Generator Data", "Generator / Synchronous Machine Data")),
    ("trafo", ("Transformer Data",)),
    ("line",  ("Transmission Line Data",)),
    ("load",  ("Load Data",)),
    ("end",   ("Notes on data provenance",)),
]

# Row patterns (applied within their own section only).
_BUS_ROW   = re.compile(r"^\s*(\d{1,3})\s+(\d+(?:\.\d+)?)\s*kV\b", re.MULTILINE)
# Generator row: "Bus kV  <rest>" — the rest is tokenised because column layout
# varies (14-bus: "MVA V0 Role"; 39-bus: "Pg Qg Qmin/Qmax Vg Role").
_GEN_HEAD  = re.compile(r"^\s*(\d{1,3})\s+(\d+(?:\.\d+)?)\s*kV\s+(.+)$",
                        re.MULTILINE)
_TRAFO_ROW = re.compile(
    r"(\d{1,3})\s*-\s*(\d{1,3})\s+(\d+(?:\.\d+)?)\s*kV\s*/\s*(\d+(?:\.\d+)?)\s*kV"
    r"\s+([\d.]+)\s+([\d.]+)\s+(\d+(?:\.\d+)?)", re.MULTILINE)
# Line row: "a-b R X B [MVA] [length]" — B is the total charging susceptance;
# MVA (rating) and length are optional (39-bus omits length).
_LINE_ROW  = re.compile(
    r"^\s*(\d{1,3})\s*-\s*(\d{1,3})\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)"
    r"(?:\s+([\d.]+))?(?:\s+([\d.]+))?\s*$", re.MULTILINE)
_LOAD_ROW  = re.compile(r"^\s*(\d{1,3})\s+(-?[\d.]+)\s+(-?[\d.]+)\s*$",
                        re.MULTILINE)
_SHUNT_ROW = re.compile(r"^\s*(\d{1,3})\s*\(shunt\).*?\+?\s*(\d+(?:\.\d+)?)",
                        re.MULTILINE | re.IGNORECASE)


def _extract_full_text(pdf_path: str, cfg: ParseConfig) -> str:
    """Return the concatenated text of every page (datasheet tables may live
    on a separate page from the drawing)."""
    try:
        import fitz  # pymupdf
    except ImportError as e:  # pragma: no cover - dependency guard
        raise ImportError(
            "pymupdf is required for SLD parsing. Install with `pip install pymupdf`."
        ) from e
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    doc = fitz.open(pdf_path)
    try:
        return "\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()


def _split_sections(text: str) -> Optional[dict]:
    """Locate each datasheet section by its header. Returns None if the core
    tables are absent (so the caller can fall back to geometry)."""
    idx: dict[str, int] = {}
    for key, headers in _TBL_SECTIONS:
        pos = -1
        for h in headers:
            p = text.find(h)
            if p >= 0 and (pos < 0 or p < pos):
                pos = p
        idx[key] = pos
    # Need at least the bus + line tables for a meaningful model.
    if idx["bus"] < 0 or idx["line"] < 0:
        return None

    order = [k for k, _ in _TBL_SECTIONS]
    sections: dict[str, str] = {}
    for i, key in enumerate(order):
        start = idx[key]
        if start < 0:
            continue
        # section ends at the next header that actually exists
        end = len(text)
        for nxt in order[i + 1:]:
            if idx[nxt] > start:
                end = idx[nxt]
                break
        sections[key] = text[start:end]
    return sections


def parse_sld_tables(pdf_path: str,
                     cfg: Optional[ParseConfig] = None) -> Optional[dict]:
    """Parse an embedded IEEE-style datasheet (text tables) into a topology.

    Returns the same dict shape as :func:`build_topology`, or ``None`` if the
    PDF does not contain recognisable data tables.
    """
    try:
        import networkx as nx
    except ImportError as e:  # pragma: no cover - dependency guard
        raise ImportError(
            "networkx is required for SLD parsing. Install with `pip install networkx`."
        ) from e

    cfg = cfg or ParseConfig()
    text = _extract_full_text(pdf_path, cfg)
    sections = _split_sections(text)
    if sections is None:
        return None

    # -- Bus voltages -------------------------------------------------
    bus_kv: dict[int, float] = {}
    for m in _BUS_ROW.finditer(sections.get("bus", "")):
        bus_kv[int(m.group(1))] = float(m.group(2))
    if not bus_kv:
        return None

    def _bus_name(n: int) -> str:
        return f"Bus_{n}"

    def _kv(n: int) -> float:
        return bus_kv.get(n, cfg.default_voltage_kv)

    # -- Transmission lines (pu R/X on 100 MVA base -> ohms) ----------
    base_mva = 100.0

    # Collect raw line rows first so bus voltages can be reconciled before
    # impedances are scaled by the (per-endpoint) base voltage.
    raw_lines: list[tuple[int, int, float, float, float, float]] = []
    for m in _LINE_ROW.finditer(sections.get("line", "")):
        a, b = int(m.group(1)), int(m.group(2))
        r_pu, x_pu = float(m.group(3)), float(m.group(4))
        b_pu = float(m.group(5)) if m.group(5) else 0.0  # total charging (pu)
        length_raw = m.group(7)  # length column is optional
        length = float(length_raw) if length_raw else cfg.default_line_length_km
        raw_lines.append((a, b, r_pu, x_pu, b_pu, length))

    # Enforce voltage consistency: a transmission line connects buses of the
    # same nominal voltage. Some datasheets label tap-coupled buses at a lower
    # class even though they are joined to the main grid by lines (PowerFactory
    # then aborts load flow with "different voltage levels"). Propagate the
    # higher voltage across every line-connected group until stable.
    changed = True
    while changed:
        changed = False
        for a, b, *_ in raw_lines:
            va, vb = _kv(a), _kv(b)
            if va != vb:
                hv = max(va, vb)
                bus_kv[a] = hv
                bus_kv[b] = hv
                changed = True

    lines: list[Line] = []
    for a, b, r_pu, x_pu, b_pu, length in raw_lines:
        kv = _kv(a)
        z_base = (kv ** 2) / base_mva
        r_tot, x_tot = r_pu * z_base, x_pu * z_base
        # Total shunt susceptance B[S] = B_pu / Z_base; spread over the length so
        # PowerFactory recovers the same total when it multiplies by dline.
        b_tot_us = (b_pu / z_base) * 1e6  # microsiemens, whole line
        lines.append(Line(
            loc_name=f"L{a}_{b}", bus1=_bus_name(a), bus2=_bus_name(b),
            r_ohm=r_tot / length, x_ohm=x_tot / length, length_km=length,
            b_us_per_km=b_tot_us / length,
        ))

    # -- Transformers -------------------------------------------------
    transformers: list[Transformer] = []
    for m in _TRAFO_ROW.finditer(sections.get("trafo", "")):
        a, b = int(m.group(1)), int(m.group(2))
        kv_a, kv_b = float(m.group(3)), float(m.group(4))
        x_pu = float(m.group(5))
        tap = float(m.group(6))
        s_mva = float(m.group(7))
        # In "a-b kvA/kvB", kvA is bus a's winding, kvB is bus b's winding.
        bus_kv.setdefault(a, kv_a)
        bus_kv.setdefault(b, kv_b)
        # Winding voltages must match the (reconciled) bus nominal voltages so
        # PowerFactory does not reject the transformer. When both ends share a
        # voltage the unit degenerates to an in-phase tap branch.
        va, vb = _kv(a), _kv(b)
        if va >= vb:
            hv, lv, kv_hv, kv_lv = a, b, va, vb
        else:
            hv, lv, kv_hv, kv_lv = b, a, vb, va
        transformers.append(Transformer(
            loc_name=f"T{a}_{b}", bus_hv=_bus_name(hv), bus_lv=_bus_name(lv),
            s_mva=s_mva, x_pu=x_pu, tap_ratio=tap, kv_hv=kv_hv, kv_lv=kv_lv,
        ))

    # -- Generators / synchronous machines ----------------------------
    # Two datasheet layouts are supported (detected per row):
    #   * "Bus kV  MVA  V0  Role"                     (e.g. IEEE 14-bus)
    #   * "Bus kV  Pg  Qg  Qmin/Qmax  Vg  Role"       (e.g. IEEE 39-bus)
    generators: list[Generator] = []
    for m in _GEN_HEAD.finditer(sections.get("gen", "")):
        n = int(m.group(1))
        rest = m.group(3).strip()
        tokens = rest.split()
        if not tokens:
            continue

        p_mw = 0.0
        slash_idx = next((i for i, t in enumerate(tokens) if "/" in t), None)
        if slash_idx is not None and slash_idx + 1 < len(tokens):
            # 39-bus style: Pg Qg Qmin/Qmax Vg Role...
            try:
                p_mw = float(tokens[0])
            except ValueError:
                p_mw = 0.0
            try:
                v0 = float(tokens[slash_idx + 1])
            except ValueError:
                v0 = 1.0
            role = " ".join(tokens[slash_idx + 2:]).lower()
            # No explicit nameplate MVA -> size from Pg at 0.8 pf.
            s_mva = round(max(p_mw / 0.8, 1.0), 1)
        else:
            # 14-bus style: MVA V0 Role...
            try:
                s_mva = float(tokens[0])
            except ValueError:
                s_mva = cfg.default_gen_mva
            try:
                v0 = float(tokens[1]) if len(tokens) > 1 else 1.0
            except ValueError:
                v0 = 1.0
            role = " ".join(tokens[2:]).lower()

        if "slack" in role or "swing" in role:
            bus_type = "slack"
        elif "sync" in role or "cond" in role:
            bus_type = "sync_cond"
        else:
            bus_type = "PV"
        generators.append(Generator(
            loc_name=f"G_{n}", bus=_bus_name(n),
            p_mw=p_mw, s_mva=s_mva, v0_pu=v0, bus_type=bus_type,
        ))

    # The PDF text extractor frequently breaks the generator *table* into
    # columns, dropping the Pg dispatch and Role tokens (every machine then
    # parses as P=0, which starves the network and breaks the load flow).
    # Recover those values from the unambiguous one-line summaries that sit on
    # the diagram, e.g. "Bus 30: Pg=250 MW, Qg=162 MVAr / Vg=1.050 pu (PV ...)".
    # This only matches the "Pg="/"Vg=" prose, so datasheets without it (such as
    # the IEEE 14-bus, whose generators carry no Pg) are left untouched.
    pg_map = {int(m.group(1)): float(m.group(2))
              for m in re.finditer(r"Bus\s+(\d+):\s*Pg\s*=\s*([\d.]+)\s*MW",
                                   text, re.I)}
    vg_role: dict[int, tuple] = {}
    for m in re.finditer(
            r"Bus\s+(\d+):[\s\S]{0,120}?Vg\s*=\s*([\d.]+)\s*pu\s*\(([^)\n]*)",
            text, re.I):
        vg_role.setdefault(int(m.group(1)),
                           (float(m.group(2)), m.group(3).lower()))
    for g in generators:
        try:
            n = int(g.bus.split("_")[1])
        except (IndexError, ValueError):
            continue
        if n in pg_map:
            g.p_mw = pg_map[n]
            g.s_mva = round(max(g.p_mw / 0.8, g.s_mva, 1.0), 1)
        if n in vg_role:
            vg, role = vg_role[n]
            g.v0_pu = vg
            if "slack" in role or "swing" in role:
                g.bus_type = "slack"
            elif "sync" in role or "cond" in role:
                g.bus_type = "sync_cond"
            elif "pv" in role or "gen" in role:
                g.bus_type = "PV"

    # Robust slack resolution. The per-row "Role" token can be dropped when the
    # PDF text extractor splits that column onto its own line, so also honour an
    # explicit prose statement ("Bus 31 is the system slack/swing bus"). The
    # designated machine wins and any other accidental slack is demoted to PV so
    # the model keeps exactly one reference.
    slack_bus = None
    mslack = re.search(r"Bus\s+(\d+)\s+is\s+the\s+system\s+slack", text, re.I)
    if mslack:
        slack_bus = _bus_name(int(mslack.group(1)))
    if slack_bus is None and not any(g.bus_type == "slack" for g in generators):
        # fall back to a "(Slack)" tag sitting next to a bus number anywhere.
        mtag = re.search(r"(?:Bus\s+)?(\d+)\b[^\n]*\bslack\b", text, re.I)
        if mtag:
            slack_bus = _bus_name(int(mtag.group(1)))
    if slack_bus is not None and any(g.bus == slack_bus for g in generators):
        for g in generators:
            if g.bus == slack_bus:
                g.bus_type = "slack"
            elif g.bus_type == "slack":
                g.bus_type = "PV"

    # -- Loads + shunt capacitors -------------------------------------
    loads: list[Load] = []
    shunts: list[Shunt] = []
    for m in _LOAD_ROW.finditer(sections.get("load", "")):
        line_txt = m.group(0).lower()
        if "total" in line_txt:
            continue
        n = int(m.group(1))
        loads.append(Load(
            loc_name=f"Load_{n}", bus=_bus_name(n),
            p_mw=float(m.group(2)), q_mvar=float(m.group(3)),
        ))
    # Shunt capacitor rows look like "9 (shunt) - +19 (cap.)".
    for m in _SHUNT_ROW.finditer(sections.get("load", "")):
        n = int(m.group(1))
        shunts.append(Shunt(
            loc_name=f"Shunt_{n}", bus=_bus_name(n), q_mvar=float(m.group(2)),
        ))

    # -- Materialise buses (union of all references) ------------------
    buses: list[Bus] = [
        Bus(loc_name=_bus_name(n), voltage_kv=_kv(n), cx=0.0, cy=0.0)
        for n in sorted(bus_kv)
    ]

    # -- Connectivity graph -------------------------------------------
    graph = nx.Graph()
    for bus in buses:
        graph.add_node(bus.loc_name, type="bus", kv=bus.voltage_kv)
    for ln in lines:
        graph.add_edge(ln.bus1, ln.bus2, type="line", name=ln.loc_name)
    for tr in transformers:
        graph.add_edge(tr.bus_hv, tr.bus_lv, type="transformer", name=tr.loc_name)
    for g in generators:
        graph.add_node(g.loc_name, type="generator")
        graph.add_edge(g.loc_name, g.bus, type="gen_connection")
    for ld in loads:
        graph.add_node(ld.loc_name, type="load")
        graph.add_edge(ld.loc_name, ld.bus, type="load_connection")
    for sh in shunts:
        graph.add_node(sh.loc_name, type="shunt")
        graph.add_edge(sh.loc_name, sh.bus, type="shunt_connection")

    return {
        "buses": buses,
        "lines": lines,
        "generators": generators,
        "loads": loads,
        "transformers": transformers,
        "shunts": shunts,
        "graph": graph,
        "source": "tables",
    }


# ══════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════

def parse_sld(pdf_path: str,
              cfg: Optional[ParseConfig] = None,
              overrides_path: Optional[str] = None) -> dict:
    """Full pipeline: PDF -> typed topology dict (with optional overrides).

    Strategy
    --------
    1. If the PDF embeds an IEEE-style datasheet (text tables), parse those —
       they give an exact, unambiguous model.
    2. Otherwise fall back to geometric heuristics on the vector strokes.

    Parameters
    ----------
    pdf_path : str
        Absolute path to the vector SLD PDF.
    cfg : ParseConfig, optional
        Heuristic thresholds. Defaults are tuned for clean CAD exports.
    overrides_path : str, optional
        Path to an sld_overrides.json file with user corrections. If omitted,
        a file named ``sld_overrides.json`` next to the PDF is used when present.

    Returns
    -------
    dict
        Topology with keys: buses, lines, generators, loads, transformers,
        graph, and a ``summary`` count dict.
    """
    cfg = cfg or ParseConfig()

    topology = None
    try:
        topology = parse_sld_tables(pdf_path, cfg)
    except Exception:
        topology = None

    if not topology or not topology.get("buses"):
        raw = extract_elements(pdf_path, cfg)
        classified = classify_elements(raw, cfg)
        topology = build_topology(classified, cfg)
        topology["source"] = "geometry"

    if overrides_path is None:
        guess = os.path.join(os.path.dirname(os.path.abspath(pdf_path)),
                             "sld_overrides.json")
        overrides_path = guess if os.path.isfile(guess) else None
    if overrides_path:
        topology = apply_overrides(topology, overrides_path)

    topology["summary"] = {
        "buses": len(topology["buses"]),
        "lines": len(topology["lines"]),
        "generators": len(topology["generators"]),
        "loads": len(topology["loads"]),
        "transformers": len(topology["transformers"]),
        "shunts": len(topology.get("shunts", [])),
        "source": topology.get("source", "geometry"),
    }
    return topology


def topology_to_dict(topology: dict) -> dict:
    """Serialise a topology to plain dicts (drops the NetworkX graph)."""
    return {
        "buses": [asdict(b) for b in topology["buses"]],
        "lines": [asdict(x) for x in topology["lines"]],
        "generators": [asdict(g) for g in topology["generators"]],
        "loads": [asdict(ld) for ld in topology["loads"]],
        "transformers": [asdict(t) for t in topology["transformers"]],
        "shunts": [asdict(s) for s in topology.get("shunts", [])],
        "summary": topology.get("summary", {}),
    }

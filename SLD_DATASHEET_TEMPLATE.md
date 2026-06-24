# SLD Datasheet PDF Template

This document defines the canonical way to embed network data inside a
single-line-diagram (SLD) PDF so that
[`sld_parser.py`](sld_parser.py) reads it deterministically, with zero parse
errors, and the resulting PowerFactory model converges on the first load flow.

The format is **positional, one record per line**. It is backward compatible
with the existing IEEE 14-bus and IEEE 39-bus datasheets in this repo.

> Why this matters: the parser itself is reliable. What breaks parsing is *PDF
> authoring*. The PDF text extractor (`fitz.get_text("text")`) reads a real
> multi-cell table grid cell-by-cell and can reorder or split a row across
> several lines. In the IEEE 39-bus PDF this silently dropped every
> generator's `Pg` dispatch and the `Slack` role, leaving all generators at
> 0 MW so the load flow could not solve. The rules below prevent that.

---

## 1. PDF authoring rules (these are what actually prevent parse errors)

1. **Selectable text, not an image.** Export the PDF from Word / LaTeX / HTML /
   a CAD tool. A scanned or rasterised page has no extractable text and will
   not parse.
2. **One record = one physical text line.** Do **not** lay the data out as a
   word-processor / spreadsheet table with separate cells. Each cell becomes an
   independent text object and the extractor may emit them out of order or on
   separate lines. Render the data block as **preformatted monospaced text**
   (e.g. a `<pre>` block, a LaTeX `verbatim`/`listings` block, or a
   Courier-New paragraph), one row per line.
3. **No wrapping.** Every record must fit on one line. Use a landscape / wide
   page or a smaller font so no row wraps onto a second line.
4. **Single-space delimited, ASCII only.** Separate fields with spaces. Avoid
   tabs, non-breaking spaces, en-dashes, and other Unicode look-alikes. Use a
   plain hyphen `-` for branch ids (`6-11`).
5. **No nested parentheses** in any field. Write `(PV)` or `(PV hydro)`, never
   `(PV gen. (hydro))`.
6. **Dedicated data page(s).** Put the tables on their own page(s) in the
   section order listed below. The drawing can be on earlier pages.

---

## 2. Section headers (must appear verbatim, each on its own line)

The parser locates each block by these header strings
(`_TBL_SECTIONS` in [`sld_parser.py`](sld_parser.py)). Spell them exactly:

| Section | Header line (verbatim) |
|---|---|
| Buses | `Bus Data` |
| Generators | `Generator Data`  *(or)*  `Generator / Synchronous Machine Data` |
| Transformers | `Transformer Data` |
| Lines | `Transmission Line Data` |
| Loads (and shunts) | `Load Data` |
| End sentinel | `Notes on data provenance` |

Put any free-form commentary **after** the `Notes on data provenance` line so it
is never mistaken for data. At minimum the `Bus Data` and `Transmission Line
Data` sections must be present.

---

## 3. Row grammar per section

Each bullet gives the field order, units, and the parser regex the line must
satisfy. Fields in `[brackets]` are optional. All per-unit values are on the
**100 MVA system base** unless stated.

### 3.1 Bus Data  (`_BUS_ROW`)

```
<id> <Vnom> kV <type>
```

- `id` integer bus number, `Vnom` nominal line-to-line voltage in kV, `type`
  free text (`PQ`, `PV`, `Slack`, `PQ (load)` ...). Only `id` and `Vnom` are
  read; `type` is documentation.

Example: `11 345 kV PQ`

### 3.2 Generator Data  (`_GEN_HEAD`)

One line per machine, in **either** of these two layouts (auto-detected by the
presence of a `min/max` token):

```
<bus> <Vnom> kV <MVA> <V0> <Role>
<bus> <Vnom> kV <Pg> <Qg> <Qmin/Qmax> <Vg> <Role>
```

- `MVA` nameplate; `V0`/`Vg` voltage setpoint in pu; `Pg` MW, `Qg` MVAr;
  `Qmin/Qmax` as a single slash token (e.g. `-100/300`).
- `Role` keywords: `Slack` or `Swing` -> reference machine; `Sync.cond` or
  `cond` -> synchronous condenser (P forced to 0); anything containing `PV` or
  `Gen` -> PV generator.

Examples:
- `1 69 kV 615 1.060 Slack`
- `31 16.5 kV 677.9 221.6 -100/300 0.982 Slack`

**Generator redundancy rule (strongly recommended).** Generators are the most
fragile field to extract. In addition to the table row above, include **one
prose line per generator**. The parser recovers `Pg`, `Vg`, and `Role` from
these even if the table columns get mangled:

```
Bus <id>: Pg=<P> MW, Qg=<Q> MVAr, Vg=<V> pu (<Role>)
```

Example: `Bus 31: Pg=677.9 MW, Qg=221.6 MVAr, Vg=0.982 pu (Slack)`

If the system has an explicit swing bus, also state it once in prose so it is
unambiguous: `Bus 31 is the system slack/swing bus.`

### 3.3 Transformer Data  (`_TRAFO_ROW`)

```
<hv>-<lv> <HVkV> kV/<LVkV> kV <X_pu> <tap_pu> <MVA> [<MVA_rateA>]
```

- `hv`/`lv` bus numbers; winding voltages in kV; `X_pu` leakage reactance per
  unit on the **system** base; `tap_pu` off-nominal ratio; `MVA` nameplate.

Example: `12-11 345 kV/138 kV 0.0435 1.006 300 500`

### 3.4 Transmission Line Data  (`_LINE_ROW`)

```
<from>-<to> <R_pu> <X_pu> <B_pu> [<MVA>] [<length_km>]
```

- `R_pu`, `X_pu` series impedance and `B_pu` total charging susceptance, all per
  unit on the 100 MVA base. `MVA` rating and `length_km` are optional (the
  39-bus omits length).

Example: `6-11 0.0007 0.0082 0.1389 480`

### 3.5 Load Data  (`_LOAD_ROW`)  — inside the `Load Data` section

```
<id> <P_MW> <Q_MVAr>
```

Example: `12 8.53 88`

A `Total ...` summary line is ignored. Negative Q is allowed.

### 3.6 Shunt capacitor  (`_SHUNT_ROW`)  — inside the `Load Data` section

```
<id> (shunt) - +<Qcap_MVAr> (cap.)
```

Example: `9 (shunt) - +19 (cap.)`

---

## 4. Consistency rules (so the parse is valid AND the load flow solves)

- **Equal-voltage lines.** A `Transmission Line Data` branch must connect two
  buses of the **same** `Vnom`. If the two ends are at different voltages, model
  it in `Transformer Data` instead. (The parser now force-reconciles mismatches,
  but the source data should be correct.)
- **Exactly one `Slack`** generator across the whole system.
- **Closed bus set.** Every bus id referenced by a generator, transformer, line,
  load, or shunt must exist in `Bus Data`.
- **Power balance.** Total generator `Pg` should be within a few percent of
  total load `P` (the slack covers the remainder plus losses). All generators at
  0 MW with a large load will not converge.
- **Bases.** Line `R/X/B` and transformer `X` per-unit on 100 MVA; transformer
  `tap` as a ratio (1.0 = nominal).

---

## 5. Complete worked example

Copy the block below verbatim into a monospaced / preformatted region of a
PDF (landscape, one line per record) to get a fully conformant 3-bus datasheet.
It exercises every section, both generator layouts, the prose redundancy line, a
transformer, a line, a load, and a shunt.

```
3-Bus Example System - 100 MVA, 60 Hz system base

Bus Data
1 345 kV Slack
2 345 kV PV
3 16.5 kV PV / Gen

Generator Data
1 345 kV 1000 1.030 Slack
3 16.5 kV 250.0 90.0 -100/300 1.050 PV
Bus 1: Pg=0 MW, Qg=0 MVAr, Vg=1.030 pu (Slack)
Bus 3: Pg=250 MW, Qg=90 MVAr, Vg=1.050 pu (PV)
Bus 1 is the system slack/swing bus.

Transformer Data
2-3 345 kV/16.5 kV 0.0200 1.025 300 500

Transmission Line Data
1-2 0.0035 0.0411 0.6987 600

Load Data
2 200 80
2 (shunt) - +50 (cap.)
Total 200 80

Notes on data provenance
Any free-form commentary can go here; it is never parsed as data.
```

Expected parse: 3 buses, 1 line, 1 transformer, 2 generators (one Slack), 1
load, 1 shunt; generation (1000 MW slack capacity + 250 MW dispatch) covers the
200 MW load; line 1-2 connects two 345 kV buses; the 345/16.5 kV step-up is a
transformer, not a line.

---

## 6. Optional follow-ups (not included here)

- A `validate_datasheet_pdf` tool that runs `parse_sld` on a candidate PDF and
  reports missing sections, unmatched rows, voltage-mismatched lines, a missing
  slack, and the generation-vs-load balance (reusing the diagnostics in
  [`Agent_DIgSILENT.py`](Agent_DIgSILENT.py)).
- A generator that emits a conformant PDF directly from a topology dict.

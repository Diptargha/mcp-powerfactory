"""
PF Network Builder — topology dict → DIgSILENT PowerFactory model
=================================================================

Consumes the topology produced by ``sld_parser.parse_sld()`` and creates the
corresponding PowerFactory objects (ElmTerm, ElmLne, ElmSym, ElmLod, ElmTr2)
inside a target grid (ElmNet).

This module receives a live PowerFactory ``app`` handle from the caller; it
performs NO connection management and MUST be invoked on the dedicated
PowerFactory thread (see ``MCP_PowerFactory._pf``).

Design notes
------------
- Connectivity is the priority: every element is wired to its bus(es) through
  freshly created StaCubic cubicles. This yields a topologically correct model.
- Electrical ratings and equipment types are set on a best-effort basis. Each
  type-dependent attribute write is guarded so a missing/renamed attribute in a
  given PowerFactory version downgrades to a warning instead of aborting the
  whole build. Users can refine types inside PowerFactory afterwards.

Author
------
  Andrea Pomarico
"""

from __future__ import annotations

from typing import Any, Optional


class _NullLog:
    """Fallback logger when none is supplied."""
    def info(self, *_a, **_k): ...
    def ok(self, *_a, **_k): ...
    def warn(self, *_a, **_k): ...
    def error(self, *_a, **_k): ...
    def section(self, *_a, **_k): ...


def _safe_set(obj: Any, attr: str, value: Any, warnings: list[str],
              ctx: str) -> bool:
    """Set an attribute defensively; record a warning on failure."""
    try:
        if hasattr(obj, "SetAttribute"):
            obj.SetAttribute(attr, value)
        else:  # pragma: no cover - PF objects always expose SetAttribute
            setattr(obj, attr, value)
        return True
    except Exception as e:
        warnings.append(f"{ctx}: could not set '{attr}'={value} ({e})")
        return False


def _set_first(obj: Any, attrs: list[str], value: Any, warnings: list[str],
               ctx: str) -> bool:
    """Try several candidate attribute names; stop at the first that works."""
    for attr in attrs:
        try:
            obj.SetAttribute(attr, value)
            return True
        except Exception:
            continue
    warnings.append(f"{ctx}: none of {attrs} accepted value {value}")
    return False


def _get_or_create_grid(app, network_name: str, log) -> Any:
    """Return a fresh ElmNet named ``network_name``.

    For a clean, idempotent rebuild any pre-existing grid of the same name is
    deleted first (otherwise re-running would create duplicate buses/lines).
    """
    netdat = app.GetProjectFolder("netdat")
    if netdat is None:
        raise RuntimeError("Network data folder not found (GetProjectFolder('netdat'))")

    for grid in netdat.GetContents("*.ElmNet") or []:
        if getattr(grid, "loc_name", None) == network_name:
            log.info(f"Deleting existing grid for clean rebuild: {network_name}")
            try:
                grid.Delete()
            except Exception as e:
                log.warn(f"Could not delete existing grid '{network_name}': {e}")

    grid = netdat.CreateObject("ElmNet", network_name)
    if grid is None:
        raise RuntimeError(f"Failed to create grid '{network_name}'")
    try:
        grid.Activate()
    except Exception as e:
        log.warn(f"Grid created but Activate() failed: {e}")
    log.ok(f"Created grid: {network_name}")
    return grid


def _new_cubicle(terminal, name: str):
    """Create a connection cubicle (StaCubic) inside a terminal."""
    return terminal.CreateObject("StaCubic", name)


def build_network(app,
                  topology: dict,
                  network_name: str = "SLD_Network",
                  log: Optional[object] = None) -> dict:
    """Create PowerFactory objects for a parsed SLD topology.

    Parameters
    ----------
    app : powerfactory application handle
        Live PowerFactory application (already connected, project active).
    topology : dict
        Output of ``sld_parser.parse_sld`` (buses, lines, generators, loads,
        transformers). Items may be dataclasses or plain dicts.
    network_name : str
        Name of the target grid (ElmNet) to create/populate.
    log : object, optional
        Logger exposing info/ok/warn/error. Defaults to a silent logger.

    Returns
    -------
    dict
        {created: {...counts...}, warnings: [...], success: bool, grid: name}
    """
    log = log or _NullLog()
    warnings: list[str] = []

    def _attr(item, name, default=None):
        """Read a field from a dataclass or dict uniformly."""
        if isinstance(item, dict):
            return item.get(name, default)
        return getattr(item, name, default)

    grid = _get_or_create_grid(app, network_name, log)

    # ── Buses (ElmTerm) ───────────────────────────────────────────
    bus_objs: dict[str, Any] = {}
    bus_kv: dict[str, float] = {}
    for bus in topology.get("buses", []):
        name = _attr(bus, "loc_name")
        if not name:
            continue
        elm = grid.CreateObject("ElmTerm", name)
        if elm is None:
            warnings.append(f"bus '{name}': CreateObject returned None")
            continue
        kv = _attr(bus, "voltage_kv", 110.0)
        _safe_set(elm, "uknom", kv, warnings, f"bus '{name}'")
        # phase technology: 3-phase AC (best effort)
        _safe_set(elm, "systype", 0, warnings, f"bus '{name}'")
        bus_objs[name] = elm
        bus_kv[name] = kv
    log.ok(f"Created {len(bus_objs)} bus(es)")

    # Container for equipment types (TypLne). Prefer the project's equipment
    # type library; fall back to the grid itself if unavailable.
    try:
        type_folder = app.GetProjectFolder("equip") or grid
    except Exception:
        type_folder = grid

    cub_counter = {"n": 0}

    def _connect(elm, terminal, attr_candidates, ctx):
        cub_counter["n"] += 1
        cub = _new_cubicle(terminal, f"Cub_{cub_counter['n']}")
        if cub is None:
            warnings.append(f"{ctx}: failed to create cubicle")
            return False
        return _set_first(elm, attr_candidates, cub, warnings, ctx)

    # ── Lines (ElmLne) ────────────────────────────────────────────
    n_lines = 0
    for ln in topology.get("lines", []):
        name = _attr(ln, "loc_name")
        b1, b2 = _attr(ln, "bus1"), _attr(ln, "bus2")
        t1, t2 = bus_objs.get(b1), bus_objs.get(b2)
        if t1 is None or t2 is None:
            warnings.append(f"line '{name}': missing bus(es) {b1}/{b2} — skipped")
            continue
        elm = grid.CreateObject("ElmLne", name)
        if elm is None:
            warnings.append(f"line '{name}': CreateObject returned None")
            continue
        _connect(elm, t1, ["bus1"], f"line '{name}' bus1")
        _connect(elm, t2, ["bus2"], f"line '{name}' bus2")
        length = _attr(ln, "length_km", 1.0) or 1.0
        _safe_set(elm, "dline", length, warnings, f"line '{name}'")

        # R/X per length live on a TypLne, not on the ElmLne. Create a
        # dedicated type carrying the parsed positive-sequence impedance
        # (ohm/km) and the line's rated voltage, then attach it.
        for stale in type_folder.GetContents(f"typ_{name}.TypLne") or []:
            try:
                stale.Delete()
            except Exception:
                pass
        typ = type_folder.CreateObject("TypLne", f"typ_{name}")
        if typ is not None:
            _safe_set(typ, "rline", _attr(ln, "r_ohm", 0.1), warnings,
                      f"type '{name}'")
            _safe_set(typ, "xline", _attr(ln, "x_ohm", 0.4), warnings,
                      f"type '{name}'")
            _safe_set(typ, "uline", bus_kv.get(b1, 110.0), warnings,
                      f"type '{name}'")
            _safe_set(typ, "bline", _attr(ln, "b_us_per_km", 0.0), warnings,
                      f"type '{name}'")  # shunt charging susceptance (µS/km)
            _safe_set(typ, "sline", 1.0, warnings, f"type '{name}'")  # kA rating
            _safe_set(typ, "nlnph", 3, warnings, f"type '{name}'")
            _set_first(elm, ["typ_id"], typ, warnings, f"line '{name}' typ_id")
        else:
            warnings.append(f"line '{name}': could not create TypLne")
        n_lines += 1
    log.ok(f"Created {n_lines} line(s)")

    # ── Transformers (ElmTr2 + TypTr2) ────────────────────────────
    base_mva = 100.0  # system base the parsed per-unit X refers to
    n_trafos = 0
    for tr in topology.get("transformers", []):
        name = _attr(tr, "loc_name")
        bhv, blv = _attr(tr, "bus_hv"), _attr(tr, "bus_lv")
        thv, tlv = bus_objs.get(bhv), bus_objs.get(blv)
        if thv is None or tlv is None:
            warnings.append(f"trafo '{name}': missing bus(es) {bhv}/{blv} — skipped")
            continue
        elm = grid.CreateObject("ElmTr2", name)
        if elm is None:
            warnings.append(f"trafo '{name}': CreateObject returned None")
            continue
        _connect(elm, thv, ["bushv", "bus1"], f"trafo '{name}' HV")
        _connect(elm, tlv, ["buslv", "bus2"], f"trafo '{name}' LV")

        s_mva = _attr(tr, "s_mva", 100.0) or 100.0
        x_pu = _attr(tr, "x_pu", 0.1)
        kv_hv = _attr(tr, "kv_hv", 110.0)
        kv_lv = _attr(tr, "kv_lv", 13.8)
        tap_ratio = _attr(tr, "tap_ratio", 1.0) or 1.0
        # Short-circuit voltage (%) referred to the transformer's own MVA base:
        #   uk% = X_pu(system) * (S_rated / S_base) * 100
        uk_pct = x_pu * (s_mva / base_mva) * 100.0

        for stale in type_folder.GetContents(f"typ_{name}.TypTr2") or []:
            try:
                stale.Delete()
            except Exception:
                pass
        typ = type_folder.CreateObject("TypTr2", f"typ_{name}")
        if typ is not None:
            _safe_set(typ, "strn", s_mva, warnings, f"ttype '{name}'")
            _safe_set(typ, "utrn_h", kv_hv, warnings, f"ttype '{name}'")
            _safe_set(typ, "utrn_l", kv_lv, warnings, f"ttype '{name}'")
            _safe_set(typ, "uktr", uk_pct, warnings, f"ttype '{name}'")
            _safe_set(typ, "pcutr", 0.0, warnings, f"ttype '{name}'")  # copper losses
            # Vector group: wye/wye with ZERO phase shift. This is a per-unit
            # benchmark with no transformer phase displacement; a spurious shift
            # on the 11-12-13 transformer loop would inject huge circulating
            # flow and break load-flow convergence. ('vecgrp' itself is a derived
            # string and not directly writable, so set the components instead.)
            _safe_set(typ, "tr2cn_h", "YN", warnings, f"ttype '{name}'")
            _safe_set(typ, "tr2cn_l", "YN", warnings, f"ttype '{name}'")
            _safe_set(typ, "nt2ag", 0, warnings, f"ttype '{name}'")
            # Tap changer on HV side: 1% steps so we can realise the off-nominal ratio.
            _safe_set(typ, "tap_side", 0, warnings, f"ttype '{name}'")
            _safe_set(typ, "dutap", 1.0, warnings, f"ttype '{name}'")
            _safe_set(typ, "nntap0", 0, warnings, f"ttype '{name}'")
            _safe_set(typ, "ntpmn", -20, warnings, f"ttype '{name}'")
            _safe_set(typ, "ntpmx", 20, warnings, f"ttype '{name}'")
            _set_first(elm, ["typ_id"], typ, warnings, f"trafo '{name}' typ_id")
            # Off-nominal ratio -> tap position (HV 1% steps).
            nntap = int(round((tap_ratio - 1.0) * 100.0))
            _safe_set(elm, "nntap", nntap, warnings, f"trafo '{name}'")
        else:
            warnings.append(f"trafo '{name}': could not create TypTr2")
        n_trafos += 1
    log.ok(f"Created {n_trafos} transformer(s)")

    # ── Generators / synchronous machines (ElmSym + TypSym) ───────
    # av_mode 'constv' = local voltage control; ip_ctrl=1 marks the slack
    # (reference) machine. Sync. condensers are voltage-controlled with P=0.
    n_gens = 0
    slack_elems: list[Any] = []      # generators flagged as reference machine
    biggest = {"elm": None, "p": -1.0}  # fallback reference if none flagged
    for g in topology.get("generators", []):
        name = _attr(g, "loc_name")
        bus = _attr(g, "bus")
        term = bus_objs.get(bus)
        if term is None:
            warnings.append(f"gen '{name}': missing bus {bus} — skipped")
            continue
        elm = grid.CreateObject("ElmSym", name)
        if elm is None:
            warnings.append(f"gen '{name}': CreateObject returned None")
            continue
        _connect(elm, term, ["bus1"], f"gen '{name}'")

        s_mva = _attr(g, "s_mva", 120.0) or 120.0
        bus_type = _attr(g, "bus_type", "PV")
        is_slack = (bus_type == "slack")
        p_mw = 0.0 if bus_type == "sync_cond" else _attr(g, "p_mw", 0.0)
        if is_slack:
            slack_elems.append(elm)
        if p_mw > biggest["p"]:
            biggest = {"elm": elm, "p": p_mw}

        for stale in type_folder.GetContents(f"typ_{name}.TypSym") or []:
            try:
                stale.Delete()
            except Exception:
                pass
        typ = type_folder.CreateObject("TypSym", f"typ_{name}")
        if typ is not None:
            _safe_set(typ, "sgn", s_mva, warnings, f"gtype '{name}'")
            _safe_set(typ, "ugn", bus_kv.get(bus, 110.0), warnings, f"gtype '{name}'")
            _safe_set(typ, "cosn", 0.85, warnings, f"gtype '{name}'")
            _set_first(elm, ["typ_id"], typ, warnings, f"gen '{name}' typ_id")
        else:
            warnings.append(f"gen '{name}': could not create TypSym")

        _safe_set(elm, "ngnum", 1, warnings, f"gen '{name}'")
        _safe_set(elm, "pgini", p_mw, warnings, f"gen '{name}'")
        _safe_set(elm, "usetp", _attr(g, "v0_pu", 1.0), warnings, f"gen '{name}'")
        _safe_set(elm, "av_mode", "constv", warnings, f"gen '{name}'")
        _safe_set(elm, "ip_ctrl", 1 if is_slack else 0, warnings, f"gen '{name}'")
        n_gens += 1

    # A load flow needs exactly one reference (slack) machine. If the datasheet
    # role column did not flag one, promote the largest generator so ComLdf can
    # still run instead of aborting with "no reference machine".
    if not slack_elems and biggest["elm"] is not None:
        _safe_set(biggest["elm"], "ip_ctrl", 1, warnings, "gen (auto-slack)")
        warnings.append(
            f"no slack in datasheet — promoted '{biggest['elm'].loc_name}' "
            f"(P={biggest['p']} MW) to reference machine")
    log.ok(f"Created {n_gens} generator(s)")

    # ── Loads (ElmLod) ────────────────────────────────────────────
    n_loads = 0
    for ld in topology.get("loads", []):
        name = _attr(ld, "loc_name")
        bus = _attr(ld, "bus")
        term = bus_objs.get(bus)
        if term is None:
            warnings.append(f"load '{name}': missing bus {bus} — skipped")
            continue
        elm = grid.CreateObject("ElmLod", name)
        if elm is None:
            warnings.append(f"load '{name}': CreateObject returned None")
            continue
        _connect(elm, term, ["bus1"], f"load '{name}'")
        _safe_set(elm, "plini", _attr(ld, "p_mw", 50.0), warnings, f"load '{name}'")
        _safe_set(elm, "qlini", _attr(ld, "q_mvar", 15.0), warnings, f"load '{name}'")
        n_loads += 1
    log.ok(f"Created {n_loads} load(s)")

    # ── Shunt capacitors (ElmShnt) ────────────────────────────────
    n_shunts = 0
    for sh in topology.get("shunts", []):
        name = _attr(sh, "loc_name")
        bus = _attr(sh, "bus")
        term = bus_objs.get(bus)
        if term is None:
            warnings.append(f"shunt '{name}': missing bus {bus} — skipped")
            continue
        elm = grid.CreateObject("ElmShnt", name)
        if elm is None:
            warnings.append(f"shunt '{name}': CreateObject returned None")
            continue
        _connect(elm, term, ["bus1"], f"shunt '{name}'")
        _safe_set(elm, "shtype", 2, warnings, f"shunt '{name}'")  # 2 = C (capacitor)
        _safe_set(elm, "qcapn", _attr(sh, "q_mvar", 0.0), warnings, f"shunt '{name}'")
        _safe_set(elm, "ushnm", bus_kv.get(bus, 110.0), warnings, f"shunt '{name}'")
        n_shunts += 1
    log.ok(f"Created {n_shunts} shunt(s)")

    created = {
        "buses": len(bus_objs),
        "lines": n_lines,
        "transformers": n_trafos,
        "generators": n_gens,
        "loads": n_loads,
        "shunts": n_shunts,
    }
    log.ok(f"Network build complete in grid '{network_name}': {created}")

    return {
        "grid": network_name,
        "created": created,
        "warnings": warnings,
        "success": True,
    }

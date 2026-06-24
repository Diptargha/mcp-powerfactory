"""
╔══════════════════════════════════════════════════════════════════╗
║           DIGSILENT AGENT — Standalone RMS Simulation            ║
╚══════════════════════════════════════════════════════════════════╝

Author
------
  Andrea Pomarico
  
"""

import sys
import os
import json
import csv
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── PowerFactory Python path ──────────────────────────────────────
# The bundled vendor `powerfactory` module lives next to the PowerFactory
# installation. Its directory is resolved lazily (from powermcp config, with an
# environment-variable fallback) and injected onto sys.path only at the moment
# of the deferred `import powerfactory`, never at module import time.

def _powerfactory_python_path():
    import os
    try:
        from powermcp.config import get_path
        p = get_path("powerfactory", "python_path", must_exist=False)
        if p:
            return p
    except Exception:
        pass
    return os.environ.get("POWERFACTORY_PYTHON_PATH") or os.environ.get("PYTHONPATH")


def _ensure_powerfactory_on_path():
    """Inject the PowerFactory python_path dir onto sys.path if not present."""
    raw = _powerfactory_python_path()
    if not raw:
        return
    for _path in [part.strip() for part in raw.split(os.pathsep) if part.strip()]:
        if _path not in sys.path:
            sys.path.append(_path)


# Deferred import: powerfactory is only available when PowerFactory is running.
# Importing it at module level would crash the MCP server on startup if PF isn't
# open yet.  The actual import happens inside connect() when a tool is invoked.
pf = None

# ══════════════════════════════════════════════════════════════════
# CONFIGURATION — edit this block to match your setup
# ══════════════════════════════════════════════════════════════════

@dataclass
class SimulationConfig:
    """All parameters needed to run one RMS simulation."""
    # ── Module ───────────────────────────────────────────────────
    powerfactory_python_path: str = ""
    # ── Project ───────────────────────────────────────────────────
    project_path: str = ""
    study_case:   str = r"ctocto"
    base_study_case: str = r"0. Base"

    # ── Fault ─────────────────────────────────────────────────────
    # fault_type : "bus"  → EvtShc ON + EvtShc OFF (clear)
    #              "line" → EvtShc ON + EvtSwitch OPEN (trip line)
    #              "gen_switch" → EvtSwitch on generator (open/close)
    fault_type:    str = "bus"
    fault_element: str = "Bus 01.ElmTerm"   # PF object name for the short-circuit
    switch_element: str = ""                # PF object name for generator switch event (e.g., Gen 05.ElmSym)
    t_switch: float = 1.0                    # time when generator switch is applied
    switch_state: int = 0                    # EvtSwitch.i_switch (0=open, 1=close)

    # ── RMS simulation timing (seconds) ──────────────────────────
    t_start: float = 0.0
    t_fault: float = 1.0    # time when fault is applied
    t_clear: float = 1.08   # fault clearance time  (FCT = 80 ms)
    t_end:   float = 10.0   # total simulation duration

    # ── Time step ─────────────────────────────────────────────────
    dt_rms: float = 0.01    # seconds

    # ── CSV output ────────────────────────────────────────────────
    output_dir:   str = r""
    run_label:    str = "run_001"
    result_name:  str = "All calculations.ElmRes"
    export_pfd: int = 0
    open_digsilent: int = 1
    word_document: int = 0
    final_word_document: int = 1
    final_presentation: int = 1

    # Set to 1 to enable optional LLM pipeline steps.
    # Disabled by default to reduce API quota usage on quick test runs.
    run_review_agent: int = 0
    run_final_report_agent: int = 0
    run_mitigation_agent: int = 0

    # ──────────────────────────────────────────────────────────────
    @classmethod
    def from_json(cls, path: str) -> "SimulationConfig":
        """Load config from a JSON file, overriding only the keys present."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**{k: v for k, v in data.items()
                      if k in cls.__dataclass_fields__})

    # ── Signals to export ─────────────────────────────────────────
    # Each entry: (object_name, variable_name, friendly_label)
    # Adjust names to match elements in your network model.
    signals: list = field(default_factory=lambda: [
        # Bus voltages (Texas Grid terminals)
        ("CRANE 0.ElmTerm",      "m:u",    "V_CRANE_pu"),
        ("HOUSTON 14 0.ElmTerm", "m:u",    "V_HOUSTON14_pu"),
        ("ABILENE 1 0.ElmTerm",  "m:u",    "V_ABILENE1_pu"),
        ("ALPINE 0.ElmTerm",     "m:u",    "V_ALPINE_pu"),

        # Generator speeds (Texas Grid synchronous machines)
        ("sym_1073_1.ElmSym",    "s:speed", "Speed_1073_pu"),
        ("sym_1033_1.ElmSym",    "s:speed", "Speed_1033_pu"),
        ("sym_1004_1.ElmSym",    "s:speed", "Speed_1004_pu"),

        # Generator rotor angles
        ("sym_1073_1.ElmSym",    "s:firel", "Angle_1073_deg"),
        ("sym_1033_1.ElmSym",    "s:firel", "Angle_1033_deg"),
    ])


# ══════════════════════════════════════════════════════════════════
# LOGGER
# ══════════════════════════════════════════════════════════════════

class Logger:
    """Simple timestamped console logger."""

    @staticmethod
    def info(msg: str):  print(f"[INFO]  {time.strftime('%H:%M:%S')} | {msg}")

    @staticmethod
    def ok(msg: str):    print(f"[OK]    {time.strftime('%H:%M:%S')} | ✅ {msg}")

    @staticmethod
    def warn(msg: str):  print(f"[WARN]  {time.strftime('%H:%M:%S')} | ⚠️  {msg}")

    @staticmethod
    def error(msg: str): print(f"[ERROR] {time.strftime('%H:%M:%S')} | ❌ {msg}")

    @staticmethod
    def section(title: str):
        bar = "═" * 60
        print(f"\n{bar}\n  {title}\n{bar}")


log = Logger()


# ══════════════════════════════════════════════════════════════════
# DIGSILENT AGENT
# ══════════════════════════════════════════════════════════════════

class DIgSILENTAgent:
    """
    Standalone agent that wraps the PowerFactory Python API.
    All public methods return (success: bool, message: str).
    """

    # Keep one PowerFactory handle per Python process.
    # PowerFactory cannot be started multiple times in the same process.
    _shared_app: Optional[object] = None
    _shared_project_path: Optional[str] = None
    _shared_project: Optional[object] = None
    _create_case_request_cache: dict[str, tuple[bool, str, float]] = {}
    _create_case_request_ttl_sec: int = 3600

    @classmethod
    def _apply_show_preference(cls, app, open_digsilent: bool = True) -> None:
        """Show PowerFactory window only when requested."""
        if not open_digsilent:
            return
        for attempt in range(1, 6):
            try:
                app.Show()
                return
            except Exception as e:
                if attempt == 5:
                    log.warn(f"app.Show() failed after 5 attempts: {e}")
                else:
                    log.warn(f"app.Show() attempt {attempt} failed: {e} — retrying in 2s")
                    time.sleep(2)

    def __init__(self, config: SimulationConfig):
        self.cfg = config
        self.app: Optional[object] = None
        self.project: Optional[object] = None
        self.result_objects: dict = {}   # label → PF result object
        # Subfolder for this run's outputs
        self.run_output_dir: str = ""

    def _ensure_run_output_dir(self) -> str:
        """Create and return the run-specific output subdirectory."""
        if not self.run_output_dir:
            safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.cfg.run_label)
            self.run_output_dir = os.path.join(self.cfg.output_dir, safe_label)
            os.makedirs(self.run_output_dir, exist_ok=True)
            log.info(f"Run output directory: {self.run_output_dir}")
        return self.run_output_dir

    @staticmethod
    def _find_study_case_exact(folder, case_name: str):
        """Return the IntCase whose loc_name exactly matches case_name."""
        try:
            cases = folder.GetContents("*.IntCase") or []
        except Exception:
            cases = folder.GetContents() or []

        for case in cases:
            if getattr(case, "loc_name", None) == case_name:
                return case
        return None

    def _find_study_case_in_project(self, case_name: str):
        """Find an IntCase anywhere in the active project by exact loc_name."""
        if not case_name:
            return None

        candidates = []
        try:
            candidates.extend(self.app.GetCalcRelevantObjects("*.IntCase") or [])
        except Exception:
            pass
        try:
            candidates.extend(self.app.GetCalcRelevantObjects(f"{case_name}.IntCase") or [])
        except Exception:
            pass
        if self.project is not None:
            try:
                candidates.extend(self.project.GetContents("*.IntCase", 1) or [])
            except Exception:
                pass

        seen = set()
        for case in candidates:
            case_id = id(case)
            if case_id in seen:
                continue
            seen.add(case_id)
            if getattr(case, "loc_name", None) == case_name:
                return case
        return None

    @staticmethod
    def _list_study_case_names(folder) -> set[str]:
        """Return all study case loc_name values in the study folder."""
        try:
            cases = folder.GetContents("*.IntCase") or []
        except Exception:
            cases = folder.GetContents() or []
        names = set()
        for case in cases:
            name = getattr(case, "loc_name", None)
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
        return names

    @classmethod
    def _prune_create_case_request_cache(cls) -> None:
        """Remove expired create_study_case idempotency entries."""
        now = time.time()
        expired = [
            key
            for key, (_, _, ts) in cls._create_case_request_cache.items()
            if now - ts > cls._create_case_request_ttl_sec
        ]
        for key in expired:
            cls._create_case_request_cache.pop(key, None)

    # ──────────────────────────────────────────────────────────────
    # STEP 1 — Connect to PowerFactory & activate project
    # ──────────────────────────────────────────────────────────────

    def connect(self) -> tuple[bool, str]:
        log.section("STEP 1 — Connect to PowerFactory")
        try:
            global pf
            open_digsilent = bool(getattr(self.cfg, "open_digsilent", 1))
            if pf is None:
                _ensure_powerfactory_on_path()
                import powerfactory as pf
            if DIgSILENTAgent._shared_app is None:
                try:
                    self.app = pf.GetApplicationExt()
                except Exception as ext_err:
                    self.app = pf.GetApplication()
                    if self.app is None:
                        raise RuntimeError(
                            f"GetApplicationExt() failed ({ext_err}); "
                            "GetApplication() also returned None"
                        ) from ext_err
                if self.app is None:
                    raise RuntimeError("GetApplicationExt() returned None")
                self._apply_show_preference(self.app, open_digsilent)
                DIgSILENTAgent._shared_app = self.app
                log.ok("PowerFactory application obtained and shown")
            else:
                self.app = DIgSILENTAgent._shared_app
                self._apply_show_preference(self.app, open_digsilent)
                log.ok("Reusing existing PowerFactory application in this process")
        except Exception as e:
            log.error(f"Cannot connect to PowerFactory: {e}")
            return False, str(e)

        try:
            if DIgSILENTAgent._shared_project_path != self.cfg.project_path:
                self.project = self.app.ActivateProject(self.cfg.project_path)
                if self.project is None:
                    raise RuntimeError(f"Project not found: {self.cfg.project_path}")
                DIgSILENTAgent._shared_project = self.project
                DIgSILENTAgent._shared_project_path = self.cfg.project_path
                log.ok(f"Project activated: {self.cfg.project_path}")
            else:
                self.project = DIgSILENTAgent._shared_project
                log.ok(f"Reusing already active project: {self.cfg.project_path}")
        except Exception as e:
            log.error(f"Cannot activate project: {e}")
            return False, str(e)

        return True, "Connected and project activated"

    # ──────────────────────────────────────────────────────────────
    # STEP 2 — Activate study case
    # ──────────────────────────────────────────────────────────────

    def activate_study_case(self) -> tuple[bool, str]:
        log.section("STEP 2 — Activate Study Case")
        try:
            folder = self.app.GetProjectFolder('study')
            target_name = self.cfg.study_case
            base_name = getattr(self.cfg, "base_study_case", "0. Base")

            if folder is not None:
                # Standard project: study cases folder exists
                target_case = self._find_study_case_exact(folder, target_name)
                if target_case is not None:
                    target_case.Activate()
                else:
                    base_case = self._find_study_case_exact(folder, base_name)
                    if base_case is None:
                        raise RuntimeError(
                            f"Base study case not found in study folder: '{base_name}'"
                        )
                    if target_name == base_name:
                        base_case.Activate()
                    else:
                        new_study_case = folder.AddCopy(base_case, target_name)
                        if new_study_case is None:
                            target_case = self._find_study_case_exact(folder, target_name)
                            if target_case is None:
                                raise RuntimeError(
                                    f"Study case copy failed: '{target_name}'"
                                )
                            new_study_case = target_case
                        new_study_case.Activate()
                        log.ok(f"Study case copied from '{base_name}' to '{target_name}'")
            else:
                # Non-standard project: search the whole project for *.IntCase by name
                log.warn("GetProjectFolder('study') returned None — searching project for IntCase objects")
                case_name = target_name.split('\\')[-1]
                target_case = self._find_study_case_in_project(case_name)
                if target_case is not None:
                    target_case.Activate()
                else:
                    base_case = self._find_study_case_in_project(base_name)
                    if base_case is not None:
                        if case_name == base_name:
                            base_case.Activate()
                        else:
                            parent = base_case.GetParent()
                            if parent is None:
                                raise RuntimeError(
                                    f"Cannot copy study case '{base_name}': parent folder not found"
                                )
                            new_study_case = parent.AddCopy(base_case, case_name)
                            if new_study_case is None:
                                new_study_case = self._find_study_case_in_project(case_name)
                                if new_study_case is None:
                                    raise RuntimeError(
                                        f"Study case copy failed: '{case_name}'"
                                    )
                            new_study_case.Activate()
                            log.ok(f"Study case copied from '{base_name}' to '{case_name}'")
                    else:
                        active = self.app.GetActiveStudyCase()
                        if active is None:
                            raise RuntimeError(
                                f"Study case '{case_name}' not found and no active study case is available. "
                                "Activate a study case in PowerFactory or check the names in the config."
                            )
                        active_name = getattr(active, "loc_name", "active")
                        log.warn(
                            f"Study case '{case_name}' not found; "
                            f"continuing with active study case '{active_name}'"
                        )
                        return True, f"Using active study case: {active_name}"

            log.ok(f"Study case activated: {target_name}")
            return True, "Study case activated"
        except Exception as e:
            log.error(f"Study case activation failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # STEP 4 — Run load flow
    # ──────────────────────────────────────────────────────────────

    def run_loadflow(self) -> tuple[bool, str]:
        log.section("STEP 4 — Load Flow (ComLdf)")
        try:
            ldf = self.app.GetFromStudyCase('ComLdf')
            if ldf is None:
                raise RuntimeError("ComLdf not found in study case")
            err = ldf.Execute()
            if err:
                raise RuntimeError(f"ComLdf returned error code {err}")
            log.ok("Load flow converged successfully")
            return True, "Load flow OK"
        except Exception as e:
            log.error(f"Load flow failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # STEP 5 — Configure & run RMS simulation
    # ──────────────────────────────────────────────────────────────

    def run_rms_simulation(self) -> tuple[bool, str]:
        log.section("STEP 5 — RMS Simulation (ComInc + ComSim)")
        try:
            # -- Build fault events BEFORE initialisation -------------
            log.info(f"Applying fault at t={self.cfg.t_fault}s, clearing at t={self.cfg.t_clear}s")
            self._apply_fault_event()

            # -- Register monitored variables in the result object ----
            reg_summary = self._register_result_variables()

            # -- Initialise simulation --------------------------------
            inc = self.app.GetFromStudyCase('ComInc')
            inc.iopt_sim   = 'rms'
            inc.iopt_show  = 0
            inc.iopt_adapt = 0
            inc.dtgrd      = self.cfg.dt_rms
            inc.start      = self.cfg.t_start
            self.app.EchoOff()
            err = inc.Execute()
            self.app.EchoOn()
            if err:
                raise RuntimeError(f"ComInc (initialisation) returned error code {err}")
            log.ok(f"Simulation initialised | dt={self.cfg.dt_rms}s")

            # -- Run simulation ---------------------------------------
            sim = self.app.GetFromStudyCase('ComSim')
            sim.tstop = self.cfg.t_end
            err = sim.Execute()
            if err:
                raise RuntimeError(f"ComSim returned error code {err}")
            log.ok(f"RMS simulation completed | t_end={self.cfg.t_end}s")
            return True, f"RMS simulation OK | signals: {reg_summary}"

        except Exception as e:
            log.error(f"RMS simulation failed: {e}")
            return False, str(e)

    def _register_result_variables(self) -> str:
        """Register the configured monitored signals on the result object.

        Without this step the RMS result object only records the time vector,
        which produces a CSV with a single column (and unusable plots).

        Returns a short human-readable summary of what was registered.
        """
        try:
            res = self.app.GetFromStudyCase(self.cfg.result_name)
            if res is None:
                msg = f"result object '{self.cfg.result_name}' not found"
                log.warn(f"{msg} — skipping variable registration")
                return msg

            registered = 0
            failed = []
            missing = []
            for entry in getattr(self.cfg, "signals", []) or []:
                try:
                    obj_name, var_name = entry[0], entry[1]
                except (TypeError, IndexError):
                    continue

                elements = self.app.GetCalcRelevantObjects(obj_name) or []
                if not elements:
                    missing.append(obj_name)
                    continue

                try:
                    res.AddVariable(elements[0], var_name)
                    registered += 1
                except Exception as add_err:
                    failed.append(f"{obj_name}:{var_name} ({add_err})")

            if registered:
                log.ok(f"Registered {registered} monitored variable(s) on '{self.cfg.result_name}'")
            if missing:
                log.warn(f"Signals not found and skipped: {', '.join(sorted(set(missing)))}")
            if failed:
                log.warn(f"AddVariable failed for: {', '.join(failed)}")
            if not registered:
                log.warn("No monitored variables registered — CSV will contain only the time column")

            summary = f"{registered} registered"
            if missing:
                summary += f", {len(missing)} missing"
            if failed:
                summary += f", {len(failed)} failed"
            return summary
        except Exception as e:
            log.warn(f"Could not register result variables: {e}")
            return f"registration error: {e}"

    def _apply_fault_event(self):
        """
        Clear all existing events, then create fault ON + clearance events.

        fault_type = "bus"  : EvtShc ON → EvtShc OFF (removes short-circuit)
        fault_type = "line" : EvtShc ON → EvtSwitch OPEN (trips the line)
        fault_type = "gen_switch" : EvtSwitch on selected generator
        """
        try:
            evt_folder = self.app.GetFromStudyCase('Simulation Events/Fault.IntEvt')
            if evt_folder is None:
                raise RuntimeError("Event folder not found: Simulation Events/Fault.IntEvt")

            # -- Clear existing events --------------------------------
            for obj in evt_folder.GetContents():
                obj.Delete()
            log.info("Existing simulation events cleared")

            raw_fault_type = str(getattr(self.cfg, "fault_type", "bus") or "bus")
            fault_type = raw_fault_type.strip().lower().replace("-", "_").replace(" ", "_")
            if fault_type in ("generator", "switch", "generator_switch"):
                fault_type = "gen_switch"

            if fault_type == "gen_switch":
                switch_element = (
                    getattr(self.cfg, "switch_element", "")
                    or getattr(self.cfg, "fault_element", "")
                )
                switch_time = float(
                    getattr(self.cfg, "t_switch", getattr(self.cfg, "switch_time", getattr(self.cfg, "t_fault", 1.0)))
                )

                raw_switch_state = getattr(self.cfg, "switch_state", getattr(self.cfg, "open_close", 0))
                if isinstance(raw_switch_state, str):
                    s = raw_switch_state.strip().lower()
                    if s in ("open", "trip", "off"):
                        switch_state = 0
                    elif s in ("close", "on"):
                        switch_state = 1
                    else:
                        switch_state = int(raw_switch_state)
                else:
                    switch_state = int(raw_switch_state)

                matches = self.app.GetCalcRelevantObjects(switch_element)
                if (not matches) and switch_element and ("." not in switch_element):
                    matches = self.app.GetCalcRelevantObjects(f"{switch_element}.ElmSym")
                if not matches:
                    all_gens = self.app.GetCalcRelevantObjects("*.ElmSym")
                    matches = [g for g in all_gens if getattr(g, "loc_name", "") == switch_element]
                if not matches:
                    raise RuntimeError(f"Switch target not found: {switch_element}")
                target = matches[0]

                # If a dedicated switch object exists for this generator name, prefer it.
                switch_obj_matches = self.app.GetCalcRelevantObjects(f"{target.loc_name}.StaSwitch")
                if switch_obj_matches:
                    target = switch_obj_matches[0]

                self.addSwitchEvent(target, switch_time, switch_state)
                action = "OPEN" if switch_state == 0 else "CLOSE"
                target_name = getattr(target, "loc_name", switch_element)
                log.info(f"EvtSwitch {action} → {target_name} at t={switch_time}s")
                return

            if fault_type not in ("bus", "line"):
                raise RuntimeError(f"Unsupported fault_type '{raw_fault_type}'. Use bus, line, or gen_switch.")

            # -- Faulted element --------------------------------------
            target = self.app.GetCalcRelevantObjects(self.cfg.fault_element)[0]

            # -- Short-circuit ON (same for both types) ---------------
            sc_on          = evt_folder.CreateObject('EvtShc', target.loc_name)
            sc_on.p_target = target
            sc_on.time     = self.cfg.t_fault
            sc_on.i_shc    = 0   # 3-phase fault
            log.info(f"EvtShc ON  → {self.cfg.fault_element} at t={self.cfg.t_fault}s")

            # -- Clearance (depends on fault_type) --------------------
            if fault_type == "line":
                # Trip the line: open its switch at t_clear
                self.addSwitchEvent(target, self.cfg.t_clear, 0)
                log.info(f"EvtSwitch OPEN → {self.cfg.fault_element} at t={self.cfg.t_clear}s")
            else:
                # Bus fault: remove short-circuit at t_clear
                sc_off          = evt_folder.CreateObject('EvtShc', target.loc_name)
                sc_off.p_target = target
                sc_off.time     = self.cfg.t_clear
                sc_off.i_shc    = 4   # clear fault
                log.info(f"EvtShc OFF → {self.cfg.fault_element} at t={self.cfg.t_clear}s")

        except Exception as e:
            log.warn(f"Could not create fault events automatically: {e}")
            log.warn("Continuing simulation without explicit fault — check your IntEvt folder")

    def addSwitchEvent(self, obj, sec, open_close):
        faultFolder = self.app.GetFromStudyCase("Simulation Events/Fault.IntEvt")
        if faultFolder is None:
            raise RuntimeError("Event folder not found: Simulation Events/Fault.IntEvt")
        event = faultFolder.CreateObject("EvtSwitch", obj.loc_name)
        if event is None:
            raise RuntimeError(f"Could not create EvtSwitch for target '{obj.loc_name}'")
        event.p_target = obj
        event.time = sec
        event.i_switch = open_close
        return event

    # ──────────────────────────────────────────────────────────────
    # STEP 6 — Export results to CSV
    # ──────────────────────────────────────────────────────────────

    def export_results_to_csv(self) -> tuple[bool, str]:
        log.section("STEP 6 — Export Results to CSV")
        try:
            run_dir = self._ensure_run_output_dir()

            filename = os.path.join(
                run_dir,
                f"{self.cfg.run_label}_RMS.csv"
            )

            # -- Use ComRes (PowerFactory built-in CSV exporter) ------
            comRes = self.app.GetFromStudyCase("ComRes")
            comRes.pResult  = self.app.GetFromStudyCase(self.cfg.result_name)
            comRes.f_name   = filename
            comRes.iopt_sep = 0   # use custom separators below
            comRes.col_Sep  = ";" # column separator
            comRes.dec_Sep  = "." # decimal separator
            comRes.iopt_exp = 6   # export format: CSV with time column
            comRes.iopt_csel = 0  # all columns
            comRes.iopt_vars = 0  # all variables
            comRes.iopt_tsel = 0  # full time range
            comRes.iopt_rscl = 0  # no rescaling
            err = comRes.Execute()
            if err:
                raise RuntimeError(f"ComRes.Execute() returned error code {err}")

            log.ok(f"CSV saved → {filename}")
            return True, filename

        except Exception as e:
            log.error(f"CSV export failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # STEP 7 — Optional export active project to PFD
    # ──────────────────────────────────────────────────────────────

    def export_project_to_pfd(self) -> tuple[bool, str]:
        log.section("STEP 7 — Export Active Project to PFD")
        try:
            active_project = self.app.GetActiveProject()
            if active_project is None:
                raise RuntimeError("No active project found")

            run_dir = self._ensure_run_output_dir()
            safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.cfg.run_label)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            pfd_path = os.path.join(run_dir, f"{safe_label}_{timestamp}.pfd")

            pfd_export = self.app.GetFromStudyCase("ComPfdexport")
            if pfd_export is None:
                raise RuntimeError("ComPfdexport command not found in study case")

            pfd_export.SetAttribute("g_objects", [active_project])
            pfd_export.SetAttribute("g_file", pfd_path)
            err = pfd_export.Execute()
            if err:
                raise RuntimeError(f"ComPfdexport.Execute() returned error code {err}")

            log.ok(f"PFD exported → {pfd_path}")
            return True, pfd_path

        except Exception as e:
            log.error(f"PFD export failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # STEP 8 — Generate standard plots from CSV
    # ──────────────────────────────────────────────────────────────

    def generate_standard_plots(self, csv_path: str) -> tuple[bool, str]:
        log.section("STEP 8 — Generate Standard Plots")
        try:
            if not os.path.exists(csv_path):
                raise RuntimeError(f"CSV file not found: {csv_path}")

            import pandas as pd
            run_dir = self._ensure_run_output_dir()

            # Detect delimiter from the first line; PF exports are usually ';'.
            with open(csv_path, "r", encoding="utf-8", errors="replace") as fh:
                line_1 = fh.readline()
                line_2 = fh.readline()
            delimiter = ";" if line_1.count(";") >= line_1.count(",") else ","

            # PowerFactory often exports two header rows:
            #   row 1: object names (Bus 01, G 01, ...)
            #   row 2: variable labels (u1, Magnitude in p.u., Speed in p.u., ...)
            has_two_row_header = (
                bool(line_2)
                and "Time in s" in line_2
                and (
                    "Magnitude in p.u." in line_2
                    or "Speed in p.u." in line_2
                    or "rel.Angle" in line_2
                )
            )

            if has_two_row_header:
                df = pd.read_csv(csv_path, sep=delimiter, header=[0, 1], decimal=".")
                if df.empty or len(df.columns) <= 1:
                    raise RuntimeError(f"Could not parse CSV: {csv_path}")

                time_data = pd.to_numeric(df.iloc[:, 0], errors="coerce")
                voltage_series = []
                speed_series = []
                used_labels = set()

                def _unique_label(base: str) -> str:
                    label = base
                    idx = 2
                    while label in used_labels:
                        label = f"{base}_{idx}"
                        idx += 1
                    used_labels.add(label)
                    return label

                for i in range(1, len(df.columns)):
                    col = df.columns[i]
                    obj_name = str(col[0]).strip()
                    var_name = str(col[1]).strip().lower()
                    series = pd.to_numeric(df.iloc[:, i], errors="coerce")
                    if series.notna().sum() == 0:
                        continue

                    if "magnitude in p.u." in var_name:
                        voltage_series.append((_unique_label(obj_name), series))
                    elif "speed" in var_name:
                        speed_series.append((_unique_label(obj_name), series))

            else:
                df = pd.read_csv(csv_path, sep=delimiter, decimal=".")
                if df.empty or len(df.columns) <= 1:
                    raise RuntimeError(f"Could not parse CSV: {csv_path}")

                time_data = pd.to_numeric(df.iloc[:, 0], errors="coerce")
                voltage_series = []
                speed_series = []
                for col in df.columns[1:]:
                    col_text = str(col).lower()
                    series = pd.to_numeric(df[col], errors="coerce")
                    if series.notna().sum() == 0:
                        continue

                    if (
                        "magnitude in p.u." in col_text
                        or col_text.endswith("_pu")
                        or "voltage" in col_text
                    ):
                        voltage_series.append((str(col), series))
                    elif "speed" in col_text:
                        speed_series.append((str(col), series))

            # Generate voltage magnitude plot
            if voltage_series:
                fig, ax = plt.subplots(figsize=(12, 6))
                for label, series in voltage_series:
                    ax.plot(time_data, series, label=label, linewidth=1.5)
                ax.set_xlabel('Time (s)', fontsize=11)
                ax.set_ylabel('Voltage (pu)', fontsize=11)
                ax.set_title(f'Bus Voltages — {self.cfg.run_label}', fontsize=13, fontweight='bold')
                ax.grid(True, alpha=0.3)
                ax.legend(loc='best', fontsize=9)
                fig.tight_layout()
                voltage_plot = os.path.join(run_dir, f"{self.cfg.run_label}_voltages.png")
                fig.savefig(voltage_plot, dpi=150, bbox_inches='tight')
                plt.close(fig)
                log.ok(f"Voltage plot saved → {voltage_plot}")
            else:
                log.warn("No voltage columns found in CSV for plotting")

            # Generate generator speed plot if available
            if speed_series:
                fig, ax = plt.subplots(figsize=(12, 6))
                for label, series in speed_series:
                    ax.plot(time_data, series, label=label, linewidth=1.5)
                ax.set_xlabel('Time (s)', fontsize=11)
                ax.set_ylabel('Speed (p.u.)', fontsize=11)
                ax.set_title(f'Generator Speeds — {self.cfg.run_label}', fontsize=13, fontweight='bold')
                ax.grid(True, alpha=0.3)
                ax.legend(loc='best', fontsize=9)
                fig.tight_layout()
                speed_plot = os.path.join(run_dir, f"{self.cfg.run_label}_gen_speeds.png")
                fig.savefig(speed_plot, dpi=150, bbox_inches='tight')
                plt.close(fig)
                log.ok(f"Generator speed plot saved → {speed_plot}")
            else:
                log.warn("No generator speed columns found in CSV for plotting")

            if not voltage_series and not speed_series:
                raise RuntimeError("No voltage or generator speed columns could be identified in CSV")

            return True, "Standard plots generated successfully"

        except Exception as e:
            log.error(f"Standard plots generation failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # CLOSE — shut down PowerFactory
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def close(cls) -> None:
        """Exit PowerFactory and reset the shared application handle."""
        if cls._shared_app is not None:
            try:
                cls._shared_app.Exit()
                log.ok("PowerFactory closed")
            except Exception as e:
                log.warn(f"PowerFactory Exit() raised: {e}")
            finally:
                cls._shared_app = None
                cls._shared_project = None
                cls._shared_project_path = None

    # ──────────────────────────────────────────────────────────────
    # CREATE NEW PROJECT — fresh, empty project for a clean build
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def _create_new_project(cls, app, project_name: str):
        """Create and activate a fresh, empty PowerFactory project.

        Any pre-existing project of the same name is deleted first so the build
        is idempotent. A minimal study case is created and activated so that
        calculation commands (ComLdf, etc.) are available.
        """
        user = app.GetCurrentUser()
        if user is None:
            raise RuntimeError("GetCurrentUser() returned None")

        # Delete a same-named project for a clean rebuild.
        for old in user.GetContents(f"{project_name}.IntPrj") or []:
            try:
                old.Delete()
                log.info(f"Deleted existing project for clean rebuild: {project_name}")
            except Exception as e:
                log.warn(f"Could not delete existing project '{project_name}': {e}")

        prj = user.CreateObject("IntPrj", project_name)
        if prj is None:
            raise RuntimeError(f"Failed to create project '{project_name}'")
        prj.Activate()

        # A study case is required for calculation commands to exist.
        study_folder = app.GetProjectFolder("study")
        case_parent = study_folder if study_folder is not None else prj
        sc = case_parent.CreateObject("IntCase", "Study Case")
        if sc is not None:
            sc.Activate()

        cls._shared_project = prj
        cls._shared_project_path = None  # not activated via path
        log.ok(f"Created and activated new project: {project_name}")
        return prj

    @staticmethod
    def _collect_bus_voltages(app) -> dict:
        """Return {bus_name: voltage_pu} for all calc-relevant terminals."""
        out: dict = {}
        try:
            terminals = app.GetCalcRelevantObjects("*.ElmTerm") or []
        except Exception:
            return out
        for term in terminals:
            try:
                name = term.loc_name
                v = term.GetAttribute("m:u")  # voltage magnitude in p.u.
                out[name] = round(float(v), 4)
            except Exception:
                continue
        return out

    @staticmethod
    def _diagnose_loadflow(app) -> list:
        """Inspect the active network for common load-flow abort causes.

        Returns a list of human-readable problem strings (empty if none found).
        Checks: a reference/slack machine exists, line rated voltage matches
        both terminal nominal voltages, and transformer winding voltages match
        their HV/LV terminal nominal voltages. These are the conditions that
        make ComLdf refuse to run ("connected between different voltage levels",
        "nominal voltage differs", "no reference machine").
        """
        problems: list = []

        def _term_kv(cubicle):
            try:
                term = cubicle.GetAttribute("cterm")
                return float(term.GetAttribute("uknom"))
            except Exception:
                return None

        def _close(a, b, tol=0.01):
            return a is not None and b is not None and abs(a - b) <= tol * max(abs(b), 1.0)

        try:
            syms = app.GetCalcRelevantObjects("*.ElmSym") or []
            xnets = app.GetCalcRelevantObjects("*.ElmXnet") or []
            n_ref = 0
            for s in syms:
                try:
                    if int(s.GetAttribute("ip_ctrl")) == 1:
                        n_ref += 1
                except Exception:
                    pass
            for x in xnets:
                try:
                    if int(x.GetAttribute("bustp")) == 0:  # SL reference
                        n_ref += 1
                except Exception:
                    n_ref += 1
            if n_ref == 0:
                problems.append("No reference/slack machine found (set one generator's "
                                "ip_ctrl=1 or add an external grid).")
            elif n_ref > 1:
                problems.append(f"{n_ref} reference machines found (expected exactly 1).")
        except Exception:
            pass

        try:
            for ln in app.GetCalcRelevantObjects("*.ElmLne") or []:
                try:
                    typ = ln.GetAttribute("typ_id")
                    uline = float(typ.GetAttribute("uline")) if typ else None
                    k1 = _term_kv(ln.GetAttribute("bus1"))
                    k2 = _term_kv(ln.GetAttribute("bus2"))
                    if k1 is not None and k2 is not None and not _close(k1, k2):
                        problems.append(
                            f"line '{ln.loc_name}': terminals at different voltage "
                            f"levels ({k1} kV vs {k2} kV).")
                    elif uline is not None and k1 is not None and not _close(uline, k1):
                        problems.append(
                            f"line '{ln.loc_name}': rated voltage {uline} kV differs "
                            f"from terminal {k1} kV.")
                except Exception:
                    continue
        except Exception:
            pass

        try:
            for tr in app.GetCalcRelevantObjects("*.ElmTr2") or []:
                try:
                    typ = tr.GetAttribute("typ_id")
                    if not typ:
                        continue
                    uh = float(typ.GetAttribute("utrn_h"))
                    ul = float(typ.GetAttribute("utrn_l"))
                    kh = _term_kv(tr.GetAttribute("bushv"))
                    kl = _term_kv(tr.GetAttribute("buslv"))
                    if kh is not None and not _close(uh, kh):
                        problems.append(
                            f"transformer '{tr.loc_name}': HV winding {uh} kV differs "
                            f"from terminal {kh} kV.")
                    if kl is not None and not _close(ul, kl):
                        problems.append(
                            f"transformer '{tr.loc_name}': LV winding {ul} kV differs "
                            f"from terminal {kl} kV.")
                except Exception:
                    continue
        except Exception:
            pass

        # Electrical-island check: every terminal must be reachable from the
        # reference machine through in-service branches, otherwise ComLdf aborts.
        try:
            terms = app.GetCalcRelevantObjects("*.ElmTerm") or []
            tid = {id(t): i for i, t in enumerate(terms)}
            parent = list(range(len(terms)))

            def _find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            def _union(a, b):
                ra, rb = _find(a), _find(b)
                if ra != rb:
                    parent[ra] = rb

            def _idx(cubicle):
                try:
                    t = cubicle.GetAttribute("cterm")
                    return tid.get(id(t))
                except Exception:
                    return None

            n_branches = 0
            for cls_, ends in (("*.ElmLne", ("bus1", "bus2")),
                               ("*.ElmTr2", ("bushv", "buslv")),
                               ("*.ElmTr3", ("bushv", "busmv"))):
                for br in app.GetCalcRelevantObjects(cls_) or []:
                    try:
                        if int(br.GetAttribute("outserv")) == 1:
                            continue
                    except Exception:
                        pass
                    idxs = [_idx(br.GetAttribute(e)) for e in ends]
                    idxs = [i for i in idxs if i is not None]
                    for k in range(1, len(idxs)):
                        _union(idxs[0], idxs[k])
                        n_branches += 1

            if terms and n_branches:
                roots = {}
                for i in range(len(terms)):
                    roots.setdefault(_find(i), []).append(i)
                if len(roots) > 1:
                    sizes = sorted((len(v) for v in roots.values()), reverse=True)
                    problems.append(
                        f"network splits into {len(roots)} electrical islands "
                        f"(terminal counts {sizes}); every island needs a reference "
                        f"or they must be connected.")
        except Exception:
            pass

        # Anomalous element data that can make PowerFactory refuse the solve.
        try:
            for ln in app.GetCalcRelevantObjects("*.ElmLne") or []:
                try:
                    typ = ln.GetAttribute("typ_id")
                    dline = float(ln.GetAttribute("dline"))
                    r = float(typ.GetAttribute("rline")) * dline
                    x = float(typ.GetAttribute("xline")) * dline
                    if abs(r) + abs(x) < 1e-9:
                        problems.append(f"line '{ln.loc_name}': ~zero impedance "
                                        f"(R={r}, X={x} ohm).")
                except Exception:
                    continue
        except Exception:
            pass

        try:
            for g in app.GetCalcRelevantObjects("*.ElmSym") or []:
                try:
                    typ = g.GetAttribute("typ_id")
                    ugn = float(typ.GetAttribute("ugn")) if typ else None
                    sgn = float(typ.GetAttribute("sgn")) if typ else None
                    kb = _term_kv(g.GetAttribute("bus1"))
                    if ugn is not None and kb is not None and not _close(ugn, kb, 0.05):
                        problems.append(
                            f"generator '{g.loc_name}': rated voltage {ugn} kV differs "
                            f"from terminal {kb} kV.")
                    if sgn is not None and sgn <= 0:
                        problems.append(
                            f"generator '{g.loc_name}': non-positive rating {sgn} MVA.")
                except Exception:
                    continue
        except Exception:
            pass

        try:
            gp = sum(float(g.GetAttribute("pgini"))
                     for g in app.GetCalcRelevantObjects("*.ElmSym") or [])
            lp = sum(float(l.GetAttribute("plini"))
                     for l in app.GetCalcRelevantObjects("*.ElmLod") or [])
            problems.append(f"[info] generation {gp:.1f} MW vs load {lp:.1f} MW "
                            f"(slack must cover {lp - gp:+.1f} MW + losses).")
        except Exception:
            pass

        return problems

    # ──────────────────────────────────────────────────────────────
    # IMPORT PROJECT — load a .pfd file into PowerFactory
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def import_project(cls, file_path: str, open_digsilent: bool = True) -> tuple[bool, str]:
        """
        Import a .pfd project file into PowerFactory and activate it.

        Parameters
        ----------
        file_path : str
            Absolute path to the .pfd export file.

        Returns
        -------
        (success, message)
        """
        global pf
        if pf is None:
            _ensure_powerfactory_on_path()
            import powerfactory as pf

        if not os.path.isfile(file_path):
            return False, f"File not found: {file_path}"

        try:
            if cls._shared_app is None:
                app = pf.GetApplicationExt()
                if app is None:
                    raise RuntimeError("GetApplicationExt() returned None")
                cls._apply_show_preference(app, open_digsilent)
                cls._shared_app = app
            else:
                app = cls._shared_app
                cls._apply_show_preference(app, open_digsilent)

            Pfdimport = app.GetFromStudyCase("ComPfdimport")
            if Pfdimport is None:
                raise RuntimeError("ComPfdimport command not found in study case")

            Pfdimport.SetAttribute("g_file", file_path)
            Pfdimport.activatePrj = 1
            err = Pfdimport.Execute()
            if err:
                raise RuntimeError(f"ComPfdimport.Execute() returned error code {err}")

            # Reset shared project so the next connect() re-activates properly.
            cls._shared_project = None
            cls._shared_project_path = None

            log.ok(f"Project imported and activated from: {file_path}")
            return True, f"Project imported successfully from {file_path}"

        except Exception as e:
            log.error(f"Project import failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # BUILD NETWORK FROM SLD — parse a vector PDF and create the model
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def build_network_from_sld(
        cls,
        pdf_path: str,
        network_name: str = "SLD_Network",
        page_index: int = 0,
        overrides_path: str = "",
        open_digsilent: bool = True,
        project_name: str = "",
    ) -> tuple[bool, dict]:
        """Parse a single-line-diagram PDF and build the PowerFactory network.

        If ``project_name`` is given, a fresh empty project of that name is
        created (replacing any existing one) and the network is built inside it.
        Otherwise a project must already be active so the new grid has a home.
        The parsing stage does not touch PowerFactory; only the build stage uses
        the API. After the build a load flow (ComLdf) is run and its convergence
        is reported.

        Parameters
        ----------
        pdf_path : str
            Absolute path to the vector SLD PDF.
        network_name : str
            Name of the grid (ElmNet) to create/populate.
        page_index : int
            Page of the PDF to parse (default 0).
        overrides_path : str
            Optional path to an sld_overrides.json correction file.
        open_digsilent : bool
            If True (default), show the PowerFactory GUI window.
        project_name : str
            If non-empty, create a new empty project with this name instead of
            using the currently active project.

        Returns
        -------
        (success, report) where report contains the topology summary, the
        builder result (created counts and warnings), and the load-flow result.
        """
        global pf
        if pf is None:
            _ensure_powerfactory_on_path()
            import powerfactory as pf

        log.section("BUILD NETWORK FROM SLD")

        import time as _time
        timings: dict[str, float] = {}

        # -- Stage A: parse the PDF (no PowerFactory dependency) -------
        try:
            _t0 = _time.perf_counter()
            import importlib
            import sld_parser
            importlib.reload(sld_parser)  # pick up edits without MCP restart
            cfg = sld_parser.ParseConfig(page_index=page_index)
            topology = sld_parser.parse_sld(
                pdf_path, cfg, overrides_path or None
            )
            summary = topology.get("summary", {})
            timings["read_sld_s"] = round(_time.perf_counter() - _t0, 3)
            log.ok(f"Parsed SLD in {timings['read_sld_s']}s: {summary}")
        except Exception as e:
            log.error(f"SLD parsing failed: {e}")
            return False, {"stage": "parse", "error": str(e)}

        if not topology.get("buses"):
            msg = "No buses detected in the SLD — nothing to build."
            log.error(msg)
            return False, {"stage": "parse", "error": msg, "summary": summary}

        # -- Stage B: acquire app + ensure a project is active --------
        try:
            _t0 = _time.perf_counter()
            if cls._shared_app is None:
                app = pf.GetApplicationExt()
                if app is None:
                    raise RuntimeError("GetApplicationExt() returned None")
                cls._apply_show_preference(app, open_digsilent)
                cls._shared_app = app
            else:
                app = cls._shared_app
                cls._apply_show_preference(app, open_digsilent)

            if project_name:
                cls._create_new_project(app, project_name)
            elif app.GetActiveProject() is None:
                raise RuntimeError(
                    "No active PowerFactory project. Import or activate a "
                    "project before building a network (or pass project_name)."
                )
        except Exception as e:
            log.error(f"Cannot access PowerFactory application/project: {e}")
            return False, {"stage": "connect", "error": str(e), "summary": summary}

        # -- Stage C: build the network -------------------------------
        try:
            import importlib
            import pf_network_builder
            importlib.reload(pf_network_builder)  # pick up edits without MCP restart
            result = pf_network_builder.build_network(
                app, topology, network_name, log=log
            )
            timings["develop_network_s"] = round(_time.perf_counter() - _t0, 3)
            log.ok(f"Built network in {timings['develop_network_s']}s")
            report = {
                "stage": "build",
                "summary": summary,
                "grid": result.get("grid"),
                "created": result.get("created"),
                "warnings": result.get("warnings", []),
            }
        except Exception as e:
            log.error(f"Network build failed: {e}")
            return False, {"stage": "build", "error": str(e), "summary": summary}

        # -- Stage D: run a load flow to validate the model -----------
        try:
            _t0 = _time.perf_counter()
            ldf = app.GetFromStudyCase("ComLdf")
            if ldf is None:
                report["loadflow"] = {"converged": False,
                                      "message": "ComLdf not found in study case"}
                report["timings"] = timings
                log.warn("ComLdf not found; skipping load-flow validation")
                return True, report

            err = ldf.Execute()
            converged = (err == 0)
            voltages = cls._collect_bus_voltages(app) if converged else {}
            timings["load_flow_s"] = round(_time.perf_counter() - _t0, 3)
            report["loadflow"] = {
                "converged": converged,
                "error_code": err,
                "bus_voltages_pu": voltages,
            }
            if not converged:
                diagnostics = cls._diagnose_loadflow(app)
                report["loadflow"]["diagnostics"] = diagnostics
                for d in diagnostics:
                    log.warn(f"  diagnostic: {d}")
            report["timings"] = timings
            if converged:
                log.ok(f"Load flow converged in {timings['load_flow_s']}s. "
                       f"{len(voltages)} bus voltages collected.")
            else:
                log.warn(f"Load flow did NOT converge (ComLdf error code {err}).")
            return True, report
        except Exception as e:
            log.error(f"Load flow step failed: {e}")
            report["loadflow"] = {"converged": False, "message": str(e)}
            report["timings"] = timings
            return True, report

    # ──────────────────────────────────────────────────────────────
    # BUILD IEEE 14-BUS — hardcoded standard test-case dataset
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def build_ieee14bus(
        cls,
        network_name: str = "IEEE_14_Bus",
        project_name: str = "IEEE_14_Bus_Test",
        open_digsilent: bool = True,
    ) -> tuple[bool, dict]:
        """Build the IEEE 14-bus standard test case in a new PowerFactory project.

        All data is hardcoded from the standard dataset (100 MVA, 60 Hz base).
        No external PDF is required. A fresh project is created (replacing any
        existing one), the network is built and commissioned, a load flow is run,
        and the result is returned.
        """
        import importlib
        import sld_parser
        importlib.reload(sld_parser)

        # ── Hardcoded IEEE 14-bus topology ────────────────────────
        base_mva = 100.0

        bus_data = [
            # (bus_no, kv)
            (1, 69.0), (2, 69.0), (3, 69.0), (4, 69.0), (5, 69.0),
            (6, 13.8), (7, 13.8), (8, 18.0), (9, 13.8), (10, 13.8),
            (11, 13.8), (12, 13.8), (13, 13.8), (14, 13.8),
        ]

        # (from, to, R_pu, X_pu, length_km)
        line_data = [
            (1,  2,  0.01938, 0.05917, 22.5),
            (1,  5,  0.05403, 0.22304, 84.9),
            (2,  3,  0.04699, 0.19797, 75.4),
            (2,  4,  0.05811, 0.17632, 67.0),
            (2,  5,  0.05695, 0.17388, 66.3),
            (3,  4,  0.06701, 0.17103, 65.1),
            (4,  5,  0.01335, 0.04211, 16.0),
            (6,  11, 0.09498, 0.19890, 75.8),
            (6,  12, 0.12291, 0.25581, 97.5),
            (6,  13, 0.06615, 0.13027, 49.5),
            (7,  9,  0.00000, 0.11001, 41.9),
            (9,  10, 0.03181, 0.08450, 32.2),
            (9,  14, 0.12711, 0.27038, 103.0),
            (10, 11, 0.08205, 0.19207, 73.1),
            (12, 13, 0.22092, 0.19988, 76.2),
            (13, 14, 0.17093, 0.34802, 133.0),
        ]

        # (from, to, kv_hv, kv_lv, X_pu, tap, s_mva)
        trafo_data = [
            (4, 7,  69.0, 13.8, 0.20912, 0.978, 55.0),
            (4, 9,  69.0, 13.8, 0.55618, 0.969, 32.0),
            (5, 6,  69.0, 13.8, 0.25202, 0.932, 45.0),
            (7, 8,  13.8, 18.0, 0.17615, 1.304, 32.0),
        ]

        # (bus_no, s_mva, v0_pu, bus_type, p_mw)
        gen_data = [
            (1,  615.0, 1.060, "slack",     0.0),
            (2,   60.0, 1.045, "PV",       21.7),
            (3,   60.0, 1.010, "sync_cond", 0.0),
            (6,   25.0, 1.070, "sync_cond", 0.0),
            (8,   25.0, 1.090, "sync_cond", 0.0),
        ]

        # (bus_no, p_mw, q_mvar)
        load_data = [
            (2,  21.7, 12.7), (3,  94.2, 19.0), (4,  47.8, -3.9),
            (5,   7.6,  1.6), (6,  11.2,  7.5), (9,  29.5, 16.6),
            (10,  9.0,  5.8), (11,  3.5,  1.8), (12,  6.1,  1.6),
            (13, 13.5,  5.8), (14, 14.9,  5.0),
        ]

        shunt_data = [(9, 19.0)]  # (bus_no, q_mvar capacitive)

        # ── Convert to sld_parser dataclasses ────────────────────
        def _bn(n):
            return f"Bus_{n}"

        bus_kv = {n: kv for n, kv in bus_data}

        buses = [sld_parser.Bus(loc_name=_bn(n), voltage_kv=kv, cx=0.0, cy=0.0)
                 for n, kv in bus_data]

        lines = []
        for a, b, r_pu, x_pu, length in line_data:
            z_base = (bus_kv[a] ** 2) / base_mva
            length = length or 1.0
            r_km = r_pu * z_base / length
            x_km = x_pu * z_base / length
            lines.append(sld_parser.Line(
                loc_name=f"L{a}_{b}", bus1=_bn(a), bus2=_bn(b),
                r_ohm=r_km, x_ohm=x_km, length_km=length,
            ))

        transformers = []
        for a, b, kv_hv, kv_lv, x_pu, tap, s_mva in trafo_data:
            hv, lv = (a, b) if kv_hv >= kv_lv else (b, a)
            transformers.append(sld_parser.Transformer(
                loc_name=f"T{a}_{b}", bus_hv=_bn(hv), bus_lv=_bn(lv),
                s_mva=s_mva, x_pu=x_pu, tap_ratio=tap,
                kv_hv=kv_hv, kv_lv=kv_lv,
            ))

        generators = [
            sld_parser.Generator(
                loc_name=f"G_{n}", bus=_bn(n),
                p_mw=p_mw, s_mva=s_mva, v0_pu=v0, bus_type=bus_type,
            )
            for n, s_mva, v0, bus_type, p_mw in gen_data
        ]

        loads = [
            sld_parser.Load(loc_name=f"Load_{n}", bus=_bn(n), p_mw=p, q_mvar=q)
            for n, p, q in load_data
        ]

        shunts = [
            sld_parser.Shunt(loc_name=f"Shunt_{n}", bus=_bn(n), q_mvar=q)
            for n, q in shunt_data
        ]

        topology = {
            "buses": buses, "lines": lines, "transformers": transformers,
            "generators": generators, "loads": loads, "shunts": shunts,
            "summary": {
                "buses": len(buses), "lines": len(lines),
                "transformers": len(transformers), "generators": len(generators),
                "loads": len(loads), "shunts": len(shunts),
                "source": "hardcoded",
            },
        }

        global pf
        if pf is None:
            _ensure_powerfactory_on_path()
            import powerfactory as pf

        log.section("BUILD IEEE 14-BUS (HARDCODED DATASET)")
        log.ok(f"Topology: {topology['summary']}")

        # ── Acquire app + create fresh project ────────────────────
        try:
            if cls._shared_app is None:
                app = pf.GetApplicationExt()
                if app is None:
                    raise RuntimeError("GetApplicationExt() returned None")
                cls._apply_show_preference(app, open_digsilent)
                cls._shared_app = app
            else:
                app = cls._shared_app
                cls._apply_show_preference(app, open_digsilent)

            cls._create_new_project(app, project_name)
        except Exception as e:
            log.error(f"Project creation failed: {e}")
            return False, {"stage": "connect", "error": str(e),
                           "summary": topology["summary"]}

        # ── Build network ─────────────────────────────────────────
        try:
            import pf_network_builder
            importlib.reload(pf_network_builder)
            result = pf_network_builder.build_network(
                app, topology, network_name, log=log,
            )
            report = {
                "stage": "build",
                "summary": topology["summary"],
                "grid": result.get("grid"),
                "created": result.get("created"),
                "warnings": result.get("warnings", []),
            }
        except Exception as e:
            log.error(f"Network build failed: {e}")
            return False, {"stage": "build", "error": str(e),
                           "summary": topology["summary"]}

        # ── Load flow ─────────────────────────────────────────────
        try:
            ldf = app.GetFromStudyCase("ComLdf")
            if ldf is None:
                report["loadflow"] = {"converged": False,
                                      "message": "ComLdf not found"}
                return True, report
            err = ldf.Execute()
            converged = (err == 0)
            voltages = cls._collect_bus_voltages(app) if converged else {}
            report["loadflow"] = {
                "converged": converged,
                "error_code": err,
                "bus_voltages_pu": voltages,
            }
            if converged:
                log.ok(f"Load flow converged. {len(voltages)} buses.")
            else:
                log.warn(f"Load flow did NOT converge (error {err}).")
            return True, report
        except Exception as e:
            log.error(f"Load flow failed: {e}")
            report["loadflow"] = {"converged": False, "message": str(e)}
            return True, report

    # ──────────────────────────────────────────────────────────────
    # DRAW DIAGRAM — auto-arrange the SLD with ComSgllayout
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def draw_diagram(
        cls,
        network_name: str = "IEEE_14_Bus",
        open_digsilent: bool = True,
    ) -> tuple[bool, str]:
        """Run ComSgllayout to auto-arrange the SLD for a named grid.

        The grid must already exist in the active project (built via
        build_network_from_sld). ComSgllayout creates graphic objects for
        every network element and positions them using PF's auto-layout engine.
        """
        global pf
        if pf is None:
            _ensure_powerfactory_on_path()
            import powerfactory as pf

        log.section("DRAW SLD DIAGRAM")

        import time as _time
        _t_draw0 = _time.perf_counter()

        try:
            if cls._shared_app is None:
                app = pf.GetApplicationExt()
                if app is None:
                    raise RuntimeError("GetApplicationExt() returned None")
                cls._apply_show_preference(app, open_digsilent)
                cls._shared_app = app
            else:
                app = cls._shared_app
                cls._apply_show_preference(app, open_digsilent)

            if app.GetActiveProject() is None:
                raise RuntimeError("No active project. Build the network first.")

            # Locate the target grid.
            netdat = app.GetProjectFolder("netdat")
            if netdat is None:
                raise RuntimeError("netdat folder not found")
            grids = netdat.GetContents(f"{network_name}.ElmNet") or []
            if not grids:
                raise RuntimeError(f"Grid '{network_name}' not found in netdat")
            grid = grids[0]

            # Activate the grid so PF knows which elements are relevant.
            try:
                grid.Activate()
            except Exception as e:
                log.warn(f"grid.Activate() failed (non-fatal): {e}")

            sc = app.GetActiveStudyCase()
            if sc is None:
                raise RuntimeError("No active study case")

            # Unfreeze the graphics board so the layout tool can add objects.
            desktop = None
            try:
                desktop = app.GetFromStudyCase("SetDesktop")
                if desktop is not None:
                    try:
                        desktop.Show()
                    except Exception:
                        pass
                    desktop.Unfreeze()
                    log.info("Unfroze graphics desktop")
            except Exception as e:
                log.warn(f"Could not unfreeze desktop (non-fatal): {e}")

            # Remove any pre-existing network diagram for this grid so the
            # Diagram Layout Tool generates a fresh one (avoids duplicates on
            # re-run). The tool stores diagrams under Network Model\Diagrams.
            prj = app.GetActiveProject()
            try:
                for old in (prj.GetContents(f"{network_name}.IntGrfnet", 1)
                            or []):
                    old.Delete()
            except Exception as ex:
                log.warn(f"Could not clear old IntGrfnet (non-fatal): {ex}")

            # Run the Diagram Layout Tool. iAction=0 / iGenType=0 generates a
            # complete new diagram and auto-arranges graphic symbols for the
            # selected grid (pGrids). The tool creates the IntGrfnet itself.
            #
            # Use a FRESH command object: a persisted ComSgllayout retains a
            # 'neighborStartElems' selection from previous runs that may point
            # to deleted elements (dangling reference) and break Execute().
            try:
                for old in (sc.GetContents("*.ComSgllayout") or []):
                    old.Delete()
                for old in (sc.GetContents("*.SetSelect") or []):
                    old.Delete()
            except Exception as ex:
                log.warn(f"Could not clear stale layout commands: {ex}")

            layout = sc.CreateObject("ComSgllayout", "SLD Layout")
            if layout is None:
                layout = app.GetFromStudyCase("ComSgllayout")
            if layout is None:
                raise RuntimeError("Could not obtain ComSgllayout command")

            for attr, val in (("iAction", 0), ("iGenType", 0)):
                try:
                    layout.SetAttribute(attr, val)
                except Exception as ex:
                    log.warn(f"ComSgllayout: could not set {attr}={val}: {ex}")
            # Select the target grid. Without pGrids the tool aborts with
            # "Diagram could not be generated, because no grid has been selected".
            try:
                layout.SetAttribute("pGrids", [grid])
            except Exception:
                try:
                    layout.SetAttribute("pGrids", grid)
                except Exception as ex:
                    log.warn(f"ComSgllayout: could not set pGrids: {ex}")
            log.info("ComSgllayout configured: iAction=0, iGenType=0, pGrids=grid")

            err = layout.Execute()
            draw_s = round(_time.perf_counter() - _t_draw0, 3)

            # The tool creates the IntGrfnet under Network Model\Diagrams during
            # Execute. Count the placed graphic symbols (IntGrf) to confirm.
            symbols = 0
            try:
                gnets = prj.GetContents(f"{network_name}.IntGrfnet", 1) or []
                for gn in gnets:
                    symbols += len(gn.GetContents("*.IntGrf", 1) or [])
            except Exception:
                pass

            if err == 0 and symbols > 0:
                log.ok(f"ComSgllayout completed for grid '{network_name}' "
                       f"in {draw_s}s ({symbols} symbols)")
                return True, (f"SLD diagram drawn for grid '{network_name}' "
                              f"({symbols} symbols, draw_diagram_s={draw_s})")

            return False, (
                f"ComSgllayout finished (err={err}) but produced {symbols} "
                f"symbols for grid '{network_name}'.")

        except Exception as e:
            log.error(f"draw_diagram failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # MODIFY OBJECT PARAMETER — set a PowerFactory attribute by name
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def modify_parameter(
        cls,
        object_name: str,
        variable: str,
        new_value,
        open_digsilent: bool = True,
    ) -> tuple[bool, str]:
        """
        Modify one attribute on all PowerFactory objects matching object_name.

        Parameters
        ----------
        object_name : str
            PowerFactory object query passed to GetCalcRelevantObjects
            (example: "G 10.ElmSym").
        variable : str
            Attribute name to modify (example: "e:outserv").
        new_value : Any
            New value written through SetAttribute.

        Returns
        -------
        (success, message)
        """
        global pf
        if pf is None:
            _ensure_powerfactory_on_path()
            import powerfactory as pf

        try:
            if cls._shared_app is None:
                app = pf.GetApplicationExt()
                if app is None:
                    raise RuntimeError("GetApplicationExt() returned None")
                cls._apply_show_preference(app, open_digsilent)
                cls._shared_app = app
            else:
                app = cls._shared_app
                cls._apply_show_preference(app, open_digsilent)

            objects = app.GetCalcRelevantObjects(object_name)
            if not objects:
                raise RuntimeError(f"No objects found for query: {object_name}")

            def _coerce_value(current_value, incoming_value):
                if incoming_value is None:
                    return None

                # Preserve non-string values that are already typed.
                if not isinstance(incoming_value, str):
                    if isinstance(current_value, bool):
                        return bool(incoming_value)
                    if isinstance(current_value, int) and not isinstance(current_value, bool):
                        return int(incoming_value)
                    if isinstance(current_value, float):
                        return float(incoming_value)
                    return incoming_value

                raw = incoming_value.strip()

                # Coerce by current attribute type when available.
                if isinstance(current_value, bool):
                    token = raw.lower()
                    if token in ("1", "true", "yes", "on"):
                        return True
                    if token in ("0", "false", "no", "off"):
                        return False
                    raise ValueError(f"Cannot cast '{incoming_value}' to bool")

                if isinstance(current_value, int) and not isinstance(current_value, bool):
                    return int(float(raw))

                if isinstance(current_value, float):
                    return float(raw)

                # Fallback inference when current value is string/None/unknown.
                token = raw.lower()
                if token in ("true", "false"):
                    return token == "true"
                try:
                    if "." not in raw and "e" not in token:
                        return int(raw)
                    return float(raw)
                except ValueError:
                    return incoming_value

            for obj in objects:
                current_value = obj.GetAttribute(variable)
                typed_value = _coerce_value(current_value, new_value)
                obj.SetAttribute(variable, typed_value)

            log.ok(
                f"Updated '{variable}' to '{new_value}' for {len(objects)} object(s) matching '{object_name}'"
            )
            return (
                True,
                f"Updated {len(objects)} object(s): {object_name} | {variable}={new_value}",
            )

        except Exception as e:
            log.error(f"Parameter update failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # LOAD FLOW — run ComLdf on the currently active study case
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def _export_loadflow_snapshot_to_csv(
        cls,
        app,
        output_dir: str,
        run_label: str,
    ) -> tuple[bool, str]:
        """Export a point-in-time load-flow snapshot to CSV."""
        try:
            safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_label or "run")
            base_dir = output_dir or r"C:\RMS_Results"
            run_dir = os.path.join(base_dir, safe_label)
            os.makedirs(run_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = os.path.join(run_dir, f"{safe_label}_loadflow_{timestamp}.csv")

            fieldnames = [
                "element_type",
                "e:loc_name",
                "m:u",
                "m:phiu",
                "m:P:bus1",
                "m:Q:bus1",
                "e:plini",
                "e:qlini",
                "c:loading",
                "n:Pflow:bus1",
            ]

            def _safe_get_attr(obj, attr_name: str):
                try:
                    return obj.GetAttribute(attr_name)
                except Exception:
                    return None

            def _name_of(obj) -> str:
                return str(getattr(obj, "loc_name", str(obj)))

            def _scalar(value):
                if value is None:
                    return ""
                if isinstance(value, (str, int, float, bool)):
                    return value
                return str(value)

            rows = []

            buses = app.GetCalcRelevantObjects("*.ElmTerm") or []
            for obj in sorted(buses, key=_name_of):
                rows.append({
                    "element_type": "ElmTerm",
                    "e:loc_name": _name_of(obj),
                    "m:u": _scalar(_safe_get_attr(obj, "m:u")),
                    "m:phiu": _scalar(_safe_get_attr(obj, "m:phiu")),
                    "m:P:bus1": "",
                    "m:Q:bus1": "",
                    "e:plini": "",
                    "e:qlini": "",
                    "c:loading": "",
                    "n:Pflow:bus1": "",
                })

            generators = app.GetCalcRelevantObjects("*.ElmSym") or []
            for obj in sorted(generators, key=_name_of):
                rows.append({
                    "element_type": "ElmSym",
                    "e:loc_name": _name_of(obj),
                    "m:u": "",
                    "m:phiu": "",
                    "m:P:bus1": _scalar(_safe_get_attr(obj, "m:P:bus1")),
                    "m:Q:bus1": _scalar(_safe_get_attr(obj, "m:Q:bus1")),
                    "e:plini": "",
                    "e:qlini": "",
                    "c:loading": "",
                    "n:Pflow:bus1": "",
                })

            loads = app.GetCalcRelevantObjects("*.ElmLod") or []
            for obj in sorted(loads, key=_name_of):
                rows.append({
                    "element_type": "ElmLod",
                    "e:loc_name": _name_of(obj),
                    "m:u": "",
                    "m:phiu": "",
                    "m:P:bus1": "",
                    "m:Q:bus1": "",
                    "e:plini": _scalar(_safe_get_attr(obj, "e:plini")),
                    "e:qlini": _scalar(_safe_get_attr(obj, "e:qlini")),
                    "c:loading": "",
                    "n:Pflow:bus1": "",
                })

            lines = app.GetCalcRelevantObjects("*.ElmLne") or []
            for obj in sorted(lines, key=_name_of):
                rows.append({
                    "element_type": "ElmLne",
                    "e:loc_name": _name_of(obj),
                    "m:u": "",
                    "m:phiu": "",
                    "m:P:bus1": "",
                    "m:Q:bus1": "",
                    "e:plini": "",
                    "e:qlini": "",
                    "c:loading": _scalar(_safe_get_attr(obj, "c:loading")),
                    "n:Pflow:bus1": _scalar(_safe_get_attr(obj, "n:Pflow:bus1")),
                })

            with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            return True, csv_path
        except Exception as e:
            return False, str(e)

    @classmethod
    def load_flow(
        cls,
        open_digsilent: bool = True,
        save_csv: bool = False,
        output_dir: str = r"C:\RMS_Results",
        run_label: str = "run_001",
    ) -> tuple[bool, str]:
        """Run a load flow (ComLdf) on the currently active study case."""
        global pf
        if pf is None:
            _ensure_powerfactory_on_path()
            import powerfactory as pf
        try:
            if cls._shared_app is None:
                app = pf.GetApplicationExt()
                if app is None:
                    raise RuntimeError("GetApplicationExt() returned None")
                cls._apply_show_preference(app, open_digsilent)
                cls._shared_app = app
            else:
                app = cls._shared_app
                cls._apply_show_preference(app, open_digsilent)

            ldf = app.GetFromStudyCase('ComLdf')
            if ldf is None:
                raise RuntimeError("ComLdf not found in study case")
            err = ldf.Execute()
            if err:
                diagnostics = cls._diagnose_loadflow(app)
                if diagnostics:
                    detail = "; ".join(diagnostics)
                    raise RuntimeError(
                        f"ComLdf returned error code {err}. Likely causes: {detail}")
                raise RuntimeError(f"ComLdf returned error code {err}")

            if save_csv:
                ok_csv, csv_msg = cls._export_loadflow_snapshot_to_csv(app, output_dir, run_label)
                if not ok_csv:
                    raise RuntimeError(f"Load flow OK, but CSV export failed: {csv_msg}")
                log.ok(f"Load flow CSV saved → {csv_msg}")
                return True, f"Load flow OK | CSV saved to {csv_msg}"

            log.ok("Load flow converged successfully")
            return True, "Load flow OK"
        except Exception as e:
            log.error(f"Load flow failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # SHORT CIRCUIT — run ComShc on the currently active study case
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def short_circuit(cls, open_digsilent: bool = True) -> tuple[bool, str]:
        """Run a short-circuit calculation (ComShc) on the currently active study case."""
        global pf
        if pf is None:
            _ensure_powerfactory_on_path()
            import powerfactory as pf
        try:
            if cls._shared_app is None:
                app = pf.GetApplicationExt()
                if app is None:
                    raise RuntimeError("GetApplicationExt() returned None")
                cls._apply_show_preference(app, open_digsilent)
                cls._shared_app = app
            else:
                app = cls._shared_app
                cls._apply_show_preference(app, open_digsilent)

            shc = app.GetFromStudyCase('ComShc')
            if shc is None:
                raise RuntimeError("ComShc not found in study case")
            err = shc.Execute()
            if err:
                raise RuntimeError(f"ComShc returned error code {err}")
            log.ok("Short-circuit calculation completed")
            return True, "Short-circuit calculation OK"
        except Exception as e:
            log.error(f"Short-circuit calculation failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # CREATE STUDY CASE — create/activate case without simulation
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def create_study_case(
        cls,
        project_path: str,
        case_name: str,
        base_study_case: str = "0. Base",
        open_digsilent: bool = True,
        request_id: str = "",
    ) -> tuple[bool, str]:
        """
        Create and activate a study case by name, without running simulations.

        If case_name already exists, it is activated as-is.
        Otherwise, the case is copied from base_study_case.

        If request_id is provided and repeated, the cached result is returned
        without executing creation/activation logic again.
        """
        global pf
        if pf is None:
            _ensure_powerfactory_on_path()
            import powerfactory as pf

        try:
            case_name = (case_name or "").strip()
            base_study_case = (base_study_case or "").strip() or "0. Base"
            request_id = (request_id or "").strip()
            if not case_name:
                raise RuntimeError("case_name cannot be empty")

            def _done(ok: bool, msg: str) -> tuple[bool, str]:
                if request_id:
                    cls._create_case_request_cache[request_id] = (ok, msg, time.time())
                return ok, msg

            if request_id:
                cls._prune_create_case_request_cache()
                cached = cls._create_case_request_cache.get(request_id)
                if cached is not None:
                    ok, msg, _ = cached
                    replay_msg = f"[idempotent replay] {msg}"
                    log.warn(f"create_study_case replay ignored for request_id='{request_id}'")
                    return ok, replay_msg

            if cls._shared_app is None:
                app = pf.GetApplicationExt()
                if app is None:
                    raise RuntimeError("GetApplicationExt() returned None")
                cls._apply_show_preference(app, open_digsilent)
                cls._shared_app = app
            else:
                app = cls._shared_app
                cls._apply_show_preference(app, open_digsilent)

            if cls._shared_project_path != project_path:
                project = app.ActivateProject(project_path)
                if project is None:
                    raise RuntimeError(f"Project not found: {project_path}")
                cls._shared_project = project
                cls._shared_project_path = project_path

            folder = app.GetProjectFolder("study")
            if folder is None:
                raise RuntimeError("Study folder not found: GetProjectFolder('study') returned None")

            before_names = cls._list_study_case_names(folder)

            target_case = cls._find_study_case_exact(folder, case_name)
            if target_case is not None:
                target_case.Activate()
                return _done(True, f"Study case already existed and was activated: {case_name}")

            base_case = cls._find_study_case_exact(folder, base_study_case)
            if base_case is None:
                raise RuntimeError(f"Base study case not found: {base_study_case}")

            new_case = folder.AddCopy(base_case, case_name)
            if new_case is None:
                target_case = cls._find_study_case_exact(folder, case_name)
                if target_case is None:
                    raise RuntimeError(f"Study case copy failed: {case_name}")
                new_case = target_case

            after_names = cls._list_study_case_names(folder)
            created_names = sorted(after_names - before_names)
            if len(created_names) > 1:
                raise RuntimeError(
                    "Unexpected multiple study-case creations detected in one call: "
                    + ", ".join(created_names)
                )
            if case_name not in after_names:
                raise RuntimeError(f"Target case not found after creation: {case_name}")

            new_case.Activate()
            log.ok(f"Study case copied from '{base_study_case}' to '{case_name}'")
            return _done(True, f"Study case created and activated: {case_name}")

        except Exception as e:
            log.error(f"Create study case failed: {e}")
            return False, str(e)

    # ──────────────────────────────────────────────────────────────
    # PIPELINE — run all steps in sequence
    # ──────────────────────────────────────────────────────────────

    def run_pipeline(self) -> dict:
        """
        Execute the full pipeline and return a status report dict.
        Each step is guarded: a failure stops the pipeline early.

        Per-step wall-clock durations (seconds) are recorded under
        ``report["timings"]``, along with ``report["timings"]["total"]``.
        """
        report = {
            "connect":          None,
            "activate_case":    None,
            "load_flow":        None,
            "rms_simulation":   None,
            "csv_export":       None,
            "standard_plots":   None,
            "pfd_export":       None,
            "csv_path":         None,
            "pfd_path":         None,
            "timings":          {},
            "success":          False,
        }

        timings = report["timings"]
        pipeline_start = time.perf_counter()

        def _record(step_key: str, fn):
            """Run a step, record its duration, and return (ok, msg)."""
            step_start = time.perf_counter()
            ok, msg = fn()
            timings[step_key] = round(time.perf_counter() - step_start, 3)
            return ok, msg

        steps = [
            ("connect",        self.connect),
            ("activate_case",  self.activate_study_case),
            ("load_flow",      self.run_loadflow),
            ("rms_simulation", self.run_rms_simulation),
            ("csv_export",     self.export_results_to_csv),
        ]

        for key, fn in steps:
            ok, msg = _record(key, fn)
            report[key] = {"ok": ok, "msg": msg}
            if not ok:
                timings["total"] = round(time.perf_counter() - pipeline_start, 3)
                log.error(f"Pipeline stopped at step '{key}': {msg}")
                return report

        # -- Standard plots (always enabled by default) ----------------
        csv_path = report["csv_export"]["msg"]
        if report["csv_export"]["ok"] and csv_path:
            ok, msg = _record("standard_plots", lambda: self.generate_standard_plots(csv_path))
            report["standard_plots"] = {"ok": ok, "msg": msg}
            if not ok:
                log.warn(f"Standard plots generation failed, continuing anyway: {msg}")
        else:
            timings["standard_plots"] = 0.0
            report["standard_plots"] = {"ok": True, "msg": "Skipped (no CSV available)"}

        do_export_pfd = bool(getattr(self.cfg, "export_pfd", 0))
        if do_export_pfd:
            ok, msg = _record("pfd_export", self.export_project_to_pfd)
            report["pfd_export"] = {"ok": ok, "msg": msg}
            if not ok:
                timings["total"] = round(time.perf_counter() - pipeline_start, 3)
                log.error(f"Pipeline stopped at step 'pfd_export': {msg}")
                return report
            report["pfd_path"] = msg
        else:
            timings["pfd_export"] = 0.0
            report["pfd_export"] = {"ok": True, "msg": "Skipped (export_pfd=0)"}

        timings["total"] = round(time.perf_counter() - pipeline_start, 3)
        report["csv_path"] = report["csv_export"]["msg"]
        report["success"]  = True
        log.section("PIPELINE COMPLETE")
        log.ok(f"All steps passed in {timings['total']}s. Results → {report['csv_path']}")
        return report


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":

    # ── Load config from JSON (edit simulation_config.json, not this file)
    _cfg_path = os.path.join(os.path.dirname(__file__), "simulation_config.json")
    cfg = SimulationConfig.from_json(_cfg_path)

    sys.path.append(cfg.powerfactory_python_path)

    agent  = DIgSILENTAgent(cfg)
    report = agent.run_pipeline()

    # ── Print final summary ────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  PIPELINE REPORT")
    print("═" * 60)
    for step, result in report.items():
        if isinstance(result, dict):
            status = "✅" if result["ok"] else "❌"
            print(f"  {status}  {step:<20} {result['msg']}")
    print(f"\n  Overall success: {'✅ YES' if report['success'] else '❌ NO'}")
    if report["csv_path"]:
        print(f"  CSV output:      {report['csv_path']}")
    print("═" * 60)


"""Run all cases from simulation_config.json."""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault(
    "POWERFACTORY_PYTHON_PATH",
    r"C:\Program Files\DIgSILENT\PowerFactory 2024 SP5A\Python\3.12",
)
sys.path.insert(0, _HERE)

from Agent_DIgSILENT import SimulationConfig, DIgSILENTAgent  # noqa: E402

CFG_PATH = os.path.join(_HERE, "simulation_config.json")


def main() -> int:
    with open(CFG_PATH, encoding="utf-8") as f:
        raw = json.load(f)

    cases = raw.get("cases") or [raw]
    reports = []

    for i, case in enumerate(cases):
        cfg = SimulationConfig.from_json(CFG_PATH)
        for key, value in case.items():
            if key in cfg.__dataclass_fields__:
                setattr(cfg, key, value)
        cfg.run_label = case.get("case_name", cfg.run_label)

        print(f"\n=== Running case {i + 1}/{len(cases)}: {cfg.run_label} ===", flush=True)
        report = DIgSILENTAgent(cfg).run_pipeline()
        reports.append((cfg.run_label, report))
        print(f"Case {cfg.run_label} success: {report.get('success')}", flush=True)

    print("\n=== SUMMARY ===", flush=True)
    failed = 0
    for name, report in reports:
        ok = report.get("success")
        if not ok:
            failed += 1
        status = "OK" if ok else "FAILED"
        csv_path = report.get("csv_path") or "n/a"
        print(f"{name}: {status} -> {csv_path}", flush=True)
        for step, result in report.items():
            if isinstance(result, dict) and not result.get("ok"):
                print(f"  {step}: {result.get('msg')}", flush=True)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

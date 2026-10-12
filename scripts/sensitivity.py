"""How the gain depends on how expensive the post-source build steps are.

The change trace is the real one. The RUN step costs are swapped for other
profiles, because the demo's install step is deliberately small and fast and
a production image's is not. Everything here is model output, computed by
replaying the measured prefix cache rule over the real trace.

    python scripts/sensitivity.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from layerfloor.model import StepCost, optimal_order, replay
from layerfloor.policies import build_policies
from layerfloor.specs import py_service_spec
from layerfloor.trace import change_masks, commit_trace

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / ".lfcache" / "repos" / "flask"

# (seconds, megabytes) per RUN step. The first row is what demo.py measured.
PROFILES = {
    "measured demo":    {"install": (4.19, 1.36), "compile": (0.57, 0.23),
                         "bundle": (0.66, 0.50)},
    "small service":    {"install": (20.0, 60.0), "compile": (6.0, 15.0),
                         "bundle": (4.0, 8.0)},
    "typical service":  {"install": (45.0, 180.0), "compile": (12.0, 40.0),
                         "bundle": (8.0, 25.0)},
    "heavy post-build": {"install": (45.0, 180.0), "compile": (90.0, 120.0),
                         "bundle": (30.0, 60.0)},
    "install dominates": {"install": (180.0, 600.0), "compile": (5.0, 10.0),
                          "bundle": (3.0, 5.0)},
}

COPY_MB = {"manifest": 0.13, "src": 0.09, "docs": 0.48, "tests": 0.04,
           "examples": 0.01, "meta": 0.03, "ci": 0.004}


def costs_for(profile) -> dict:
    costs = {k: StepCost(0.0, int(v * 1e6), True) for k, v in COPY_MB.items()}
    for name, (secs, mb) in profile.items():
        costs[name] = StepCost(secs, int(mb * 1e6), False)
    return costs


def costs_for_spec(pspec, base) -> dict:
    """Add the merged COPY layer a coarse policy uses.

    Its size is taken as the sum of the groups it swallows, which is what a
    single COPY of the same files costs to within compression noise.
    """
    costs = dict(base)
    named = {s.name for s in pspec.copies}
    folded = sum(v for k, v in COPY_MB.items() if k not in named)
    for s in pspec.copies:
        if s.name not in costs:
            costs[s.name] = StepCost(0.0, int(folded * 1e6), True)
    return costs


def main() -> int:
    if not REPO.exists():
        raise SystemExit("run demo.py first so the repository is cloned")
    spec = py_service_spec(ROOT / ".lfcache" / "wheelhouse")
    trace = commit_trace(REPO, 599)
    _names, masks = change_masks(spec, trace)
    print("{} commits of real history, {} build steps".format(len(trace),
                                                              len(spec.steps)))
    print()
    print("{:<19} {:>9} {:>9} {:>9} {:>9} {:>8}".format(
        "RUN cost profile", "mono s", "guide s", "exact s", "vs guide",
        "vs mono"))
    for label, profile in PROFILES.items():
        costs = costs_for(profile)
        pol = build_policies(spec, trace, costs, "seconds")
        out = {}
        for name in ("monolith", "manifest-first", "layerfloor"):
            pspec, order = pol[name]
            _n, m = change_masks(pspec, trace)
            out[name] = replay(pspec, order, m,
                               costs_for_spec(pspec, costs))["seconds"]
        print("{:<19} {:>9.2f} {:>9.2f} {:>9.2f} {:>8.2f}x {:>7.2f}x".format(
            label, out["monolith"], out["manifest-first"], out["layerfloor"],
            out["manifest-first"] / out["layerfloor"],
            out["monolith"] / out["layerfloor"]))
    print()
    print("mono  = one COPY . . followed by the RUN steps")
    print("guide = manifest copied first, then COPY . ., then the RUN steps")
    print("exact = layerfloor order over the same ten steps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

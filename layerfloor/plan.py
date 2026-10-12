"""Plan the layer order for a repository, without executing any build.

    python -m layerfloor.plan --repo . --spec myspec.json

The spec declares the COPY groups, the RUN steps, what each RUN step reads,
and how much each RUN step costs. COPY layer sizes are measured from the
current checkout; the change trace comes from git history.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .engine import write_layer
from .model import StepCost, optimal_order, ratio_rule_order, replay
from .policies import as_dockerfile
from .spec import COPY, RUN, BuildSpec, Step
from .trace import change_masks, change_rates, commit_trace


def load_spec(path: Path) -> tuple:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    steps, costs = [], {}
    for grp in doc["copy"]:
        steps.append(Step(name=grp["name"], kind=COPY,
                          paths=tuple(grp["paths"])))
    for r in doc["run"]:
        steps.append(Step(name=r["name"], kind=RUN, outdir=r.get("outdir", ""),
                          argv=tuple(r.get("argv", ())),
                          needs=tuple(r.get("needs", ()))))
        costs[r["name"]] = StepCost(seconds=float(r.get("seconds", 1.0)),
                                    nbytes=int(float(r.get("megabytes", 1.0))
                                               * 1e6),
                                    reproducible=bool(r.get("reproducible")))
    return BuildSpec(doc.get("name", "spec"), steps), costs, doc


def measure_copy_sizes(repo: Path, spec: BuildSpec, costs: dict) -> dict:
    """Compressed size of each COPY group in the current checkout."""
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"],
                             capture_output=True, text=True,
                             encoding="utf-8").stdout.split("\n")
    tracked = [p for p in tracked if p]
    for step in spec.copies:
        members = [p for p in tracked if step.matches(p)]
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "s"
            for rel in members:
                src = Path(repo) / rel
                if not src.is_file():
                    continue
                dest = stage / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src.read_bytes())
            stage.mkdir(parents=True, exist_ok=True)
            blob = Path(tmp) / "l.tgz"
            size = write_layer(stage, blob)
        costs[step.name] = StepCost(seconds=0.0, nbytes=size,
                                    reproducible=True)
    return costs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--commits", type=int, default=500)
    ap.add_argument("--metric", default="seconds", choices=["seconds", "bytes"])
    ap.add_argument("--dockerfile", help="write the planned Dockerfile here")
    args = ap.parse_args(argv)

    repo = Path(args.repo)
    spec, costs, doc = load_spec(Path(args.spec))
    trace = commit_trace(repo, args.commits)
    if not trace:
        raise SystemExit("no commit history found in " + str(repo))
    costs = measure_copy_sizes(repo, spec, costs)
    names, masks = change_masks(spec, trace)
    rates = change_rates(names, masks)

    order = optimal_order(spec, masks, costs, args.metric)
    current = doc.get("current_order") or (
        [s.name for s in spec.copies] + [s.name for s in spec.runs])

    print("commits analysed: " + str(len(trace)))
    print("change rate per COPY group:")
    for n in names:
        print("  {:14s} {:.3f}  {:8.2f} MB".format(n, rates[n],
                                                   costs[n].nbytes / 1e6))
    print()
    print("ratio rule over the COPY groups alone, descending "
          "cost * (1 - q) / q (the precedence free optimum):")
    for n in ratio_rule_order(names, costs, rates, "bytes"):
        print("  " + n)
    print()
    for label, o in (("current", current), ("layerfloor", order)):
        try:
            spec.validate_order(o)
        except ValueError as exc:
            print(label + " order is infeasible: " + str(exc))
            continue
        out = replay(spec, o, masks, costs)
        print("{:11s} {:7.3f} s and {:7.3f} MB per commit   {}".format(
            label, out["seconds"], out["bytes"] / 1e6, " -> ".join(o)))

    text = as_dockerfile(spec, order, base=doc.get("base", "python:3.13-slim"))
    if args.dockerfile:
        Path(args.dockerfile).write_text(text, encoding="utf-8")
        print()
        print("wrote " + args.dockerfile)
    else:
        print()
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""End to end demo: measure what layer ordering costs on a real repository.

Fits the ordering on an older slice of the commit history, then executes every
build in a later held-out slice under each ordering and records real seconds
and real registry uploads.

    python demo.py                      # reproduces the published chart
    python demo.py --eval 5 --fit 200   # quicker, for a smoke test
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from layerfloor.engine import BlobStore, Builder, Session
from layerfloor.model import StepCost, replay
from layerfloor.policies import as_dockerfile, build_policies
from layerfloor.spec import RUN
from layerfloor.specs import py_service_spec
from layerfloor.trace import (change_masks, change_rates, commit_trace,
                              ensure_repo, uncovered_paths)

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".lfcache"
WHEELS = CACHE / "wheelhouse"
DEFAULT_REPO = "https://github.com/pallets/flask.git"
PACKAGES = ["blinker", "click", "itsdangerous", "jinja2", "markupsafe",
            "werkzeug"]


def ensure_wheelhouse() -> Path:
    WHEELS.mkdir(parents=True, exist_ok=True)
    if list(WHEELS.glob("*.whl")):
        return WHEELS
    import subprocess
    print("fetching wheels for the install step ...")
    subprocess.run([sys.executable, "-m", "pip", "download", "--quiet",
                    "--only-binary=:all:", "--dest", str(WHEELS)] + PACKAGES,
                   check=True)
    return WHEELS


def topological(spec) -> list:
    """Declaration order made feasible: COPY steps first, then RUN steps."""
    out = [s.name for s in spec.copies]
    pending = list(spec.runs)
    while pending:
        for step in list(pending):
            if all(n in out for n in step.needs):
                out.append(step.name)
                pending.remove(step)
                break
        else:
            raise ValueError("cyclic precedence")
    return out


def measure_costs(builder, spec, sha, base=None) -> dict:
    """One cold build gives every step's real seconds and layer size.

    A second cold build of the same content decides, by digest, whether each
    step is reproducible: a reproducible layer costs nothing to upload when it
    is needlessly re-executed, an irreproducible one costs its full size.

    RUN costs are measured once and shared across the policies through `base`.
    The policies differ only in order, so re-probing each one would feed
    measurement noise into the comparison. COPY layer sizes are always
    measured per spec, because merging COPY groups really does change them.
    """
    order = topological(spec)
    first = builder.build(spec, order, sha, Session("cost-a"))
    second = builder.build(spec, order, sha, Session("cost-b"))
    costs = {}
    for a, b in zip(first.steps, second.steps):
        is_copy = spec.by_name(a.name).is_copy
        if base and not is_copy and a.name in base:
            costs[a.name] = base[a.name]
            continue
        costs[a.name] = StepCost(
            seconds=0.0 if is_copy else min(a.seconds, b.seconds),
            nbytes=a.layer_bytes,
            reproducible=(a.digest == b.digest),
            materialise_seconds=builder.copy_cost(a.digest) if is_copy else 0.0)
    return costs


def run_policy(builder, spec, order, name, warm_sha, commits, verbose=True):
    """Execute every held-out commit under one ordering.

    Records which build commands the engine actually had to re-run, and how
    long each of those executions took. The two are combined afterwards, once
    every policy has contributed samples, so that each command is priced the
    same way everywhere and a busy machine cannot move the comparison.
    """
    session = Session(name)
    builder.build(spec, order, warm_sha, session)       # prior image, not scored
    secs, pushed, misses, events, samples = [], [], [], [], []
    t0 = time.perf_counter()
    for i, (sha, _paths) in enumerate(commits):
        res = builder.build(spec, order, sha, session)
        ran = [s for s in res.steps if not s.cached and s.kind == RUN]
        events.append([s.name for s in ran])
        samples.extend((s.name, s.seconds) for s in ran)
        secs.append(res.seconds)
        pushed.append(res.pushed_bytes)
        misses.append(res.misses)
        if verbose and (i + 1) % 10 == 0:
            print("    {:>3}/{} commits, {:.1f}s elapsed".format(
                i + 1, len(commits), time.perf_counter() - t0), flush=True)
    return {
        "wall_seconds": statistics.fmean(secs),
        "bytes": statistics.fmean(pushed),
        "misses": statistics.fmean(misses),
        "wall_seconds_total": sum(secs),
        "bytes_total": sum(pushed),
        "per_commit_events": events,
        "per_commit_samples": samples,
        "per_commit_bytes": pushed,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help="git URL or local path of the repository to study")
    ap.add_argument("--fit", type=int, default=575,
                    help="commits used to fit the ordering")
    ap.add_argument("--eval", type=int, default=25,
                    help="later commits held out for the measured builds")
    ap.add_argument("--metric", default="seconds", choices=["seconds", "bytes"],
                    help="quantity the optimiser minimises")
    ap.add_argument("--out", default=str(ROOT / "docs"),
                    help="where the chart, results and Dockerfile are written")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.repo.startswith(("http", "git@")):
        slug = args.repo.rstrip("/").split("/")[-1].removesuffix(".git")
        repo = ensure_repo(args.repo, CACHE / "repos" / slug)
    else:
        repo, slug = Path(args.repo), Path(args.repo).name
    wheels = ensure_wheelhouse()

    spec = py_service_spec(wheels)
    trace = commit_trace(repo, args.fit + args.eval)
    if len(trace) < args.eval + 2:
        raise SystemExit("not enough history: " + str(len(trace)) + " commits")
    fit_trace, eval_trace = trace[:-args.eval], trace[-args.eval:]
    print("repository {}: {} commits with file changes, {} fit / {} held out"
          .format(slug, len(trace), len(fit_trace), len(eval_trace)))

    names, fit_masks = change_masks(spec, fit_trace)
    rates = change_rates(names, fit_masks)
    print("change rate per COPY step (fit window):")
    for n in names:
        print("  {:10s} {:.3f}".format(n, rates[n]))
    stray = uncovered_paths(spec, trace)
    if stray:
        print("paths claimed by no COPY step: {} ({} touches)"
              .format(len(stray), sum(stray.values())))

    blobs = BlobStore(CACHE / "blobs")
    builder = Builder(repo, CACHE / "work", blobs, env={"PYTHONPATH": str(ROOT)})

    print("measuring step costs with two cold builds ...")
    costs = measure_costs(builder, spec, eval_trace[0][0])
    for n in topological(spec):
        c = costs[n]
        print("  {:10s} run {:6.2f}s  copy {:5.2f}s  {:7.2f} MB  "
              "reproducible={}".format(n, c.seconds, c.materialise_seconds,
                                       c.nbytes / 1e6, c.reproducible))

    policies = build_policies(spec, fit_trace, costs, args.metric)
    warm_sha = fit_trace[-1][0]
    results, orders = {}, {}
    for name, (pspec, order) in policies.items():
        pcosts = costs
        if pspec is not spec:
            pcosts = measure_costs(builder, pspec, eval_trace[0][0], base=costs)
        print("policy {}: {}".format(name, " -> ".join(order)), flush=True)
        measured = run_policy(builder, pspec, order, name, warm_sha,
                              eval_trace)
        _n, emasks = change_masks(pspec, eval_trace)
        predicted = replay(pspec, order, emasks, pcosts)
        measured["predicted_seconds"] = predicted["seconds"]
        measured["predicted_bytes"] = predicted["bytes"]
        measured["steps_rebuilt"] = predicted["steps"]
        results[name] = measured
        orders[name] = order
        print("  {:.3f} MB per commit; {:.2f} cache misses against {:.2f} "
              "predicted; wall clock {:.3f} s"
              .format(measured["bytes"] / 1e6, measured["misses"],
                      predicted["steps"], measured["wall_seconds"]),
              flush=True)

    # Price every re-execution the engine performed at the median of all
    # that command's real executions, pooled over every policy. The event
    # counts are observed, the price is measured many times over, and both
    # are identical across policies, so neither machine noise nor the cold
    # cost probe can move the comparison.
    pool = {}
    for r in results.values():
        for step_name, secs in r["per_commit_samples"]:
            pool.setdefault(step_name, []).append(secs)
    price = {k: statistics.median(v) for k, v in sorted(pool.items())}
    print()
    print("price per re-execution, median of all observed runs:")
    for k, v in price.items():
        print("  {:10s} {:6.3f} s over {} executions".format(
            k, v, len(pool[k])))
    for r in results.values():
        r["seconds"] = statistics.fmean(
            sum(price[n] for n in ev) for ev in r["per_commit_events"])
        r["commands_rerun"] = statistics.fmean(
            len(ev) for ev in r["per_commit_events"])

    from layerfloor.report import before_after_chart
    meta = {"repo": slug, "fit_commits": len(fit_trace),
            "eval_commits": len(eval_trace), "metric": args.metric,
            "price_seconds": {k: round(v, 4) for k, v in price.items()},
            "price_samples": {k: len(v) for k, v in pool.items()},
            "orders": orders, "change_rates": rates,
            "step_costs": {k: {"seconds": round(v.seconds, 4),
                               "bytes": v.nbytes,
                               "reproducible": v.reproducible,
                               "materialise_seconds": round(
                                   v.materialise_seconds, 4)}
                           for k, v in costs.items()}}
    before_after_chart(results, meta, out_dir / "before_after.png")
    (out_dir / "Dockerfile.layerfloor").write_text(
        as_dockerfile(spec, orders["layerfloor"],
                      subs={str(Path(wheels).resolve()): "/wheels"}),
        encoding="utf-8")
    slim = {k: {kk: vv for kk, vv in v.items()
                if kk not in ("per_commit_samples", "per_commit_events")}
            for k, v in results.items()}
    (out_dir / "results.json").write_text(
        json.dumps({"meta": meta, "results": slim}, indent=2, default=str),
        encoding="utf-8")

    print()
    print("{:<16} {:>10} {:>11} {:>9} {:>10} {:>8}".format(
        "policy", "sec/commit", "MB/commit", "cmds/cmt", "wall sec",
        "vs best"))
    best = min(r["seconds"] for r in results.values())
    for name in ["monolith", "churn-sort", "manifest-first", "layerfloor-seq",
                 "layerfloor"]:
        r = results[name]
        print("{:<16} {:>10.3f} {:>11.3f} {:>9.2f} {:>10.3f} {:>7.2f}x"
              .format(name, r["seconds"], r["bytes"] / 1e6,
                      r["commands_rerun"], r["wall_seconds"],
                      r["seconds"] / best))
    print()
    print(json.dumps(slim, indent=2, default=str))
    print("wrote", out_dir / "before_after.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

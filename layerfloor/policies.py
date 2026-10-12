"""The layer orderings being compared.

monolith        COPY . . then every RUN step. The first Dockerfile anyone writes.
manifest-first  COPY the dependency manifest, install, then COPY . . and build.
                The ordering every container guide recommends.
churn-sort      Fine grained COPY steps sorted by ascending change frequency,
                then the RUN steps. The folklore rule applied at full granularity.
layerfloor      Exact minimum-cost order over the same fine grained steps.
layerfloor-seq  Ablation: exact order, but forbidden to place a COPY step after
                any RUN step. Isolates the gain that comes from interleaving.
"""
from dataclasses import replace

from .model import optimal_order
from .spec import BuildSpec
from .trace import change_masks, change_rates


def _topological_runs(spec: BuildSpec, placed) -> list:
    """Order RUN steps so each follows its inputs, keeping declaration order."""
    out, seen = [], set(placed)
    pending = list(spec.runs)
    while pending:
        progressed = False
        for step in list(pending):
            if all(n in seen for n in step.needs):
                out.append(step.name)
                seen.add(step.name)
                pending.remove(step)
                progressed = True
        if not progressed:
            raise ValueError("cyclic precedence among RUN steps")
    return out


def _sequential_spec(spec: BuildSpec) -> BuildSpec:
    """Every RUN step depends on every COPY step: no interleaving allowed."""
    copies = [s.name for s in spec.copies]
    steps = list(spec.copies) + [
        replace(r, needs=tuple(dict.fromkeys(list(r.needs) + copies)))
        for r in spec.runs]
    return BuildSpec(spec.name + "-seq", steps)


def build_policies(spec: BuildSpec, fit_trace: list, costs: dict,
                   metric: str = "seconds") -> dict:
    """Return {policy name: (spec variant, order)} fitted on fit_trace only."""
    policies = {}

    mono = spec.merge_copies(keep=(), merged_name="app")
    policies["monolith"] = (mono, ["app"] + _topological_runs(mono, ["app"]))

    mf = spec.merge_copies(keep=("manifest",), merged_name="app")
    policies["manifest-first"] = (mf, ["manifest", "install", "app",
                                       "compile", "bundle"])

    names, masks = change_masks(spec, fit_trace)
    rates = change_rates(names, masks)
    ordered_copies = sorted(names, key=lambda n: (rates[n], n))
    policies["churn-sort"] = (spec, ordered_copies +
                              _topological_runs(spec, ordered_copies))

    policies["layerfloor"] = (spec, optimal_order(spec, masks, costs, metric))

    seq = _sequential_spec(spec)
    policies["layerfloor-seq"] = (seq, optimal_order(seq, masks, costs, metric))
    return policies


def as_dockerfile(spec: BuildSpec, order, base="python:3.13-slim",
                  subs=None) -> str:
    """Emit the ordering as a Dockerfile.

    A COPY group with several paths becomes one COPY instruction per path,
    kept adjacent. Adjacent COPY instructions have the same invalidation set
    as one merged layer, because a change in any of them already invalidates
    everything that follows, and splitting them lets an unchanged path keep
    its blob.
    """
    subs = subs or {}
    lines = ["FROM " + base, "WORKDIR /app"]
    for name in order:
        step = spec.by_name(name)
        lines.append("")
        lines.append("# layer group: " + name)
        if step.is_copy:
            files = [p for p in step.paths if not p.endswith("/")]
            dirs = [p for p in step.paths if p.endswith("/")]
            if files:
                lines.append("COPY " + " ".join(files) + " ./")
            for d in dirs:
                lines.append("COPY " + d + " ./" + d)
        else:
            argv = []
            for a in step.argv:
                a = a.replace("{PY}", "python")
                for old, repl in subs.items():
                    a = a.replace(old, repl)
                argv.append(a)
            lines.append("RUN " + " ".join(argv))
    return chr(10).join(lines) + chr(10)

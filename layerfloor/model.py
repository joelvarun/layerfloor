"""Cost model and exact layer ordering.

Docker's build cache is a prefix cache: a miss on step i forces every step
after i to re-execute. The cost of placing a step therefore depends only on
the SET of steps placed before it, never on their internal order, so the
minimum-cost order is found exactly by a dynamic program over subsets.
"""
import itertools
import math
from collections import Counter
from dataclasses import dataclass

from .spec import BuildSpec


@dataclass(frozen=True)
class StepCost:
    """Measured cost of one step.

    seconds is the execution time of a RUN command. A COPY step carries
    seconds = 0: re-applying a layer whose content is already in the local
    store is not work the ordering can save, while re-running a build command
    is. The measured materialisation time is kept for reference only.
    """
    seconds: float
    nbytes: int
    reproducible: bool = False
    materialise_seconds: float = 0.0


def _metric(cost: StepCost, metric: str) -> float:
    if metric == "seconds":
        return cost.seconds
    if metric == "bytes":
        return float(cost.nbytes)
    raise ValueError("unknown metric " + metric)


def replay(spec: BuildSpec, order, masks, costs) -> dict:
    """Replay the prefix cache over a real commit trace.

    Returns mean rebuilt seconds, mean pushed bytes and mean re-executed steps
    per commit. Mirrors engine.build exactly: an unchanged COPY that is
    re-executed pushes nothing because its layer blob is content addressed,
    while a re-executed RUN pushes a fresh blob unless it is reproducible.
    """
    copy_index = {s.name: i for i, s in enumerate(spec.copies)}
    steps = [spec.by_name(n) for n in order]
    tot_s = tot_b = tot_n = 0.0
    for m in masks:
        dirty = False
        for step in steps:
            changed = step.is_copy and bool(m >> copy_index[step.name] & 1)
            dirty = dirty or changed
            if not dirty:
                continue
            c = costs[step.name]
            tot_s += c.seconds
            tot_n += 1
            if step.is_copy:
                if changed:
                    tot_b += c.nbytes
            elif not c.reproducible:
                tot_b += c.nbytes
    n = max(len(masks), 1)
    return {"seconds": tot_s / n, "bytes": tot_b / n, "steps": tot_n / n,
            "commits": len(masks)}


def _mask_counts(masks):
    return list(Counter(masks).items())


def optimal_order(spec: BuildSpec, masks, costs, metric: str = "seconds") -> list:
    """Exact minimum-cost order by dynamic programming over subsets.

    O(2^n * n * d) for n steps and d distinct commit masks, against n! for a
    permutation search. Precedence (a RUN reads earlier COPY steps) is
    enforced by only expanding feasible steps.
    """
    steps = spec.steps
    n = len(steps)
    if n > 22:
        raise ValueError(
            "exact ordering is exponential in the step count; " + str(n) +
            " steps needs " + str(1 << n) + " states. Merge COPY groups "
            "first, or order the groups by the ratio rule instead.")
    idx = {s.name: i for i, s in enumerate(steps)}
    copy_bit = [-1] * n
    for j, s in enumerate(spec.copies):
        copy_bit[idx[s.name]] = j
    need_mask = [sum(1 << idx[d] for d in s.needs) for s in steps]
    counts = _mask_counts(masks)
    total = max(len(masks), 1)

    self_rate = [0.0] * n
    for i in range(n):
        if copy_bit[i] >= 0:
            bit = 1 << copy_bit[i]
            self_rate[i] = sum(c for m, c in counts if m & bit) / total

    cmask = [0] * (1 << n)
    pany = [0.0] * (1 << n)
    for S in range(1, 1 << n):
        low = S & -S
        i = low.bit_length() - 1
        cm = cmask[S ^ low] | (1 << copy_bit[i] if copy_bit[i] >= 0 else 0)
        cmask[S] = cm
        pany[S] = sum(c for m, c in counts if m & cm) / total

    inf = math.inf
    f = [inf] * (1 << n)
    pick = [-1] * (1 << n)
    f[0] = 0.0
    for S in range(1, 1 << n):
        best, best_j, best_tie = inf, -1, 0.0
        for i in range(n):
            bit = 1 << i
            if not S & bit:
                continue
            rest = S ^ bit
            if f[rest] == inf or need_mask[i] & ~rest:
                continue
            c = costs[steps[i].name]
            if metric == "seconds":
                add = c.seconds * pany[S]
            elif copy_bit[i] >= 0:
                add = c.nbytes * self_rate[i]
            else:
                add = 0.0 if c.reproducible else c.nbytes * pany[S]
            val = f[rest] + add
            tie = self_rate[i]
            if val < best - 1e-12 or (val < best + 1e-12 and tie > best_tie):
                best, best_j, best_tie = min(val, best), i, tie
        f[S], pick[S] = best, best_j

    order = []
    S = (1 << n) - 1
    while S:
        i = pick[S]
        if i < 0:
            raise ValueError("no feasible order satisfies the precedence graph")
        order.append(steps[i].name)
        S ^= 1 << i
    order.reverse()
    return order


def expected_cost_independent(order, costs, rates, metric="seconds") -> float:
    """Closed-form cost when step changes are independent Bernoulli events."""
    survive = 1.0
    total = 0.0
    for name in order:
        q = rates.get(name, 0.0)
        survive *= 1.0 - q
        total += _metric(costs[name], metric) * (1.0 - survive)
    return total


def ratio_rule_order(names, costs, rates, metric="seconds") -> list:
    """Order by descending c*(1-q)/q, the exact optimum under independence.

    Adjacent exchange of a then b costs R*(s_b*(1-q_b)*q_a - s_a*(1-q_a)*q_b),
    so a belongs first exactly when s_a*(1-q_a)/q_a exceeds the same ratio for
    b. Large expensive steps that rarely change go first; cheap churning steps
    go last. Sorting by change frequency alone ignores the cost factor.
    """
    def sort_key(name):
        q = rates.get(name, 0.0)
        s = _metric(costs[name], metric)
        if q <= 0.0:
            return (0, 0.0, name)
        return (1, -(s * (1.0 - q) / q), name)
    return sorted(names, key=sort_key)


def brute_force_order(spec: BuildSpec, masks, costs, metric="seconds"):
    """Reference implementation: evaluate every feasible permutation."""
    best, best_order = math.inf, None
    for perm in itertools.permutations(spec.names):
        try:
            spec.validate_order(perm)
        except ValueError:
            continue
        val = replay(spec, perm, masks, costs)[metric]
        if val < best - 1e-12:
            best, best_order = val, list(perm)
    return best_order, best

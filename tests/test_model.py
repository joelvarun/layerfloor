"""The exact ordering must agree with an exhaustive permutation search."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from layerfloor.model import (StepCost, brute_force_order,
                              expected_cost_independent, optimal_order,
                              ratio_rule_order, replay)
from layerfloor.spec import COPY, RUN, BuildSpec, Step


def random_instance(rng, n_copy=4, n_run=2, n_commits=30):
    copies = [Step(name="c" + str(i), kind=COPY, paths=("d" + str(i) + "/",))
              for i in range(n_copy)]
    runs = []
    for j in range(n_run):
        needs = tuple(rng.sample([c.name for c in copies],
                                 k=rng.randint(1, max(1, n_copy // 2))))
        runs.append(Step(name="r" + str(j), kind=RUN, outdir="o" + str(j),
                         argv=("echo", str(j)), needs=needs))
    spec = BuildSpec("rand", copies + runs)
    costs = {}
    for s in spec.steps:
        costs[s.name] = StepCost(seconds=round(rng.uniform(0.05, 9.0), 3),
                                 nbytes=rng.randint(10_000, 40_000_000),
                                 reproducible=False)
    masks = []
    for _ in range(n_commits):
        m = 0
        while m == 0:
            m = rng.getrandbits(n_copy)
        masks.append(m)
    return spec, costs, masks


def test_dp_matches_brute_force():
    rng = random.Random(20261012)
    for _ in range(6):
        spec, costs, masks = random_instance(rng)
        for metric in ("seconds", "bytes"):
            dp = optimal_order(spec, masks, costs, metric)
            spec.validate_order(dp)
            bf, bf_cost = brute_force_order(spec, masks, costs, metric)
            dp_cost = replay(spec, dp, masks, costs)[metric]
            assert abs(dp_cost - bf_cost) < 1e-9, (metric, dp, bf,
                                                   dp_cost, bf_cost)


def test_ratio_rule_is_optimal_when_independent():
    """Descending cost*(1-q)/q minimises the independent-change objective."""
    import itertools
    rng = random.Random(7)
    for _ in range(40):
        names = ["s" + str(i) for i in range(5)]
        costs = {n: StepCost(seconds=round(rng.uniform(0.1, 20), 3), nbytes=0)
                 for n in names}
        rates = {n: round(rng.uniform(0.02, 0.9), 3) for n in names}
        rule = ratio_rule_order(names, costs, rates)
        best = min(expected_cost_independent(p, costs, rates)
                   for p in itertools.permutations(names))
        got = expected_cost_independent(rule, costs, rates)
        assert abs(got - best) < 1e-9, (rule, got, best)


def test_frequency_sort_is_not_optimal():
    """A counterexample: ordering by change frequency alone loses to the rule.

    b changes more often than a but is far cheaper, so the frequency rule puts
    b first and pays for a on every b change.
    """
    costs = {"a": StepCost(seconds=1.0, nbytes=0),
             "b": StepCost(seconds=40.0, nbytes=0)}
    rates = {"a": 0.10, "b": 0.30}
    freq = sorted(costs, key=lambda n: rates[n])
    rule = ratio_rule_order(list(costs), costs, rates)
    assert freq == ["a", "b"] and rule == ["b", "a"]
    assert (expected_cost_independent(rule, costs, rates)
            < expected_cost_independent(freq, costs, rates))


def test_clean_commit_costs_nothing():
    spec, costs, _ = random_instance(random.Random(3))
    order = optimal_order(spec, [1], costs)
    assert replay(spec, order, [0], costs)["seconds"] == 0.0


def test_unchanged_copy_layer_pushes_nothing():
    """Re-executing a COPY whose content did not change uploads no bytes."""
    copies = [Step(name="a", kind=COPY, paths=("a/",)),
              Step(name="b", kind=COPY, paths=("b/",))]
    spec = BuildSpec("two", copies)
    costs = {"a": StepCost(0.1, 1_000_000), "b": StepCost(0.1, 2_000_000)}
    only_a_changed = [0b01]
    out = replay(spec, ["a", "b"], only_a_changed, costs)
    assert out["bytes"] == 1_000_000
    assert out["seconds"] == 0.2


def test_merge_copies_keeps_every_path():
    from layerfloor.specs import py_service_spec
    spec = py_service_spec(Path("."))
    merged = spec.merge_copies(keep=("manifest",))
    before = {p for s in spec.copies for p in s.paths}
    after = {p for s in merged.copies for p in s.paths}
    assert before == after
    assert len(merged.copies) == 2
    for r in merged.runs:
        assert all(n in merged.names for n in r.needs)


def test_no_interleaving_means_order_cannot_help():
    """Copies first, runs last: every ordering costs exactly the same.

    If a commit changes any COPY group at all then the prefix is dirty before
    the first RUN step is reached, so every RUN step re-executes whatever the
    COPY order was; and if it changes none, nothing re-executes. A COPY layer
    is pushed exactly when its own content changed, which is also independent
    of position. Reordering COPY groups therefore changes neither rebuild time
    nor uploaded bytes. Sorting layers by change frequency cannot pay on its
    own; only moving a RUN step earlier can.
    """
    import itertools
    rng = random.Random(99)
    spec, costs, masks = random_instance(rng, n_copy=4, n_run=2)
    costs = {k: StepCost(0.0 if k.startswith("c") else v.seconds, v.nbytes,
                         v.reproducible) for k, v in costs.items()}
    runs = [s.name for s in spec.runs]
    seen = set()
    for perm in itertools.permutations([s.name for s in spec.copies]):
        out = replay(spec, list(perm) + runs, masks, costs)
        seen.add((round(out["seconds"], 9), round(out["bytes"], 6)))
    assert len(seen) == 1, seen


def test_interleaving_beats_every_sequential_order():
    """Placing a RUN step before a churning COPY group strictly wins."""
    steps = [Step(name="c0", kind=COPY, paths=("a/",)),
             Step(name="c1", kind=COPY, paths=("b/",)),
             Step(name="r0", kind=RUN, outdir="o", argv=("x",), needs=("c0",))]
    spec = BuildSpec("tiny", steps)
    costs = {"c0": StepCost(0.0, 1000), "c1": StepCost(0.0, 1000),
             "r0": StepCost(10.0, 5000)}
    masks = [0b01, 0b10, 0b10, 0b10]
    seq = replay(spec, ["c0", "c1", "r0"], masks, costs)["seconds"]
    inter = replay(spec, ["c0", "r0", "c1"], masks, costs)["seconds"]
    assert seq == 10.0 and inter == 2.5
    assert optimal_order(spec, masks, costs) == ["c0", "r0", "c1"]

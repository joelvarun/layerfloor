"""Build specification: ordered steps that produce container image layers.

A step is either a COPY (materialise a set of repository paths into the build
context) or a RUN (execute a command that writes a layer into its own output
directory). `needs` records which steps a RUN step depends on, which is the
precedence the ordering must respect; `reads` names the paths its command
actually touches, which lets the engine skip unpacking the rest.
"""
from dataclasses import dataclass, field, replace
from typing import Sequence

COPY = "copy"
RUN = "run"


@dataclass(frozen=True)
class Step:
    name: str
    kind: str
    paths: tuple = ()
    argv: tuple = ()
    outdir: str = ""
    needs: tuple = ()
    reads: tuple = ()

    @property
    def is_copy(self) -> bool:
        return self.kind == COPY

    def matches(self, path: str) -> bool:
        """True when a repository path belongs to this COPY step."""
        for pre in self.paths:
            if pre.endswith("/"):
                if path.startswith(pre):
                    return True
            elif path == pre:
                return True
        return False


@dataclass
class BuildSpec:
    name: str
    steps: list = field(default_factory=list)

    def by_name(self, name: str) -> Step:
        for s in self.steps:
            if s.name == name:
                return s
        raise KeyError(name)

    @property
    def names(self) -> list:
        return [s.name for s in self.steps]

    @property
    def copies(self) -> list:
        return [s for s in self.steps if s.is_copy]

    @property
    def runs(self) -> list:
        return [s for s in self.steps if not s.is_copy]

    def validate_order(self, order: Sequence) -> None:
        seen = set()
        for name in order:
            step = self.by_name(name)
            missing = [n for n in step.needs if n not in seen]
            if missing:
                raise ValueError("step " + name + " placed before " + ",".join(missing))
            seen.add(name)
        if set(order) != set(self.names):
            raise ValueError("order is not a permutation of the step set")

    def merge_copies(self, keep: Sequence = (), merged_name: str = "app") -> "BuildSpec":
        """Collapse every COPY step except `keep` into one coarse COPY step.

        This is how the common Dockerfile shapes are expressed: a single
        `COPY . .` (keep nothing) or manifest-first (keep the manifest step).
        """
        keep = list(keep)
        folded = [s for s in self.copies if s.name not in keep]
        if not folded:
            return BuildSpec(self.name, list(self.steps))
        paths = tuple(p for s in folded for p in s.paths)
        coarse = Step(name=merged_name, kind=COPY, paths=paths)
        folded_names = {s.name for s in folded}
        steps = [s for s in self.copies if s.name in keep] + [coarse]
        for r in self.runs:
            needs = tuple(merged_name if n in folded_names else n for n in r.needs)
            steps.append(replace(r, needs=tuple(dict.fromkeys(needs))))
        return BuildSpec(self.name + "-merged", steps)

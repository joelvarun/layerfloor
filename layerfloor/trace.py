"""Extract a real change trace from a git repository.

The trace is the only empirical input the ordering model needs: for every
commit, which COPY steps of the build spec had their content changed.
"""
import subprocess
from pathlib import Path

from .spec import BuildSpec


def git(repo, *args, check=True):
    out = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and out.returncode != 0:
        raise RuntimeError("git " + " ".join(args) + " failed: " + out.stderr.strip())
    return out.stdout


def ensure_repo(url: str, dest: Path) -> Path:
    dest = Path(dest)
    if (dest / ".git").is_dir():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--quiet", url, str(dest)], check=True)
    return dest


def commit_trace(repo, limit: int, branch: str = "HEAD") -> list:
    """Return [(sha, [changed paths]), ...] oldest commit first.

    Merge commits are skipped: git reports no file list for them under the
    default diff, so they carry no churn signal.
    """
    raw = git(repo, "log", branch, "--no-merges", "-n", str(limit),
              "--name-only", "--pretty=format:@%H")
    trace = []
    sha = None
    paths = []
    for line in raw.splitlines():
        line = line.rstrip()
        if line.startswith("@"):
            if sha is not None:
                trace.append((sha, paths))
            sha, paths = line[1:], []
        elif line:
            paths.append(line.replace("\\", "/"))
    if sha is not None:
        trace.append((sha, paths))
    trace.reverse()
    return [(s, p) for s, p in trace if p]


def change_masks(spec: BuildSpec, trace: list) -> tuple:
    """Bitmask per commit over the spec's COPY steps.

    Returns (copy_names, masks). Bit i is set when commit t changed at least
    one path belonging to copy step i.
    """
    copies = spec.copies
    names = [s.name for s in copies]
    masks = []
    for _sha, paths in trace:
        m = 0
        for path in paths:
            for i, step in enumerate(copies):
                if step.matches(path):
                    m |= 1 << i
        masks.append(m)
    return names, masks


def change_rates(names, masks) -> dict:
    n = max(len(masks), 1)
    return {name: sum(1 for m in masks if m >> i & 1) / n
            for i, name in enumerate(names)}


def uncovered_paths(spec: BuildSpec, trace: list) -> dict:
    """Paths touched by the trace that no COPY step claims, with hit counts."""
    out = {}
    copies = spec.copies
    for _sha, paths in trace:
        for path in paths:
            if not any(s.matches(path) for s in copies):
                out[path] = out.get(path, 0) + 1
    return out

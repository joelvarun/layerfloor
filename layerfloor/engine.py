"""A small build engine that really executes the steps it measures.

It reproduces the two mechanisms that decide what a rebuild costs:
  1. a prefix step cache keyed by (parent key, step identity), exactly how a
     container builder invalidates everything after a changed instruction;
  2. a content addressed registry, so a re-executed step that produces the
     same bytes uploads nothing.
Layer archives are written with normalised tar metadata, so a layer digest is
a pure function of file content. Whether a RUN step is reproducible under that
definition is measured, not assumed.
"""
import gzip
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


def _norm(ti):
    ti.mtime = 0
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mode = 0o755 if ti.isdir() else 0o644
    return ti


def write_layer(src_root: Path, out_path: Path, normalise: bool = True) -> int:
    """Write a tar.gz of src_root in sorted order. Returns compressed size.

    normalise=True strips tar metadata, so the digest is a pure function of
    file content. That is the right model for a COPY layer, whose content is
    exactly the files in the build context. RUN layers are archived as the
    command produced them, timestamps included, because that is what a
    container builder commits.
    """
    files = sorted(p for p in Path(src_root).rglob("*") if p.is_file())
    with open(out_path, "wb") as fh:
        gz = gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, compresslevel=6)
        try:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tf:
                for p in files:
                    arc = str(p.relative_to(src_root)).replace(os.sep, "/")
                    tf.add(str(p), arcname=arc,
                           filter=_norm if normalise else None, recursive=False)
        finally:
            gz.close()
    return out_path.stat().st_size


def normalise_tar_stream(src_tar: Path, out_path: Path) -> int:
    """Re-emit a tar as a normalised tar.gz without unpacking it to disk.

    git archive writes members in tree order, which is a function of the tree
    alone, so the result is still content addressed.
    """
    with open(out_path, "wb") as fh:
        gz = gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, compresslevel=6)
        try:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tf:
                with tarfile.open(src_tar) as src:
                    for ti in src:
                        if not ti.isfile():
                            continue
                        body = src.extractfile(ti)
                        tf.addfile(_norm(ti), body)
        finally:
            gz.close()
    return out_path.stat().st_size


def digest_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class BlobStore:
    """Shared local content store. Holds every layer any policy produced."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, dg: str) -> Path:
        return self.root / (dg + ".tgz")

    def put(self, src: Path) -> tuple:
        dg = digest_of(src)
        dest = self.path(dg)
        size = src.stat().st_size
        if not dest.exists():
            shutil.copyfile(src, dest)
        return dg, size

    def extract(self, dg: str, dest: Path, prefixes=None) -> None:
        """Apply a layer to a build context, writing only what differs.

        A file already on disk with identical bytes is left alone. The content
        is the same either way; skipping the write just avoids paying the
        filesystem for it, which on a large unchanged tree is most of the
        cost of applying a layer.
        """
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(self.path(dg)) as tf:
            for m in tf:
                if not m.isfile():
                    continue
                if prefixes is not None and not any(
                        m.name == p or m.name.startswith(p.rstrip("/") + "/")
                        for p in prefixes):
                    continue
                src = tf.extractfile(m)
                if src is None:
                    continue
                data = src.read()
                target = dest / m.name
                if (target.is_file() and target.stat().st_size == len(data)
                        and target.read_bytes() == data):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)


class Registry:
    """Per-policy upload accounting: a digest already held transfers nothing."""

    def __init__(self):
        self.have = set()
        self.pushed_bytes = 0
        self.deduped_bytes = 0

    def push(self, dg: str, size: int) -> int:
        if dg in self.have:
            self.deduped_bytes += size
            return 0
        self.have.add(dg)
        self.pushed_bytes += size
        return size


@dataclass
class Session:
    """One policy's build history: its cache, its registry, its context.

    The build context persists across commits, as a builder's does. A step
    that hits the cache needs nothing re-applied, because the files its layer
    would write are already the ones on disk.
    """
    name: str
    registry: Registry = field(default_factory=Registry)
    cache: dict = field(default_factory=dict)
    stage: Path = None


@dataclass
class StepRun:
    name: str
    kind: str
    cached: bool
    seconds: float
    layer_bytes: int
    pushed_bytes: int
    digest: str


@dataclass
class BuildResult:
    sha: str
    policy: str
    steps: list = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return sum(s.seconds for s in self.steps if not s.cached)

    @property
    def pushed_bytes(self) -> int:
        return sum(s.pushed_bytes for s in self.steps)

    @property
    def misses(self) -> int:
        return sum(1 for s in self.steps if not s.cached)


class Builder:
    """Executes build specs against real commits of a real git repository."""

    def __init__(self, repo: Path, workroot: Path, blobs: BlobStore, env: dict = None):
        self.repo = Path(repo)
        self.workroot = Path(workroot)
        self.workroot.mkdir(parents=True, exist_ok=True)
        self.blobs = blobs
        self.env = dict(os.environ)
        self.env.update(env or {})
        self._ctx = {}
        self._tree = {}
        self._extract_cost = {}

    def _tree_paths(self, sha: str) -> set:
        if sha not in self._tree:
            out = subprocess.run(
                ["git", "-C", str(self.repo), "ls-tree", "-r", "--name-only", sha],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            self._tree[sha] = set(out.stdout.split("\n")) - {""}
        return self._tree[sha]

    def _archive_args(self, sha: str, step) -> list:
        present = self._tree_paths(sha)
        args = []
        for pre in step.paths:
            if pre.endswith("/"):
                if any(p.startswith(pre) for p in present):
                    args.append(pre.rstrip("/"))
            elif pre in present:
                args.append(pre)
        return args

    def context_layer(self, sha: str, step) -> tuple:
        """Deterministic layer for one COPY step at one commit: (digest, size)."""
        key = (sha, step.name)
        if key in self._ctx:
            return self._ctx[key]
        args = self._archive_args(sha, step)
        tmp = Path(tempfile.mkdtemp(dir=self.workroot, prefix="ctx"))
        try:
            blob = tmp / "layer.tgz"
            if args:
                tar_path = tmp / "a.tar"
                with open(tar_path, "wb") as fh:
                    subprocess.run(["git", "-C", str(self.repo), "archive", sha,
                                    "--"] + args, stdout=fh, check=True)
                size = normalise_tar_stream(tar_path, blob)
            else:
                empty = tmp / "s"
                empty.mkdir()
                size = write_layer(empty, blob)
            dg, _ = self.blobs.put(blob)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._ctx[key] = (dg, size)
        return self._ctx[key]

    def copy_cost(self, dg: str) -> float:
        """Seconds to materialise one COPY layer, measured once per digest."""
        if dg not in self._extract_cost:
            tmp = Path(tempfile.mkdtemp(dir=self.workroot, prefix="mc"))
            try:
                t0 = time.perf_counter()
                self.blobs.extract(dg, tmp)
                self._extract_cost[dg] = time.perf_counter() - t0
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return self._extract_cost[dg]

    @staticmethod
    def _reads_after(spec, order, pos):
        """Path prefixes that a later RUN step reads, or None for all of them."""
        prefixes = set()
        for name in order[pos + 1:]:
            step = spec.by_name(name)
            if step.is_copy:
                continue
            if not step.reads:
                return None
            prefixes.update(step.reads)
        return prefixes or None

    @staticmethod
    def _read_later(spec, order) -> list:
        """For each position, whether a later step depends on this step.

        A layer nothing downstream depends on never has to be unpacked into
        the build context. Skipping it changes no recorded cost: COPY steps
        are charged no time at all, and the layer is pushed on the same rule
        either way.
        """
        wanted = set()
        flags = [False] * len(order)
        for i in range(len(order) - 1, -1, -1):
            flags[i] = order[i] in wanted
            wanted |= set(spec.by_name(order[i]).needs)
        return flags

    def build(self, spec, order, sha: str, session: Session) -> BuildResult:
        spec.validate_order(order)
        read_later = self._read_later(spec, order)
        res = BuildResult(sha=sha, policy=session.name)
        if session.stage is None:
            session.stage = Path(tempfile.mkdtemp(dir=self.workroot, prefix="b"))
        stage = session.stage
        parent = "scratch"
        for pos, name in enumerate(order):
            step = spec.by_name(name)
            if step.is_copy:
                dg, size = self.context_layer(sha, step)
                key = hashlib.sha256(
                    ("|".join([parent, "copy", name, dg])).encode()).hexdigest()
                hit = key in session.cache
                if read_later[pos] and not hit:
                    self.blobs.extract(dg, stage,
                                       self._reads_after(spec, order, pos))
                pushed = 0 if hit else session.registry.push(dg, size)
                res.steps.append(StepRun(name, step.kind, hit, 0.0,
                                         size, pushed, dg))
                session.cache[key] = dg
            else:
                key = hashlib.sha256(
                    ("|".join([parent, "run", name] + list(step.argv))).encode()
                ).hexdigest()
                hit = key in session.cache
                if hit:
                    dg, size = session.cache[key]
                    res.steps.append(StepRun(name, step.kind, True, 0.0, size, 0, dg))
                else:
                    out = stage / step.outdir
                    if out.exists():
                        shutil.rmtree(out, ignore_errors=True)
                    out.mkdir(parents=True, exist_ok=True)
                    argv = [a.replace("{PY}", sys.executable) for a in step.argv]
                    t0 = time.perf_counter()
                    proc = subprocess.run(argv, cwd=stage, env=self.env,
                                          capture_output=True, text=True,
                                          encoding="utf-8", errors="replace")
                    secs = time.perf_counter() - t0
                    if proc.returncode != 0:
                        raise RuntimeError("step " + name + " failed: " +
                                           (proc.stderr or proc.stdout)[-800:])
                    tmp = Path(tempfile.mkdtemp(dir=self.workroot, prefix="lay"))
                    try:
                        blob = tmp / "layer.tgz"
                        size = write_layer(out, blob, normalise=False)
                        dg, _ = self.blobs.put(blob)
                    finally:
                        shutil.rmtree(tmp, ignore_errors=True)
                    pushed = session.registry.push(dg, size)
                    session.cache[key] = (dg, size)
                    res.steps.append(StepRun(name, step.kind, False, secs,
                                             size, pushed, dg))
            parent = key
        return res

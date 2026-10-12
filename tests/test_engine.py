"""The engine's cache and registry behaviour, on a throwaway git repository."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from layerfloor.engine import BlobStore, Builder, Session
from layerfloor.spec import COPY, RUN, BuildSpec, Step


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "app").mkdir(parents=True)
    (r / "doc").mkdir()
    (r / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (r / "doc" / "guide.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(r)], check=True)
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    _git(r, "add", "-A")
    _git(r, "commit", "--quiet", "-m", "first")
    first = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    (r / "doc" / "guide.txt").write_text("hello again\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "--quiet", "-m", "doc only")
    second = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    return r, first, second


def _spec():
    return BuildSpec("t", [
        Step(name="app", kind=COPY, paths=("app/",)),
        Step(name="doc", kind=COPY, paths=("doc/",)),
        Step(name="build", kind=RUN, outdir="out/b", needs=("app",),
             argv=("{PY}", "-c",
                   "import pathlib,time; "
                   "pathlib.Path('out/b/stamp').write_text(str(time.time()))")),
    ])


def _builder(repo, tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    return Builder(repo, tmp_path / "work", blobs)


def test_rebuild_of_same_commit_is_a_full_cache_hit(repo, tmp_path):
    r, first, _ = repo
    b = _builder(r, tmp_path)
    s = Session("t")
    cold = b.build(_spec(), ["app", "doc", "build"], first, s)
    assert cold.misses == 3
    warm = b.build(_spec(), ["app", "doc", "build"], first, s)
    assert warm.misses == 0 and warm.seconds == 0.0 and warm.pushed_bytes == 0


def test_run_step_after_churn_is_invalidated(repo, tmp_path):
    """doc before build: a doc-only commit re-runs build and re-pushes it."""
    r, first, second = repo
    b = _builder(r, tmp_path)
    s = Session("bad")
    b.build(_spec(), ["app", "doc", "build"], first, s)
    after = b.build(_spec(), ["app", "doc", "build"], second, s)
    names = {st.name: st for st in after.steps}
    assert not names["doc"].cached
    assert not names["build"].cached
    assert names["build"].pushed_bytes > 0


def test_run_step_before_churn_is_untouched(repo, tmp_path):
    """build before doc: the same commit leaves the build layer alone."""
    r, first, second = repo
    b = _builder(r, tmp_path)
    s = Session("good")
    b.build(_spec(), ["app", "build", "doc"], first, s)
    after = b.build(_spec(), ["app", "build", "doc"], second, s)
    names = {st.name: st for st in after.steps}
    assert names["build"].cached
    assert not names["doc"].cached
    assert after.pushed_bytes == names["doc"].pushed_bytes


def test_copy_layer_digest_is_content_only(repo, tmp_path):
    """Two commits with identical content for a step share one layer digest."""
    r, first, second = repo
    b = _builder(r, tmp_path)
    spec = _spec()
    a = b.context_layer(first, spec.by_name("app"))
    c = b.context_layer(second, spec.by_name("app"))
    assert a[0] == c[0]
    d = b.context_layer(first, spec.by_name("doc"))
    e = b.context_layer(second, spec.by_name("doc"))
    assert d[0] != e[0]

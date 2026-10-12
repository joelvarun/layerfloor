"""The build spec used by the demo: a Python service image.

Nine layers at the granularity a careful author would write, plus the two
coarse shapes that real Dockerfiles actually use (see policies.py).
"""
from pathlib import Path

from .spec import COPY, RUN, BuildSpec, Step

PY_SERVICE_COPIES = [
    ("manifest", ("pyproject.toml", "uv.lock", "setup.cfg", "setup.py",
                  "requirements/")),
    ("src", ("src/",)),
    ("docs", ("docs/",)),
    ("tests", ("tests/",)),
    ("examples", ("examples/",)),
    ("meta", ("CHANGES.rst", "README.md", "LICENSE.txt", "CONTRIBUTING.rst")),
    ("ci", (".github/", ".devcontainer/", ".editorconfig", ".gitignore",
            ".pre-commit-config.yaml", ".readthedocs.yaml")),
]


def py_service_spec(wheelhouse: Path) -> BuildSpec:
    """Three real RUN steps over seven COPY groups.

    install  reads the dependency manifest
    compile  reads the sources and the installed dependencies
    bundle   reads the documentation tree
    """
    wh = str(Path(wheelhouse).resolve())
    steps = [Step(name=n, kind=COPY, paths=p) for n, p in PY_SERVICE_COPIES]
    steps += [
        Step(name="install", kind=RUN, outdir="out/deps", needs=("manifest",),
             reads=("pyproject.toml", "requirements"),
             argv=("{PY}", "-m", "layerfloor.steps", "deps",
                   "--wheelhouse", wh, "--manifest", "pyproject.toml",
                   "--out", "out/deps")),
        Step(name="compile", kind=RUN, outdir="out/app",
             needs=("src", "install"), reads=("src", "out/deps"),
             argv=("{PY}", "-m", "layerfloor.steps", "compile",
                   "--src", "src", "--deps", "out/deps", "--out", "out/app")),
        Step(name="bundle", kind=RUN, outdir="out/assets", needs=("docs",),
             reads=("docs",),
             argv=("{PY}", "-m", "layerfloor.steps", "bundle",
                   "--src", "docs", "--out", "out/assets", "--label", "docs")),
    ]
    return BuildSpec("py-service", steps)

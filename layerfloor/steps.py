"""Real build step implementations, invoked as subprocesses by the engine.

Each subcommand writes only into its own output directory, which the engine
archives as that step's layer.
"""
import argparse
import compileall
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def _declared_deps(manifest: Path) -> list:
    """Package names a Python manifest declares. Tolerant by design: the
    manifest format changes over a repository's history."""
    if not manifest.exists():
        return []
    text = manifest.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"(?m)^dependencies\s*=\s*\[(.*?)(?=^\])", text,
                      re.S | re.M)
    names = []
    if block:
        # Entries are double quoted. Environment markers embed single quotes
        # ("pkg>=1; python_version < '3.10'"), so only double quotes delimit.
        for raw in re.findall(r'"([^"]+)"', block.group(1)):
            name = re.split(r"[<>=!~;\[\s]", raw.strip(), maxsplit=1)[0]
            if name:
                names.append(name.lower().replace("_", "-"))
    return sorted(set(names))


def cmd_deps(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wheels = sorted(Path(args.wheelhouse).glob("*.whl"))
    declared = _declared_deps(Path(args.manifest))
    if declared:
        want = []
        for w in wheels:
            base = w.name.split("-")[0].lower().replace("_", "-")
            if base in declared:
                want.append(w)
    else:
        want = wheels
    if not want:
        want = wheels
    argv = [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
            "--disable-pip-version-check", "--no-python-version-warning", "-q",
            "--target", str(out)] + [str(w) for w in want]
    return subprocess.run(argv).returncode


def cmd_compile(args) -> int:
    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for root, _dirs, files in os.walk(src):
        for f in files:
            p = Path(root) / f
            rel = p.relative_to(src)
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
    # Record what the application was built against. This is the real data
    # dependency on the install step: the pinned set lands in the layer.
    deps = Path(args.deps)
    pinned = []
    if deps.exists():
        for info in sorted(deps.glob("*.dist-info")):
            meta = info / "METADATA"
            name = info.name[:-len(".dist-info")]
            digest = ""
            if meta.is_file():
                digest = hashlib.sha256(meta.read_bytes()).hexdigest()[:16]
            pinned.append(name + " " + digest)
    (out / "DEPS.txt").write_text(chr(10).join(pinned) + chr(10),
                                  encoding="utf-8")
    ok = compileall.compile_dir(str(out), quiet=2, force=True, workers=1)
    return 0 if ok else 1


def cmd_bundle(args) -> int:
    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / (args.label + ".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED,
                         strict_timestamps=False) as zf:
        for root, _dirs, files in os.walk(src):
            for f in sorted(files):
                p = Path(root) / f
                zf.write(p, arcname=str(p.relative_to(src)).replace(os.sep, "/"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="layerfloor.steps")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deps", help="install declared dependencies offline")
    d.add_argument("--wheelhouse", required=True)
    d.add_argument("--manifest", required=True)
    d.add_argument("--out", required=True)
    d.set_defaults(fn=cmd_deps)

    c = sub.add_parser("compile", help="stage sources and byte-compile them")
    c.add_argument("--src", required=True)
    c.add_argument("--deps", required=True)
    c.add_argument("--out", required=True)
    c.set_defaults(fn=cmd_compile)

    b = sub.add_parser("bundle", help="zip a documentation or asset tree")
    b.add_argument("--src", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--label", default="bundle")
    b.set_defaults(fn=cmd_bundle)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

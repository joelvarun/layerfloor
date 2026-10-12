"""Re-render the comparison chart from a finished run's results.json."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from layerfloor.report import before_after_chart


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    results_path = Path(argv[0] if argv else "docs/results.json")
    out = Path(argv[1]) if len(argv) > 1 else results_path.parent / "before_after.png"
    doc = json.loads(results_path.read_text(encoding="utf-8"))
    before_after_chart(doc["results"], doc["meta"], out)
    print("wrote " + str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

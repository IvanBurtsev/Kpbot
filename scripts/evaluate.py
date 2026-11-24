"""CLI замера качества: python scripts/evaluate.py [--verbose]"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kpbot.config import settings  # noqa: E402
from kpbot.evalkit import evaluate, format_report  # noqa: E402
from kpbot.pipeline import Pipeline  # noqa: E402
from kpbot.store import Store  # noqa: E402


def main(argv: list[str]) -> int:
    verbose = "--verbose" in argv or "-v" in argv

    store = Store(settings.db_path)
    pipeline = Pipeline(store, settings)
    pipeline.ensure_price_list()

    summary = evaluate(pipeline)
    print(format_report(summary, verbose=verbose))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""Run the audited October inputs offline; never authorizes publication."""
import argparse
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.evaluation.october_replay import run_corpus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=ROOT / "evals/october_incidents.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".proof-results/october-incidents")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "execution.log").open("w", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            summary = asyncio.run(run_corpus(args.corpus, args.snapshot, args.output))
    print(json.dumps({k: v for k, v in summary.items() if k not in {"reports", "source_hashes"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Inventory every terminal stage and produce exact, evidence-bound retry plans."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zsxq_pipeline.config import load_pipeline_config  # noqa: E402
from zsxq_pipeline.recovery import inventory  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = inventory(load_pipeline_config(args.config))
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k
                not in {
                    "records",
                    "review_cohorts",
                    "retry_plans",
                    "publication_review",
                }
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

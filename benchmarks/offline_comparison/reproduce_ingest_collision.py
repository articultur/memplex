"""Small real-path diagnostic for the baseline repeated-heading ingest blocker."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.offline_comparison.longmemeval import child_run, source_hash

TEXT = 'Overview.\n\n**Cost:**\n\nOne price.\n\n**Cost:**\n\nAnother price.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    public = {'question_id': 'minimal-repeated-heading', 'query': 'What is the cost?',
              'question_date': '2024/01/02 (Tue) 12:00',
              'sessions': [{'id': 'repeated-heading-session', 'date': '2024/01/01 (Mon) 12:00',
                            'turns': [{'role': 'assistant', 'content': TEXT}]}]}
    report = {'product_source_sha256': source_hash(), 'input': public, 'result': child_run(public, 30)}
    Path(args.out).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['result'], indent=2))
    # Diagnostic only: a fixed product can complete instead of being forced to reproduce a bug.
    return 0 if report['result']['status'] in ('complete', 'error') else 2


if __name__ == '__main__':
    raise SystemExit(main())

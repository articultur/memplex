"""Separate synthetic obsolete-evidence exposure audit; never part of LME score."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.offline_comparison.longmemeval import child_run, digest

CASES = (
    ('city', 'Where do I live now?', 'I live in Berlin.', 'I moved to Vienna. Vienna is my home now.'),
    ('editor', 'Which editor do I use now?', 'I use Neovim for all coding.', 'I switched to Zed for all coding.'),
    ('project', 'What is my current main project?', 'My main project is billing migration.', 'My main project is now observability.'),
    ('commute', 'How do I commute now?', 'I commute by bicycle.', 'I now take the metro to work instead.'),
    ('notes', 'Where do I keep my notes now?', 'I keep my notes in Obsidian.', 'I moved my notes to Notion.'),
)


def public_case(case):
    name, query, old, new = case
    return {'question_id': f'audit-{name}', 'query': query, 'question_date': '2024/03/01 (Fri) 12:00',
            'sessions': [{'id': 'older', 'date': '2024/01/01 (Mon) 12:00',
                          'turns': [{'role': 'user', 'content': old}]},
                         {'id': 'newer', 'date': '2024/02/01 (Thu) 12:00',
                          'turns': [{'role': 'user', 'content': new}]}]}


def run():
    rows = []
    for case in CASES:
        result = child_run(public_case(case), 30)
        row = {'case': case[0], 'result': result}
        if result['status'] == 'complete':
            ranked = result['ranked_session_ids']
            row['old_evidence_present_at_10'] = 'older' in ranked[:10]
            row['new_evidence_present_at_10'] = 'newer' in ranked[:10]
            row['outcome'] = ('both' if 'older' in ranked and 'newer' in ranked else
                              'old_only' if 'older' in ranked else 'new_only' if 'newer' in ranked else 'neither')
        rows.append(row)
    return {'kind': 'synthetic obsolete-evidence exposure audit', 'cases_sha256': digest(CASES),
            'claim': 'Presence of old evidence is not proof of wrong generated answers; no model/maintenance calls',
            'rows': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    result = run()
    Path(args.out).write_text(json.dumps(result, indent=2) + '\n')
    return 0 if all(r['result']['status'] == 'complete' for r in result['rows']) else 2


if __name__ == '__main__':
    raise SystemExit(main())

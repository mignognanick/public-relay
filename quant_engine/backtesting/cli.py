"""Replay a downloaded report and input CSV without credentials or network calls."""
import argparse
import hashlib
import json
from pathlib import Path

from quant_engine.backtesting.data import csv_text, parse_csv
from quant_engine.backtesting.engine import implementation_hash, run_backtest
from quant_engine.backtesting.models import RunRequest


def replay(export, csv):
    trial = export['trial']
    saved = export['result']['payload']['report']
    if trial['implementation_sha256'] != implementation_hash():
        raise ValueError('Code differs from the recorded implementation; check out the original commit')
    normalized = csv_text(parse_csv(csv))
    if hashlib.sha256(normalized.encode()).hexdigest() != trial['dataset']['sha256']:
        raise ValueError('Input CSV hash does not match the recorded dataset')
    result = run_backtest({'csv':normalized,'meta':trial['dataset']},RunRequest.model_validate(trial['request']))
    if result != saved:
        raise ValueError('Recomputed report differs from the saved report')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    parser.add_argument('csv',type=Path)
    args = parser.parse_args()
    try:
        report = replay(json.loads(args.report.read_text()),args.csv.read_text())
    except (ValueError,KeyError) as exc:
        parser.exit(1,f'Replay failed: {exc}\n')
    print(json.dumps({'verified':True,'template':report['template'],'dataset_sha256':report['dataset']['sha256']}))


if __name__ == '__main__':
    main()

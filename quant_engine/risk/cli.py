"""Operator commands for a file-backed risk state.

    python -m quant_engine.risk.cli --state var/risk_state.json status
    python -m quant_engine.risk.cli --state var/risk_state.json engage --reason "..." --actor nick
    python -m quant_engine.risk.cli --state var/risk_state.json disengage --reason "..." --actor nick
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from quant_engine.risk.config import RiskConfig
from quant_engine.risk.engine import RiskEngine
from quant_engine.risk.state import FileStateStore, StateUnavailable


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="quant_engine.risk.cli")
    parser.add_argument("--state", required=True, help="path to the risk state JSON file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    for name in ("engage", "disengage"):
        command = sub.add_parser(name)
        command.add_argument("--reason", required=True)
        command.add_argument("--actor", required=True)
    args = parser.parse_args(argv)

    engine = RiskEngine(RiskConfig.from_env(os.environ), FileStateStore(args.state))
    try:
        match args.command:
            case "status":
                result = engine.status()
            case "engage":
                result = engine.engage_kill_switch(args.reason, args.actor)
            case "disengage":
                result = engine.disengage_kill_switch(args.reason, args.actor)
            case _:
                parser.error(f"unknown command {args.command}")
                return 2
    except (StateUnavailable, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())

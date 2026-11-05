"""Run from the installed package: python examples/learning_cycle.py --db sample.db.

All cases, expert answers and approvals in this script are fictional sample data.
"""
import argparse
import json
from learning_engine import Engine
from learning_engine.demo import run_demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="sample.sqlite3")
    args = parser.parse_args()
    with Engine(args.db) as engine:
        print(json.dumps(run_demo(engine), ensure_ascii=False, indent=2))
        print(json.dumps(engine.metrics(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

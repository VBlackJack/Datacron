# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Grade a JSONL conversation trace against one explicit JSON acceptance case."""

import argparse
import json
from pathlib import Path

from datacron.eval.conversation_trace import ConversationCase, TraceEvent, grade


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    args = parser.parse_args()
    case = ConversationCase.model_validate_json(args.case.read_text(encoding="utf-8"))
    events = [
        TraceEvent.model_validate_json(line)
        for line in args.trace.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result = grade(case, events)
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

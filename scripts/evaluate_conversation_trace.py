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
import sys
from pathlib import Path

from pydantic import ValidationError

from datacron.eval.conversation_trace import ConversationCase, TraceEvent, grade


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    args = parser.parse_args()
    try:
        case_text = args.case.read_text(encoding="utf-8")
        trace_text = args.trace.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[trace] cannot read {exc.filename}: {exc.strerror}", file=sys.stderr)
        raise SystemExit(2) from exc
    try:
        case = ConversationCase.model_validate_json(case_text)
    except ValidationError as exc:
        print(f"[trace] {args.case} is not a conversation case: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # The trace is hand-assembled from a client export, as the documentation
    # tells the operator to do, and TraceEvent forbids extra fields. Any
    # provider metadata an exporter naturally adds - a timestamp, a turn index,
    # a request id - used to end in a pydantic stack trace naming no line, on a
    # file that can hold hundreds.
    events = []
    for number, line in enumerate(trace_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(TraceEvent.model_validate_json(line))
        except ValidationError as exc:
            print(
                f"[trace] {args.trace}:{number} is not a trace event: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
    result = grade(case, events)
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

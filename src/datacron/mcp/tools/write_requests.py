# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Optional durable replay semantics for ordinary note tools."""

from __future__ import annotations

import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec

from datacron.core.hashing import hash_text
from datacron.core.write_request import ACTIVE_WRITE_REQUEST, ReplayedWriteError, WriteRequest
from datacron.mcp.tools.payloads import _error_response

_P = ParamSpec("_P")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def replayable_write(
    function: Callable[_P, Awaitable[dict[str, Any]]],
) -> Callable[_P, Awaitable[dict[str, Any]]]:
    """Bind exact tool arguments to a request key without persisting their contents."""
    signature = inspect.signature(function)

    @wraps(function)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> dict[str, Any]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        request_id = bound.arguments.get("request_id")
        if request_id is None:
            return await function(*args, **kwargs)
        tool = function.__name__.removeprefix("_").removesuffix("_impl")
        if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
            return _error_response(
                tool,
                ValueError("request_id must contain 1-128 ASCII identifier characters"),
                time.perf_counter(),
            )
        parameters = {
            k: v for k, v in bound.arguments.items() if k not in {"app", "actor", "request_id"}
        }
        fingerprint = hash_text(
            json.dumps(
                {"tool": tool, "arguments": parameters},
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        request = WriteRequest(hash_text(request_id), fingerprint)
        token = ACTIVE_WRITE_REQUEST.set(request)
        try:
            try:
                result = await function(*args, **kwargs)
            except ReplayedWriteError as replay:
                # A historical receipt is not a claim about today's note/index.
                result = {
                    "content_hash": replay.record.after_hash,
                    "indexed": False,
                    "replayed": True,
                }
            if request.record is not None:
                target = result.get("error", result)
                target.update(
                    operation_id=request.record.operation_id,
                    committed=True,
                    rel_path=request.record.rel_path,
                )
                target.setdefault("replayed", False)
            return result
        finally:
            ACTIVE_WRITE_REQUEST.reset(token)

    return wrapped

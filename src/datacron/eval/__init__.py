# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Evaluation primitives for Datacron retrieval quality."""

from __future__ import annotations

from datacron.eval.harness import LocalEvalHarness, load_eval_questions
from datacron.eval.metrics import citation_precision, recall_at_k

__all__ = ["LocalEvalHarness", "citation_precision", "load_eval_questions", "recall_at_k"]

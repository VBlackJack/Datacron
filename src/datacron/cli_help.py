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
"""Option help shared by the command groups that mount into one CLI.

``cli.py`` mounts ``cli_library.py`` under ``datacron library``, so both are
rendered by the same ``--help``. The vault sentence was written out in each of
them, byte for byte, which meant one half of a single command's help could
drift away from the other. Neither module can import the other without a
cycle, so the shared text lives here.
"""

from __future__ import annotations

from typing import Final

VAULT_ROOT_HELP: Final[str] = (
    "Vault root. Fallback: DATACRON_VAULT_ROOT, then cwd containing VAULT.yaml under .datacron."
)

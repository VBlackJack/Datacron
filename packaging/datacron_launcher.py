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
"""PyInstaller entry point for the standalone ``datacron`` executable.

The frozen binary bundles the Typer application so a user can run Datacron
(including ``datacron setup``) without a Python install. This launcher simply
delegates to the same :data:`datacron.cli.app` used by the ``datacron`` console
script, keeping a single command surface between the wheel and the executable.
"""

from __future__ import annotations

from datacron.cli import app


def main() -> None:
    """Run the Datacron CLI application."""
    app()


if __name__ == "__main__":
    main()

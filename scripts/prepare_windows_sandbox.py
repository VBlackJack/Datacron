# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Prepare an offline Windows Sandbox validation bundle without launching it."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

_SANDBOX_INPUT = r"C:\DatacronValidation\input"
_SANDBOX_OUTPUT = r"C:\DatacronValidation\results"


def prepare(installer: Path, validator: Path, destination: Path) -> Path:
    """Create a new bundle with read-only inputs and a dedicated writable report share."""
    installer = installer.resolve(strict=True)
    validator = validator.resolve(strict=True)
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    inputs = destination / "input"
    outputs = destination / "results"
    inputs.mkdir()
    outputs.mkdir()
    shutil.copy2(installer, inputs / "Datacron-Setup.exe")
    shutil.copy2(validator, inputs / "datacron-validation.exe")
    checksum = hashlib.sha256(installer.read_bytes()).hexdigest()
    script = "\n".join(
        [
            "#Requires -Version 5.1",
            "# Copyright 2026 Julien Bombled",
            "# Licensed under the Apache License, Version 2.0.",
            "$ErrorActionPreference = 'Stop'",
            "$env:DATACRON_DISPOSABLE_MACHINE = '1'",
            f"& '{_SANDBOX_INPUT}\\datacron-validation.exe' --allow-install "
            f"--installer '{_SANDBOX_INPUT}\\Datacron-Setup.exe' --sha256 {checksum} "
            f"--report '{_SANDBOX_OUTPUT}\\install-evidence.json' "
            f"*> '{_SANDBOX_OUTPUT}\\validation.log'",
            f"$LASTEXITCODE | Set-Content -LiteralPath '{_SANDBOX_OUTPUT}\\exit-code.txt'",
            "exit $LASTEXITCODE",
            "",
        ]
    )
    (inputs / "run.ps1").write_text(script, encoding="utf-8")
    config = ET.Element("Configuration")
    ET.SubElement(config, "Networking").text = "Disable"
    mappings = ET.SubElement(config, "MappedFolders")
    for host, guest, read_only in (
        (inputs, _SANDBOX_INPUT, True),
        (outputs, _SANDBOX_OUTPUT, False),
    ):
        mapping = ET.SubElement(mappings, "MappedFolder")
        ET.SubElement(mapping, "HostFolder").text = str(host)
        ET.SubElement(mapping, "SandboxFolder").text = guest
        ET.SubElement(mapping, "ReadOnly").text = str(read_only).lower()
    command = ET.SubElement(config, "LogonCommand")
    ET.SubElement(
        command, "Command"
    ).text = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{_SANDBOX_INPUT}\\run.ps1"'
    result = destination / "validate.wsb"
    ET.ElementTree(config).write(result, encoding="utf-8", xml_declaration=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--validator", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(prepare(args.installer, args.validator, args.output))


if __name__ == "__main__":
    main()

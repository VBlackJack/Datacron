# Frequently asked questions

**English** | [Français](../fr/faq.md)

These answers describe the current Datacron behavior. For the complete workflows, see the
[installation and configuration guide](setup.md), the
[Windows installer guide](installation-windows.md), and
[operational health and durability](operational-health.md).

## Datacron configured the wrong folder-or my user profile-as the vault. Why, and how do I fix it?

Interactive setup uses the vault path you select; its default is the current directory. Current
releases explain that choice before prompting. Non-interactive `setup --yes` no longer adopts an
arbitrary directory: it requires `--vault`, `DATACRON_VAULT_ROOT`, or an existing
`.datacron/VAULT.yaml` in the current directory. The user profile root itself is always refused,
even when passed explicitly. A dedicated subfolder inside the profile remains valid.

Remove only Datacron's MCP entries for the wrong target, then set up the correct folder:

```bash
datacron unregister --client all --scope both --vault "WRONG_PATH"
datacron setup --client all --scope both --vault "CORRECT_PATH"
```

`unregister` preserves other MCP servers and does not delete notes. If you also installed a
project-scope memory protocol, remove its marked block separately from the workspace where it was
installed:

```bash
datacron protocol uninstall --client all --scope project --project "WORKSPACE_PATH"
```

Do not use `--reset` to move a vault: reset changes only the selected vault's Datacron config and
index, not client registrations.

## Why can't my AI assistant write notes?

By default, AI assistants can read your notes but never change them. To let them create and
update notes, enable **Let my AI assistants write notes** in Datacron Setup. The default
permission covers only three dedicated subfolders: `<vault>/_memory`, `<vault>/_drafts`, and
`<vault>/_journal`. Everything else stays untouched. Certified `--read-only` mode blocks writing
even when this permission is enabled.

You can enable writing later by running setup again, then restarting the AI assistant so it
reloads the configuration:

```bash
datacron setup --yes --vault "VAULT_PATH" --client all --scope both --enable-write
```

Use `--write-path` to replace the three default subfolders with one explicit location; every other
path stays protected. The installer option **Remember this permission for AI assistants installed
later**, or the CLI flag `--machine-wide-write`, reuses the same permission for assistants added
in the future. It never expands the selected writing locations.

## Why can't Antigravity see Datacron?

Datacron detects the live Antigravity profile only at `~/.gemini/antigravity`. For user scope it
merges the `datacron` server into `~/.gemini/config/mcp_config.json`. For project scope it writes
`<vault>/.agents/mcp_config.json`; Antigravity loads that file only when the vault folder is opened
as the IDE workspace. Restart Antigravity after setup so it reloads MCP configuration.

```bash
datacron setup --yes --vault "VAULT_PATH" --client antigravity --scope both
```

The workspace path was validated end to end on 2026-07-19. Datacron writes and unit-tests the
user-scope target, but discovery of that global file by Antigravity still needs validation against
the installed IDE version. If the global route does not load, open the vault as a workspace and
use the validated `.agents/mcp_config.json` route. Stale `antigravity-ide` and
`antigravity-backup` profile folders are intentionally ignored.

## Why can't LM Studio see Datacron?

LM Studio 0.3.17+ has one user MCP configuration at `~/.lmstudio/mcp.json` and no project
configuration. Datacron detects the real `~/.lmstudio` profile directory, then merges only
the `datacron` entry and preserves every other server and setting.

```bash
datacron setup --yes --vault "VAULT_PATH" --client lmstudio --scope user
```

Restart LM Studio after setup so it reloads the file. Do not use `--scope project`: LM Studio
has no project target. Its absence from `datacron protocol install` is also intentional,
because the official documentation defines no global instruction file. The
[README deeplink](../../README.en.md#add-to-lm-studio) is a manual alternative for Python
installations, but its `<YOUR_VAULT>` placeholders must be replaced in the MCP editor.

## Why does the CLI say "Unknown client X" when the documentation lists it?

The documentation may come from a newer repository revision than the executable on your `PATH`.
Check both the version and the executable being launched:

```bash
datacron --version
```

On Windows, `where datacron` or `Get-Command datacron` identifies the executable. Reinstall or
upgrade the current Datacron release-using the latest `Datacron-Setup.exe` or
`python -m pip install --upgrade datacron`-then open a new terminal and restart the AI client.
Running an older installed binary from a fresh source checkout does not add the checkout's newer
client identifiers.

## Is my index up to date?

Writes made through Datacron reconcile the index synchronously. A successful write response with
`indexed: true` is sufficient evidence for that change. Index-backed reads also repair external
file changes periodically, so `get_health` should not be polled after every operation.

Call `get_health` after out-of-band edits, when indexing confirmation is missing, or when search
results look inconsistent. It performs an exact live scan: inspect `consistent_with_vault`,
`stale_entries`, and `hash_divergences`. If the index is inconsistent, stop all writers, make a
verified backup of `.datacron` outside the vault, and run:

```bash
datacron reindex --vault "VAULT_PATH"
```

`reindex` is offline maintenance. It validates and atomically publishes a complete replacement,
and fails closed while live SQLite `-wal` or `-shm` sidecars exist.

## What does `datacron setup --reset` remove and preserve?

Reset deletes exactly two allowlisted targets under the selected vault: `.datacron/VAULT.yaml`
and the complete `.datacron/index/` directory. Setup then recreates the configuration and index
according to the current options. Symlink and reparse-point guards prevent reset from following a
redirected target.

Reset preserves Markdown notes, stable note identities, history, operation journals, audit data,
logs, and the rest of the `.datacron` sidecar. It does not remove MCP client entries, memory
protocol blocks, the installed application, or a user-level write environment setting. Use
`unregister` or `protocol uninstall` for those separate concerns.

## Which switches are available for a silent Windows installation?

`/VAULT` is required in silent mode. The other Datacron switches are opt-in:

```bat
Datacron-Setup.exe /VERYSILENT /VAULT="C:\Users\me\Notes" /INDEX /ENABLEWRITE
```

- `/VAULT="PATH"`: select the vault; required for a silent install.
- `/INDEX`: build the index during installation. Without it, silent setup skips indexing.
- `/RESETCONFIG`: delete the selected vault's Datacron config and generated index before setup.
- `/ENABLEWRITE`: enable write tools with the `_memory`, `_drafts`, and `_journal` allowlist.
- `/MACHINEWIDEWRITE`: also persist that allowlist in the user environment; ignored unless
  `/ENABLEWRITE` is present.

Without `/RESETCONFIG`, existing configuration is kept. Without `/ENABLEWRITE`, the installer
passes no write flag to setup and leaves an existing user write environment unchanged.

## What does the Windows uninstaller remove, and what does it leave behind?

The uninstaller attempts to remove Datacron's MCP entries from user configs and from the project
config under the vault stored by the installer. It removes Datacron-managed user-scope memory
protocol blocks, the application directory and shortcuts, Datacron's user `PATH` entry, and the
installer's registry state. Other MCP servers in shared config files are preserved.

It never deletes the vault, Markdown notes, or the `.datacron` sidecar, including the config,
index, identities, history, operation journal, audit data, and logs. It also leaves an existing
`DATACRON_WRITE_PATHS` user environment value unchanged. Project protocol blocks installed
manually and project registrations for other vaults may remain; remove them with
`datacron protocol uninstall` or `datacron unregister` before uninstalling the executable.

## Where are the logs, and how do I diagnose a problem?

Setup creates `<vault>/.datacron/logs/`, and `datacron status --vault "VAULT_PATH"` reports the
vault, note count, index state, index database, and the expected daily sidecar log path. Start
diagnosis with that command and verify it points to the intended vault.

The active FileLogger location is controlled by `DATACRON_LOG_DIR`; its current default is
`~/.datacron/logs`. If the sidecar log directory is empty, check that environment value and the
default directory. Set `DATACRON_LOG_DIR` to `<vault>/.datacron/logs` in the server environment if
you want runtime logs colocated with the vault. Daily files use the name
`datacron_YYYYMMDD.log`; warnings are also sent to stderr. When reporting a failure, include the
`datacron status` output, Datacron version, client name and scope, and the relevant redacted log
lines.

# Windows installation (installer)

[Français](../fr/installation-windows.md) | **English**

This page covers the graphical `Datacron-Setup.exe` installer: one double-click, no
Python and no terminal, that installs Datacron for your user account and
**automatically registers Datacron with the detected AI clients** and installs its memory
protocol (Claude Desktop, Claude Code, Cursor, Gemini CLI, Codex CLI, Windsurf, VS Code).

If you prefer the command line (`datacron setup`), see the
[installation and setup guide](setup.md) instead.

> Datacron never modifies your notes unless you explicitly enable writing, and never
> sends anything to a cloud service. It only adds a `.datacron/` folder next to your
> notes, registers its MCP server, and only adds a marked instruction block to supported
> global client rules.

## 1. Install

1. Download `Datacron-Setup.exe` from the repository's **Releases** page.
2. Double-click it. The install is **per-user**: no administrator rights, no
   elevation (UAC).
3. **Choose your vault**: the Markdown notes folder you already open in Obsidian.
   Datacron creates a `.datacron/` subfolder there (index, config, audit).
4. Leave **Index now** checked to build the index immediately (recommended), or
   uncheck it to do it later.
5. Finish. The installer adds `datacron.exe` to your **user PATH**, creates the Start
   menu shortcuts, then runs setup: it registers Datacron with each detected AI
   client, installs supported global memory instructions, and indexes the vault. Cursor still
   shows a manual **Settings > Rules** step; Claude Desktop receives the instructions during
   MCP initialization.

After installing, restart Claude Desktop (or your client) so it loads the Datacron
server.

## 2. About the vault

The vault is **your notes folder**: the source of truth. The installer does not touch
it; it only adds `.datacron/`. If you point at a folder that does not exist yet, it is
created.

## 3. Reinstalling (Keep or Reset)

If a Datacron configuration already exists in the chosen vault, the installer offers:

- **Keep** (default): nothing is overwritten; your configuration and index are
  preserved.
- **Reset**: deletes the config (`VAULT.yaml`) and the index, then rebuilds them.
  **Preserved**: your `.md` notes, the stable note identities, the history and the
  audit log.

If you reinstall pointing at a **different vault**, the installer first unregisters the
old vault from your clients to avoid stale entries.

## 4. Start menu shortcuts

- **Datacron Status**: opens a console showing vault, index, and health state.
- **Datacron Setup**: re-runs setup (for example to re-register a client).

## 5. Silent install (deployment)

For scripted deployment, `/VAULT=` is **required** in silent mode:

```bat
Datacron-Setup.exe /VERYSILENT /VAULT="C:\Users\me\Notes"
```

Options:

- `/RESETCONFIG`: reset the config and index (instead of keeping).
- `/INDEX`: index during installation. Without this switch, the index is not built at
  that time.

## 6. Uninstall

Uninstall from **Windows Settings > Apps**. In order, it: removes the `datacron` MCP
entry from your clients, removes Datacron-managed instruction blocks, removes
`datacron.exe` from your user PATH, then removes the program. **Your vault and your notes
are never touched.**

## 7. Verify

- From Claude, ask for a `get_health`, or
- launch **Datacron Status** from the Start menu (or `datacron status --vault
  "<vault>"`).

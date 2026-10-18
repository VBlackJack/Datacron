# Windows installation (installer)

[Français](../fr/installation-windows.md) | **English**

This page covers the graphical `Datacron-Setup.exe` installer: one double-click, no
Python and no terminal, that installs Datacron for your user account and
**automatically registers Datacron with the detected AI clients** (Claude Desktop, Claude
Code, Cursor, Gemini CLI, Antigravity, LM Studio, Codex CLI, Windsurf, VS Code). It also installs the
supported user-scope memory protocol.

If you prefer the command line (`datacron setup`), see the
[installation and setup guide](setup.md) instead. For troubleshooting after installation,
see the [frequently asked questions](faq.md).

> Datacron never modifies your notes unless you explicitly enable writing, and never
> sends anything to a cloud service. It only adds a `.datacron/` folder next to your
> notes, registers its MCP server, and only adds a marked instruction block to supported
> global client rules.

## 1. Install

1. Download `Datacron-Setup.exe` from the repository's **Releases** page.
2. Double-click it. The install is **per-user**: no administrator rights, no
   elevation (UAC).
3. **Choose your vault (Single-Vault or Dual-Vault)**:
   - **Primary documentation vault**: the Markdown notes folder you already open in Obsidian.
   - **Dedicated Memory Vault (optional Dual-Vault mode)**: leave empty to keep everything in one folder, or pick a second folder so Datacron stores `.datacron/` (index, config, audit) and `_memory/` separately while keeping your documentation folder completely untouched.
     On a reinstall the field shows the Memory Vault recorded by the previous install.
     Clearing it is how you return to Single-Vault; otherwise setup keeps the Memory
     Vault your client entries already name.
4. Leave **Index now** checked to build the index immediately (recommended), or
   uncheck it to do it later.
5. **Allow note writing** (optional): by default, your AI assistants can read your notes
   but never change them. On a first install both boxes are unchecked; on a reinstall the
   first box shows the choice the previous install recorded. Check **Let my AI assistants
   write notes (in 3 dedicated subfolders only)** to let them create and update notes
   only in `_memory`, `_drafts`, and `_journal` (in Dual-Vault mode, `_memory` lives in
   your dedicated Memory Vault); everything else stays untouched. Check
   **Remember this permission for AI assistants installed later** to reuse the same
   permission for assistants added in the future. Leaving the first box as you found it
   keeps the current permission of your clients; unchecking a checked box removes it.
   Leaving the second box unchecked does not change a permission that was already
   remembered.
6. Finish. The installer adds `datacron.exe` to your **user PATH**, creates the Start
   menu shortcuts, then runs setup: it registers Datacron with each detected AI
   client, installs supported global memory instructions, and indexes the vault. Cursor still
   shows a manual **Settings > Rules** step. Claude Desktop has no rules file and its chat does
   not present the server instructions to the model: paste the one-line session start
   instruction printed by setup into your Claude preferences (see
   [memory discipline](memory-discipline.md)).

After installing, restart Claude Desktop (or your client) so it loads the Datacron
server.

## 2. About the vault

The vault is **your notes folder**: the source of truth. In Single-Vault mode the installer
only adds `.datacron/`; in Dual-Vault mode `.datacron/` and `_memory/` are placed in the
dedicated Memory Vault so your primary notes folder stays completely untouched. If you point
at a folder that does not exist yet, it is created.

## 3. Reinstalling (Keep or Reset)

If a Datacron configuration already exists in the chosen vault, the installer offers:

- **Keep** (default): nothing is overwritten; your configuration and index are
  preserved, and so are the write permission and the Dual-Vault layout of your client
  entries unless you change them on the other pages.
- **Reset**: deletes the config (`VAULT.yaml`) and the index, then rebuilds them.
  **Preserved**: your `.md` notes, the stable note identities, the history and the
  audit log.

If you reinstall pointing at a **different vault**, the installer first unregisters the
old vault from your clients to avoid stale entries.

## 4. Start menu shortcuts

- **Datacron Companion**: opens the local graphical control panel (`127.0.0.1`) in your browser to manage vault topology, search, graph health, compaction, MCP clients, audit history, and settings.
- **Datacron Status**: opens a console showing vault, index, and health state.
- **Datacron Setup**: re-runs setup (for example to re-register a client).

## 5. Silent install (deployment)

For scripted deployment, `/VAULT=` is **required** in silent mode. Pass
`/SUPPRESSMSGBOXES` with it: without that switch a configuration failure, such as a
missing or empty `/VAULT=`, raises a modal error box that an unattended run cannot
dismiss, and the deployment blocks until the task times out instead of failing:

```bat
Datacron-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /VAULT="C:\Users\me\Notes"
```

Options:

- `/MEMORYVAULT="C:\Users\me\Datacron-Memory"`: optional dedicated Memory Vault root for Dual-Vault mode.
  Without it, the Memory Vault recorded by the previous install is kept.
- `/SINGLEVAULT`: return a Dual-Vault install to Single-Vault. Cannot be combined with
  `/MEMORYVAULT=`.
- `/RESETCONFIG`: reset the config and index (instead of keeping).
- `/INDEX`: index during installation. Without this switch, the index is not built at
  that time.
- `/ENABLEWRITE`: enable the confined write tools (`_memory`, `_drafts`, `_journal`).
- `/DISABLEWRITE`: remove the write allowlist from every client entry. Cannot be
  combined with `/ENABLEWRITE`.
- With neither switch, the installer passes no write choice to setup: each client entry
  keeps the write permission it already has, and an existing `DATACRON_WRITE_PATHS`
  user environment setting remains unchanged.
- `/MACHINEWIDEWRITE`: also apply the write allowlist to the user environment.
  Ignored unless `/ENABLEWRITE` is present.

## 6. Uninstall

Uninstall from **Windows Settings > Apps**. In order, it: removes the `datacron` MCP
entry from your clients, removes Datacron-managed instruction blocks, removes
`datacron.exe` from your user PATH, removes the `DATACRON_WRITE_PATHS` user environment
variable when this installer created it (`/MACHINEWIDEWRITE` or the second write box) and
nobody changed it since, then removes the program. **Your vault and your notes
are never touched.**

## 7. Verify

- From Claude, ask for a `get_health`, or
- launch **Datacron Status** from the Start menu (or `datacron status --vault
  "<vault>"`).

## 8. Windsurf: the global rules limit

Windsurf accepts at most 6000 characters in its global rules file, and the Datacron memory
protocol block alone takes about 5900 of them. If you already have global rules in Windsurf,
the installer registers Datacron but reports that the memory instructions could not be
installed in every client: Windsurf's rules file is left untouched, and the Datacron tools still
work there. The
[FAQ explains what happens and how to free the space](faq.md#why-did-the-memory-protocol-install-fail-for-windsurf).

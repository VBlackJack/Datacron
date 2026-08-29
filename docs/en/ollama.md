---
title: Use Datacron with Ollama
verified: 2026-08-11
tested_on: "Windows 11 / Ollama 0.32.6 / mcpo 0.0.20 / MCP 1.28.1 / Datacron 2026.0721.01"
---

# Use Datacron with Ollama

**English** | [Français](../fr/ollama.md)

> Ollama provides the model and its tool-calling API. A separate client or bridge must
> discover and execute Datacron's MCP tools.

## What is verified

Ollama is not an MCP host. Its API receives function descriptions, returns function calls,
then expects the client to execute those calls and send the results back. Datacron exposes a
local MCP server over the `stdio` transport through the `datacron-mcp` command. A bridge must
therefore connect these two surfaces.

This page keeps the evidence levels separate:

| Path | Status on August 11, 2026 |
|---|---|
| `ollmcp` | Verified against official documentation, but not executed end to end: its `prompt_toolkit` TUI requires a real Windows console screen and does not start in the headless test channel. |
| `mcpo` | The `stdio`-to-OpenAPI transport was locally tested with Datacron: 17 routes were discovered, followed by real `get_health`, `search_text`, and `get_note` calls. |
| Open WebUI with Ollama | Compatibility and configuration are verified against official documentation, but the local Open WebUI installation was not reconfigured during the test. |

## Prepare Datacron in read-only mode

Initialize and index the vault first with the [setup guide](setup.md). In the process that
starts the bridge, set only the vault root and the read allowlist:

```powershell
$env:DATACRON_VAULT_ROOT = "<YOUR_VAULT>"
$env:DATACRON_READ_PATHS = "<YOUR_VAULT>"
Remove-Item Env:DATACRON_WRITE_PATHS -ErrorAction SilentlyContinue
```

Do not set `DATACRON_WRITE_PATHS` for this first connection. The write tools remain
registered, but Datacron refuses them because no write root is configured.

## Option 1 - direct `ollmcp` client

`ollmcp` is an interactive MCP client designed for Ollama. Its documentation advertises
`stdio`, SSE, and Streamable HTTP transports, an `mcpServers` format with command, arguments,
and environment support, and human confirmation for tool calls enabled by default.

Installation documented by the project:

```powershell
uv tool install ollmcp==0.33.2
```

Example standalone configuration that does not write to the global `ollmcp` registry:

```json
{
  "mcpServers": {
    "datacron": {
      "command": "<PATH_TO_DATACRON_MCP>",
      "args": [],
      "env": {
        "DATACRON_VAULT_ROOT": "<YOUR_VAULT>",
        "DATACRON_READ_PATHS": "<YOUR_VAULT>",
        "DATACRON_DURABILITY": "best-effort"
      }
    }
  }
}
```

Documented launch command:

```powershell
ollmcp --servers-json <PATH_TO_SERVERS_JSON> `
  --provider ollama `
  --host http://localhost:11434 `
  --model <TOOL_CAPABLE_MODEL>
```

This command is verified against upstream documentation, but not executed in this page's
headless smoke. An interactive validation must still confirm tool discovery and calls with
the chosen model before this path can be described as locally tested.

## Option 2 - Open WebUI through the `mcpo` bridge

Open WebUI supports Ollama. Its native MCP support accepts Streamable HTTP, not a local
`stdio` server such as Datacron. The Open WebUI project provides `mcpo` to convert a `stdio`
MCP server into an OpenAPI API that the interface can consume.

The following launch was locally tested, with placeholders for machine-dependent values:

```powershell
uvx --with mcp==1.28.1 mcpo==0.0.20 `
  --host 127.0.0.1 `
  --port <LOCAL_PORT> `
  --api-key <TEMP_API_KEY> `
  --strict-auth `
  -- <PATH_TO_DATACRON_MCP>
```

The `mcp==1.28.1` pin is required in the measured state. The unconstrained
`uvx mcpo==0.0.20` launch resolved MCP 2.0.0 and failed before startup: `mcpo` still imports
`streamablehttp_client`, which MCP 2.0.0 removed. Do not remove this pin without repeating
the smoke.

Actual smoke result against a temporary vault containing one note:

```text
OpenAPI routes: 17
get_health: status=healthy, notes_count=1, consistent_with_vault=true
search_text: returned=1, marker=MCPO_DATACRON_SMOKE_20260811
get_note: rel_path=sentinel.md, marker=MCPO_DATACRON_SMOKE_20260811
write_paths_configured=false, effective_writes_enabled=false
```

The proxy was bound to `127.0.0.1`, protected by a temporary key, and stopped after the
test. Next, add its URL as an OpenAPI tool server in Open WebUI. That final step and delivery
of the result by an Ollama model were not locally measured in this lot.

## Rejected option - `mcphost`

`mcphost` is not recommended for a new integration. Its official repository has been
archived since April 13, 2026, is read-only, and states that it will receive no further
updates or bug fixes. Only the two paths above therefore remain in this guide.

## Small-model boundary

Tool-calling support in Ollama does not guarantee that a model will choose the right tool,
produce valid arguments, or complete a sequence of multiple calls. That quality depends on
the model and prompt. BL-0019 is the completed campaign that measured this boundary; BL-0107
tracks the remaining full validation of the compact profile. This page therefore certifies no
particular model.

## References

- [Official Ollama API](https://docs.ollama.com/api/introduction) - local model endpoint.
- [Ollama tool calling](https://docs.ollama.com/capabilities/tool-calling) - client-side execution loop.
- [Official ollmcp repository](https://github.com/jonigl/mcp-client-for-ollama) - installation, configuration, and MCP transports.
- [MCP in Open WebUI](https://docs.openwebui.com/features/extensibility/mcp/) - native Streamable HTTP support and the `mcpo` path for `stdio`.
- [Official mcpo repository](https://github.com/open-webui/mcpo) - MCP-to-OpenAPI bridge and launch options.
- [Official mcphost repository](https://github.com/mark3labs/mcphost) - archived state and end of maintenance.
- Local test: Windows 11, August 11, 2026, versions recorded in the frontmatter.

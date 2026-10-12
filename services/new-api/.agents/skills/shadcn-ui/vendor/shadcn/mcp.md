***REMOVED*** shadcn MCP Server

The CLI includes an MCP server that lets AI assistants search, browse, view, and install components from registries.

---

***REMOVED******REMOVED*** Setup

```bash
shadcn mcp        ***REMOVED*** start the MCP server (stdio)
shadcn mcp init   ***REMOVED*** write config for your editor
```

Editor config files:

| Editor | Config file |
|--------|------------|
| Claude Code | `.mcp.json` |
| Cursor | `.cursor/mcp.json` |
| VS Code | `.vscode/mcp.json` |
| OpenCode | `opencode.json` |
| Codex | `~/.codex/config.toml` (manual) |

---

***REMOVED******REMOVED*** Tools

> **Tip:** MCP tools handle registry operations (search, view, install). For project configuration (aliases, framework, Tailwind version), use `npx shadcn@latest info` — there is no MCP equivalent.

***REMOVED******REMOVED******REMOVED*** `shadcn:get_project_registries`

Returns registry names from `components.json`. Errors if no `components.json` exists.

**Input:** none

***REMOVED******REMOVED******REMOVED*** `shadcn:list_items_in_registries`

Lists all items from one or more registries.

**Input:** `registries` (string[]), `limit` (number, optional), `offset` (number, optional)

***REMOVED******REMOVED******REMOVED*** `shadcn:search_items_in_registries`

Fuzzy search across registries.

**Input:** `registries` (string[]), `query` (string), `limit` (number, optional), `offset` (number, optional)

***REMOVED******REMOVED******REMOVED*** `shadcn:view_items_in_registries`

View item details including full file contents.

**Input:** `items` (string[]) — e.g. `["@shadcn/button", "@shadcn/card"]`

***REMOVED******REMOVED******REMOVED*** `shadcn:get_item_examples_from_registries`

Find usage examples and demos with source code.

**Input:** `registries` (string[]), `query` (string) — e.g. `"accordion-demo"`, `"button example"`

***REMOVED******REMOVED******REMOVED*** `shadcn:get_add_command_for_items`

Returns the CLI install command.

**Input:** `items` (string[]) — e.g. `["@shadcn/button"]`

***REMOVED******REMOVED******REMOVED*** `shadcn:get_audit_checklist`

Returns a checklist for verifying components (imports, deps, lint, TypeScript).

**Input:** none

---

***REMOVED******REMOVED*** Configuring Registries

Registries are set in `components.json`. The `@shadcn` registry is always built-in.

```json
{
  "registries": {
    "@acme": "https://acme.com/r/{name}.json",
    "@private": {
      "url": "https://private.com/r/{name}.json",
      "headers": { "Authorization": "Bearer ${MY_TOKEN}" }
    }
  }
}
```

- Names must start with `@`.
- URLs must contain `{name}`.
- `${VAR}` references are resolved from environment variables.

Community registry index: `https://ui.shadcn.com/r/registries.json`

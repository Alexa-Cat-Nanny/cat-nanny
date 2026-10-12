# Friction log

Record every snag while building, as it happens. Submitting friction logs can add up to 10% to the score.
One entry per problem; keep it factual.

## Template

### <short title>
- **Date / who:**
- **Tool / doc:** (e.g. Alexa+ MCP Toolkit docs, MCP Python SDK 2.2.0, AWS SAM, Bedrock)
- **Task attempted:**
- **Steps taken:**
- **Expected:**
- **Actual:**
- **Severity:** blocker / major / minor
- **Workaround:**
- **Suggestion:**

## Entries

### Alexa+ MCP Toolkit not available to hackathon participants
- **Date / who:** 2026-10-12
- **Tool / doc:** Alexa+ MCP Toolkit, Alexa AI CLI, hackathon FAQ
- **Task attempted:** Connect a self-hosted MCP server to Alexa+ with `alexa-ai new mcp` / `alexa-ai deploy`.
- **Expected:** Install the CLI and deploy a development add-on, as the quickstart describes.
- **Actual:** The CLI is distributed from a gated registry; the toolkit is private preview for select partners. The FAQ confirms participants cannot connect to real Alexa+.
- **Severity:** major
- **Workaround:** Built a web simulator that acts as the MCP client (allowed by the FAQ).
- **Suggestion:** Offer a sandbox/simulator-only tier of the Alexa AI CLI for hackathon participants.

### Docs disagree on where `resourceUri` goes and which spec version is used
- **Date / who:** 2026-10-12
- **Tool / doc:** Alexa+ MCP client lifecycle page vs MCP Apps spec; MCP toolkit overview
- **Actual:** Alexa+ examples put `_meta.ui.resourceUri` in the tool result; the MCP Apps spec puts it in the tool definition. The overview says spec 2025-11-25, while the sample `initialize` uses 2025-03-26.
- **Severity:** minor
- **Workaround:** Send `resourceUri` in both places; the server negotiates any handshake version from 2024-11-05 to 2025-11-25.
- **Suggestion:** Align the examples with the stated spec version and document both placements.

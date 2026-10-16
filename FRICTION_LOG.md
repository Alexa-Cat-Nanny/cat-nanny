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

### MCP SDK HTTP session manager can only be started once (Lambda)
- **Date / who:** 2026-10-13
- **Tool / doc:** MCP Python SDK 2.2.0 `streamable_http_app`, Mangum
- **Task attempted:** Run the stateless Streamable HTTP app on Lambda with Mangum's lifespan handling.
- **Expected:** Build the ASGI app once at import time and reuse it across invocations, as with any ASGI app.
- **Actual:** Second invocation fails: `StreamableHTTPSessionManager .run() can only be called once per instance`.
- **Severity:** major
- **Workaround:** Build the `MCPServer` (tools, clients) once; create a fresh `streamable_http_app` per invocation (cheap).
- **Suggestion:** Allow the session manager to restart, or document the Lambda pattern.

### `ValueError` inside a tool is not a user-visible error
- **Date / who:** 2026-10-13
- **Tool / doc:** MCP Python SDK 2.2.0
- **Actual:** Raising `ValueError` from a tool surfaces as a generic internal error; only `ToolError` becomes an `isError` result with the message the assistant can read out.
- **Severity:** minor
- **Workaround:** Raise `ToolError` for every user-facing problem ("No cat named Tofu is set up. Known cats: ...").
- **Suggestion:** Say so in the tool-authoring docs, since the assistant's wording depends on it.

### Bedrock "Model access" page retired; which model id to use is not obvious
- **Date / who:** 2026-10-14
- **Tool / doc:** Amazon Bedrock console, `aws bedrock list-inference-profiles`
- **Task attempted:** Enable a Claude model and call `converse` from a new account.
- **Actual:** Docs still describe the "Model access" page, which is gone (models come from the model catalog). First calls returned "your account is being verified" for a while. Claude Sonnet 5.5 was not available; the cross-region profile `us.anthropic.claude-sonnet-4-5-20250929-v1:0` worked, and using the bare model id instead of the profile id fails.
- **Severity:** major (for a first-time user)
- **Workaround:** Wait for verification; use `list-inference-profiles` and the `us.` profile id.
- **Suggestion:** Update the getting-started page; show the exact profile id to copy next to each model.

### S3 presigned URLs default to Signature V2 with no region config
- **Date / who:** 2026-10-15
- **Tool / doc:** boto3 `generate_presigned_url`
- **Actual:** Without `Config(signature_version="s3v4")` the URL is SigV2, which buckets created after June 2020 reject.
- **Severity:** minor
- **Workaround:** Force `s3v4` on the client.

### Lambda cannot reference its own API Gateway URL
- **Date / who:** 2026-10-15
- **Tool / doc:** AWS SAM implicit `HttpApi` events
- **Actual:** Putting `!Ref ServerlessHttpApi` in the function's environment creates a circular dependency.
- **Severity:** minor
- **Workaround:** Build phone-facing links from the request's `Host` and `X-Forwarded-Proto` headers.

### Windows: PowerShell 5.1, strftime, and a system proxy
- **Date / who:** 2026-10-13
- **Tool / doc:** PowerShell 5.1, Python 3.14 on Windows, httpx
- **Actual:** `Join-Path` takes only two arguments in 5.1; `%-I` is not a valid strftime code on Windows; a system proxy setting made the simulator's calls to `localhost` fail with `ConnectError`.
- **Severity:** minor, but each one cost a teammate time
- **Workaround:** Nested `Join-Path`; `.strftime('%I').lstrip('0')`; `trust_env=False` for local URLs and `127.0.0.1` instead of `localhost`.

### MCP Apps: no guidance on sizing a view for an Echo Show
- **Date / who:** 2026-10-16
- **Tool / doc:** MCP Apps spec, Alexa+ MCP Apps page
- **Actual:** The host context has `deviceClass`, but there is no documented screen size per class or a way to preview a view at Echo Show sizes.
- **Severity:** minor
- **Workaround:** The view switches to a compact layout when `deviceClass` is `small`; the simulator renders the view at Echo Show proportions.
- **Suggestion:** Publish the viewport sizes per device class and a preview tool.

### First Bedrock call fails until an "Anthropic use case details" form is submitted
- **Date / who:** 2026-10-16
- **Tool / doc:** Amazon Bedrock Converse API
- **Actual:** `ResourceNotFoundException: Model use case details have not been submitted for this account. Fill out the Anthropic use case details form before using the model. If you have already filled out the form, try again in 15 minutes.` Nothing in the getting-started flow mentions the form; the error is also the wrong class (the model exists, access is pending).
- **Severity:** major (blocks the first run)
- **Workaround:** Bedrock console > Model catalog > the Claude model > submit the use case form; wait 15 minutes. The simulator now shows Bedrock's own message on the page instead of a generic 500.
- **Suggestion:** Surface the form during model enablement and return an access-denied class error.

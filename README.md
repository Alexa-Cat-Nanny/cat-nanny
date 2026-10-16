# Cat Nanny

An Alexa+ add-on for families who share the care of a cat. One sentence to Alexa ("Mochi just threw up")
becomes a structured observation: Alexa asks the follow-up questions a vet would, warns when something
needs a vet today, keeps photos with the observation, and shows a seven-day summary on the Echo Show screen.

Built for the Build, Ship, Shape: Amazon Developer Hackathon, **Alexa+ track**, as a self-hosted
**MCP server** (spec 2025-11-25, Streamable HTTP, MCP Apps), with the **AWS Builder** and
**Open Source** mini challenges.

- Demo video: _link_
- Rules package (open source, no I/O): [cat-health-signals](https://github.com/Alexa-Cat-Nanny/cat-health-signals)
- Where it came from: the conversation design, follow-up questions, red-flag rules, correction flow and web UI
  were first built as a FastAPI + SQLite app in
  [CatNannyFramework](https://github.com/Alexa-Cat-Nanny/CatNannyFramework); this repo is that product rebuilt
  as an MCP server on AWS.

> Cat Nanny records observations for a vet. It does not diagnose and does not replace veterinary advice.

## How it works

```
 voice ─▶ Alexa+ (here: the web simulator) ──MCP / Streamable HTTP──▶ API Gateway ─▶ Lambda: Cat Nanny MCP server
           the LLM (Amazon Bedrock) picks a tool,                                      ├─ cat-health-signals  rules
           fills its arguments, speaks the result,                                     ├─ DynamoDB            records
           renders the ui:// view on the screen                                        └─ S3                  photos
                                                                                      phone ──presigned PUT──▶ S3
```

1. The assistant reads the server's tool list once: names, descriptions, JSON schemas, output schemas.
2. When someone speaks, the assistant's LLM chooses a tool and extracts its arguments from the sentence.
   Only details the person actually said are passed; the schema rejects anything else.
3. The server stores the observation, runs the deterministic rules from `cat-health-signals`, and returns
   structured results plus `assistant_guidance`: what to say first (a red flag), which question to ask next,
   word for word.
4. Follow-up answers update the **same** observation (`event_id`); "not sure" is recorded and never asked again.
5. "Actually that was Jeremy" moves the observation, with its photos, to the other cat and keeps an audit record;
   if the cat is new, the assistant asks before creating a profile.
6. A photo is attached from the phone: the assistant shows a link, the page uploads straight to S3 with a
   presigned URL, nothing is texted or emailed.
7. `get_vet_summary` returns an MCP Apps view (`ui://`) that the Echo Show renders in a sandboxed iframe.

Hackathon participants cannot connect a self-hosted server to real Alexa+ (the Alexa+ MCP Toolkit is in
private preview). Per the hackathon FAQ, this repo includes a **web simulator** that plays Alexa+: it is an MCP
client that connects exactly as Alexa+ does (initialize handshake, Streamable HTTP, bearer auth, MCP Apps
capability), uses a model on **Amazon Bedrock** for tool selection, takes voice input, reads replies aloud, and
renders MCP Apps views on a simulated Echo Show. The page is the CatNannyFramework simulator with its data
backend replaced by MCP calls; an "MCP calls this turn" panel shows every tool call and result.

## MCP tools

| Tool | When the assistant calls it |
|---|---|
| `list_household` | Which cats and caregivers exist; which observation types are supported |
| `add_cat` | "My cat is Mochi" (idempotent by name) |
| `log_observation` | Vomiting or appetite changes; again with `event_id` to add a follow-up answer, `unknown_fields` for "not sure" |
| `correct_cat` | "No, that was Jeremy" (asks first when the cat is unknown) |
| `get_care_log` | "How has Mochi been this week?" |
| `get_photo_upload_link` | "Send me the photo link" (shown on screen) |
| `get_vet_summary` | "Show Mochi's summary for the vet" (MCP Apps view) |

Every tool declares `outputSchema`, returns `structuredContent`, carries annotations (`readOnlyHint`,
`idempotentHint`, ...), and ends with `assistant_guidance`. User-facing errors are `isError` results with a
sentence the assistant can say ("No cat named Tofu is set up. Known cats: Jeremy, Mochi.").

Observation types are deliberately limited to **vomit** and **appetite**, each with a strict detail schema
(`cat-health-signals/schemas.py`). Red flags: blood in vomit, vomiting with low energy, three vomits in
24 hours, not eating for 24 hours, a male cat not urinating.

## Repository layout

```
server/cat_nanny/     MCP server: tools.py (7 tools), store.py (DynamoDB single table + in-memory twin),
                      photos.py (S3 presigned / local), app.py (HTTP, auth, upload page), handler.py (Lambda), ui/
simulator/sim/        Web Alexa+ simulator: mcp_bridge.py (MCP client), agent.py (Bedrock Converse tool loop),
                      app.py (page + panels), static/ (voice UI, MCP Apps host)
infra/template.yaml   AWS SAM: HTTP API, Lambda, DynamoDB, S3, CloudWatch log group, cost budget
scripts/              build_lambda.py, deploy.sh, dev.ps1 (Windows), seed.py
docs/                 aws-integration.md (AWS Builder write-up), demo-script.md, zh/infra-notes.md (team notes, Chinese)
```

## Run locally (no AWS account needed)

```bash
git clone https://github.com/Alexa-Cat-Nanny/cat-health-signals
git clone https://github.com/Alexa-Cat-Nanny/cat-nanny && cd cat-nanny
cp .env.example .env
make setup
make server   # terminal 1: http://127.0.0.1:8000/mcp with the demo household (Mochi, Jeremy)
make sim      # terminal 2: open http://127.0.0.1:8080 (Chrome or Edge for voice)
make test     # 55 tests: tools, HTTP + Lambda handler, DynamoDB and S3 (moto), simulator end to end
```

On Windows (PowerShell) use `.\scripts\dev.ps1 setup|test|server|sim|build|deploy|seed|logs` instead of `make`.

Without `BEDROCK_MODEL_ID` the simulator runs in offline mode and understands a few fixed phrases
("Mochi just threw up", "it was food", "not sure", "no actually it is for Tofu", "send me the photo link",
"show the summary for the vet"). To use a real model, set `BEDROCK_MODEL_ID` (for example
`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) and `AWS_REGION` in `.env`; your AWS credentials need
`bedrock:InvokeModel`.

To try a photo from a real phone on the same Wi-Fi, set `PUBLIC_BASE_URL=http://<your laptop IP>:8000` so the
link Alexa shows is reachable from the phone.

You can also inspect the server with the MCP Inspector: `npx @modelcontextprotocol/inspector`, transport
Streamable HTTP, URL `http://127.0.0.1:8000/mcp`, header `Authorization: Bearer <MCP_AUTH_TOKEN>`.

## Deploy to AWS

Needs the AWS CLI (`aws configure`), the AWS SAM CLI and Python 3.13.

```bash
# in .env: AWS_REGION, BUDGET_EMAIL, MCP_AUTH_TOKEN (openssl rand -hex 24)
make deploy                      # packages .build/lambda, runs sam deploy, prints the stack outputs
TABLE_NAME=<output> make seed    # optional: the demo household
```

Then point the simulator at the `McpUrl` output (`MCP_URL=...` in `.env`), set `BEDROCK_MODEL_ID`, and run
`make sim`. Tool calls are logged as one JSON line each: `make logs`. Details, costs and the decisions behind
each service are in [docs/aws-integration.md](docs/aws-integration.md).

## AWS services

| Service | Use |
|---|---|
| AWS Lambda + API Gateway HTTP API | Hosts the MCP server (stateless Streamable HTTP, JSON responses, Mangum) |
| Amazon DynamoDB | One table: cats, caregivers, observations, photos, corrections |
| Amazon S3 | Photos; phones upload with presigned PUT URLs, pages view with presigned GET URLs |
| Amazon Bedrock | The simulator's LLM (Converse API with tool use) |
| Amazon CloudWatch Logs | One JSON line per tool call |
| AWS Budgets | Monthly cost warning |

## License

Apache-2.0

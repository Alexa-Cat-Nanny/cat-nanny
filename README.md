# Cat Nanny

An Alexa+ add-on that turns one-sentence voice notes about your cat ("Alexa, Mochi just threw up")
into a shared family health log, follow-up questions, red-flag warnings and a timeline your vet can use.

Built for the Build, Ship, Shape: Amazon Developer Hackathon, **Alexa+ track**, as a self-hosted
**MCP server** (spec 2025-11-25, Streamable HTTP), with the AWS Builder and Open Source mini challenges.

- Demo video: _link_
- Rules engine (open source): [cat-health-signals](https://github.com/Alexa-Cat-Nanny/cat-health-signals)

> Cat Nanny records observations for a vet. It does not diagnose and does not replace veterinary advice.

## How it works

```
 voice ─▶ Alexa+ (or the web simulator) ──Streamable HTTP──▶ API Gateway ─▶ Lambda: Cat Nanny MCP server
           LLM picks a tool, fills its                                         ├─ cat-health-signals (rules)
           arguments, speaks the result,                                       ├─ DynamoDB (cats, events, alerts)
           renders the ui:// view on screen                                    └─ SNS email (family alerts)
```

1. The assistant reads the server's tool list (names, descriptions, JSON schemas) once.
2. When someone speaks, the assistant's LLM chooses a tool and extracts its arguments from the sentence.
3. The server stores the event, runs the deterministic rules from `cat-health-signals`, and returns
   structured results plus `assistant_guidance` (what to say first, which question to ask next).
4. Red flags at the `emergency` or `vet_today` level email the family and stay open as alerts; every
   later tool result repeats them until someone acknowledges them, so the next person who talks to
   Alexa hears about it.
5. `get_vet_summary` returns an MCP Apps view (`ui://`) that renders on an Echo Show-sized screen.

Hackathon participants cannot connect a self-hosted server to real Alexa+ (the Alexa+ MCP Toolkit is
in private preview). Per the hackathon FAQ, this repo includes a **web simulator** that plays Alexa+:
it is an MCP client that connects exactly as Alexa+ does (initialize handshake, Streamable HTTP,
bearer auth, MCP Apps), uses an LLM on **Amazon Bedrock** for tool selection, takes voice input and
reads replies aloud, and renders MCP Apps views in a sandboxed iframe.

## MCP tools

| Tool | When the assistant calls it |
|---|---|
| `add_cat`, `update_cat` | Setting up or changing a cat's profile |
| `log_health_event` | Vomiting, appetite, litter box or energy changes; again with `event_id` to add follow-up answers |
| `log_care_event` | Meals, medication, weight, food changes, vet visits |
| `get_today` | "Did anyone feed Mochi?" "Did she get her medicine?" |
| `get_patterns` | "Why does she keep throwing up?" |
| `get_vet_summary` | "Show Mochi's summary for the vet" (MCP Apps view) |
| `acknowledge_alerts` | "We called the vet" (stops repeating a family alert) |

All tools declare `outputSchema`, return `structuredContent`, and carry annotations (`readOnlyHint`, etc.).

## Repository layout

```
server/cat_nanny/    MCP server: app.py (HTTP + auth), tools.py, store.py, notify.py, ui/, handler.py (Lambda)
simulator/sim/       Web Alexa+ simulator: MCP client, Bedrock agent, voice UI, MCP Apps host
infra/template.yaml  AWS SAM: HTTP API, Lambda, DynamoDB, SNS, log group, budget alarm
scripts/             build_lambda.sh, deploy.sh, seed.py
```

## Run locally (no AWS account needed)

```bash
git clone https://github.com/Alexa-Cat-Nanny/cat-health-signals ../cat-health-signals
git clone https://github.com/Alexa-Cat-Nanny/cat-nanny && cd cat-nanny
cp .env.example .env
make setup
make server   # terminal 1: http://localhost:8000/mcp with sample data for Mochi
make sim      # terminal 2: open http://localhost:8080 (Chrome or Edge for voice)
make test
```

On Windows (PowerShell) use `.\scripts\dev.ps1 setup|test|server|sim` instead of `make`.
Deploying uses bash scripts, so run `make deploy` from WSL or Git Bash.

Without `BEDROCK_MODEL_ID` the simulator runs in offline mode and understands a few fixed phrases
("Mochi just threw up", "did anyone feed Mochi today?", "show Mochi's summary for the vet").
To use a real model, enable a model in the Bedrock console, set `BEDROCK_MODEL_ID` and `AWS_REGION`,
and make sure your AWS credentials allow `bedrock:InvokeModel`.

You can also inspect the server with the MCP Inspector:
`npx @modelcontextprotocol/inspector`, transport Streamable HTTP, URL `http://localhost:8000/mcp`,
header `Authorization: Bearer <MCP_AUTH_TOKEN>`.

## Deploy to AWS

Needs the AWS CLI, the AWS SAM CLI and Python 3.13.

```bash
# in .env: AWS_REGION, ALERT_EMAIL, MCP_AUTH_TOKEN
make deploy                      # builds .build/lambda and runs sam deploy
TABLE_NAME=<output> make seed    # optional: sample history
```

Confirm the SNS subscription email. Then point the simulator at the `McpUrl` output
(`MCP_URL=...` in `.env`) and run `make sim`. Tool calls are logged as JSON lines: `make logs`.

## AWS services

| Service | Use |
|---|---|
| AWS Lambda + API Gateway HTTP API | Hosts the MCP server (stateless Streamable HTTP, JSON responses) |
| Amazon DynamoDB | Single table for cat profiles, events and family alerts |
| Amazon SNS | Emails the family when a red flag fires |
| Amazon Bedrock | The simulator's LLM (Converse API with tool use) |
| Amazon CloudWatch Logs | One JSON line per tool call |
| AWS Budgets | Cost alarm |

## License

Apache-2.0

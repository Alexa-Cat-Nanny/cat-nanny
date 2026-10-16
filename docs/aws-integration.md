# AWS integration (AWS Builder mini challenge)

What Cat Nanny uses on AWS, why each service was chosen, how it is wired, and what it costs.
Everything here is created by `infra/template.yaml` (AWS SAM) except Bedrock model access, which is enabled
per account in the console.

## Architecture

```
                 ┌──────────────────────────── simulator (laptop) ───────────────────────────┐
 voice/text ───▶ │ MCP client (mcp 2.2.0)  ◀──▶  Amazon Bedrock  Converse API, tool use      │
                 └──────────────┬─────────────────────────────────────────────────────────────┘
                                │ MCP over Streamable HTTP  (POST /mcp, Bearer token, JSON responses)
                                ▼
                 API Gateway HTTP API  ──▶  AWS Lambda  cat-nanny-mcp  (python3.13, Mangum)
                                               │  tools.py  ──▶  cat-health-signals (rules, pure Python)
                                               ├─▶ Amazon DynamoDB   one table (pk/sk)
                                               ├─▶ Amazon S3         presigned PUT / GET URLs for photos
                                               └─▶ CloudWatch Logs   one JSON line per tool call
 phone ──────────── presigned PUT ───────────────▶ Amazon S3 bucket
                                               AWS Budgets: email at 80 % of $10 / month
```

## Services and decisions

### AWS Lambda + API Gateway HTTP API: hosting the MCP server

- **What:** one function, `cat_nanny.handler.handler`, behind an HTTP API route `ANY /{proxy+}`.
  [Mangum](https://mangum.io) adapts API Gateway events to the ASGI app the MCP SDK produces.
- **Why Lambda:** an Alexa+ add-on is idle most of the day and bursts when someone talks; a function that costs
  nothing between calls fits. The MCP server runs in *stateless* mode with *JSON responses* (no SSE stream,
  no session id), which is exactly what Lambda needs and matches how Alexa+ calls servers. All state is in
  DynamoDB, so any invocation can serve any request.
- **Gotcha solved:** the MCP SDK's HTTP session manager may only be started once per process. The server
  (tools, schemas, AWS clients) is built once per Lambda container; each invocation wraps it in a fresh, cheap
  ASGI app (`handler.py`).
- **Links for phones:** the Lambda cannot know its own API Gateway URL from the template (circular reference),
  so links handed to phones (upload page, photo files) are built on the host each request came in on
  (`Host` / `X-Forwarded-Proto`), unless `PUBLIC_BASE_URL` overrides it.
- **Security:** `/mcp` requires `Authorization: Bearer <MCP_AUTH_TOKEN>` (401 without `WWW-Authenticate`, as
  the Alexa+ docs specify) and rejects browser requests whose `Origin` is not in `ALLOWED_ORIGINS` (403).
  Timeout 29 s, memory 512 MB.

### Amazon DynamoDB: records

- **What:** one on-demand table (`pk`, `sk`) holding cats, caregivers, observations, photos and correction
  audit records (`server/cat_nanny/store.py`):

  | item         | pk                         | sk                |
  |--------------|----------------------------|-------------------|
  | cat          | `H#<household>`            | `CAT#<cat_id>`    |
  | caregiver    | `H#<household>`            | `CG#<id>`         |
  | observation  | `H#<household>#C#<cat_id>` | `EV#<event_id>`   |
  | event index  | `H#<household>#E#<event>`  | `META`            |
  | photo        | `H#<household>#E#<event>`  | `PH#<photo_id>`   |
  | correction   | `H#<household>#E#<event>`  | `RA#<id>`         |

- **Why DynamoDB:** the access patterns are few and fixed: "this cat's observations in a time range",
  "this observation by id", "this observation's photos". Event ids start with a UTC timestamp, so a time range
  is a single `between` query on the sort key, and the single-table layout needs no indexes. On-demand billing
  is free at this volume.
- **Correction = move:** "that was Jeremy, not Mochi" changes the observation's partition key, so it is a put
  under the new cat, an index update, a delete under the old cat, and the photos are re-put under the new
  cat id. The observation keeps its id, details, time and reporter; a `RA#` record keeps who corrected what and why.
- **Numbers:** DynamoDB returns `Decimal`; the store converts whole numbers back to `int` so the strict detail
  schemas (`episode_count` must be an integer) still validate.
- **Tested** against [moto](https://github.com/getmoto/moto) with the same key schema as the template.

### Amazon S3: photos

- **What:** a private bucket (public access blocked, SSE-S3). Keys:
  `households/<h>/events/<event_id>/<photo_id>.<ext>`.
- **Why presigned URLs:** API Gateway + Lambda limit a request body to 6 MB and would cost a Lambda invocation
  per megabyte. Instead the upload page asks the server for an *upload target* (a presigned `PUT`, 1 hour), the
  phone uploads straight to S3, then tells the server `complete`; the server checks the object exists
  (`HeadObject`) before recording the photo. Photos are viewed with presigned `GET` URLs, so the bucket is never
  public. Signature V4 is forced on the client, because buckets created after 2020 reject V2 presigned URLs.
- **CORS** allows `PUT`/`GET` from any origin because the upload page runs on the API's domain and the simulator
  on localhost; the URL itself is the credential.
- **IAM:** the Lambda may `GetObject`/`PutObject` under `households/*` only.
- **Locally** the same interface is served by `LocalPhotoStorage` (files under `uploads/`), so the flow is
  identical without AWS.

### Amazon Bedrock: the assistant's model

- **What:** the simulator calls the Converse API with the MCP tool list converted to `toolSpec`s, loops on
  `stopReason == "tool_use"`, executes each tool through the MCP client, and feeds `toolResult`s back.
  Model: `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (a cross-region inference profile).
- **Why here, not in the Lambda:** on real Alexa+ the model is Alexa's. The server must not depend on a model;
  the simulator stands in for Alexa+, so that is where Bedrock belongs. The server is pure tools + rules.
- **Setup notes:** the "Model access" page has been retired; models are enabled from the Bedrock model catalog.
  New accounts are "being verified" for a while before the first `converse` call succeeds; Claude Sonnet 5.5 was
  not available in the account used, Sonnet 4.5 was. IAM: `bedrock:InvokeModel` on the profile and the
  underlying regional model ARNs.

### Amazon CloudWatch Logs and AWS Budgets

- A log group with 14-day retention; every tool call logs one JSON line
  `{"event":"tool_call","tool":"log_observation","args":{...},"ok":true,"ms":12.3}`, so `make logs` shows the
  conversation from the server's point of view.
- A monthly cost budget of $10 emails at 80 %.

## Deploy

```bash
aws configure                       # credentials stay on your machine; never in .env or git
cp .env.example .env                # AWS_REGION, BUDGET_EMAIL, MCP_AUTH_TOKEN, BEDROCK_MODEL_ID
make deploy                         # or .\scripts\dev.ps1 deploy on Windows
TABLE_NAME=<TableName output> make seed
MCP_URL=<McpUrl output> make sim
```

`scripts/build_lambda.py` installs the server's dependencies as Linux x86_64 wheels (no Docker) plus
`cat-health-signals`, and `sam deploy --resolve-s3` uploads the package. Redeploys are idempotent.

## Cost

At hackathon volumes everything but Bedrock sits in the free tier: Lambda (1M requests/month free),
HTTP API (1M calls free for 12 months), DynamoDB on-demand (25 GB and 200M requests free), S3 (5 GB free),
CloudWatch (5 GB ingestion free). Bedrock: a demo turn is roughly 3–6 k input tokens (tool schemas + history)
and ~100 output tokens, about $0.01–0.02 per turn with Sonnet 4.5. The budget alarm is the safety net.

## Tear down

```bash
aws s3 rm s3://<PhotoBucketName> --recursive        # the bucket must be empty first
sam delete --stack-name cat-nanny --region us-east-1
```

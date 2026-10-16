#!/usr/bin/env bash
# Build and deploy the MCP server stack (Lambda + HTTP API + DynamoDB + S3 + budget).
# Needs: AWS CLI credentials (aws configure), AWS SAM CLI, and in .env: AWS_REGION, BUDGET_EMAIL, MCP_AUTH_TOKEN.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[ -f .env ] && set -a && . ./.env && set +a

: "${AWS_REGION:?set AWS_REGION in .env}"
: "${BUDGET_EMAIL:?set BUDGET_EMAIL in .env}"
: "${MCP_AUTH_TOKEN:?set MCP_AUTH_TOKEN in .env (openssl rand -hex 24)}"
STACK="${STACK_NAME:-cat-nanny}"

./scripts/build_lambda.sh

sam deploy \
  --template-file infra/template.yaml \
  --stack-name "$STACK" \
  --region "$AWS_REGION" \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "BudgetEmail=$BUDGET_EMAIL" \
    "McpAuthToken=$MCP_AUTH_TOKEN" \
    "HouseholdId=${HOUSEHOLD_ID:-demo}" \
    "HouseholdTz=${HOUSEHOLD_TZ:-America/Los_Angeles}" \
    "AllowedOrigins=${ALLOWED_ORIGINS:-http://127.0.0.1:8080,http://localhost:8080}"

aws cloudformation describe-stacks --stack-name "$STACK" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table

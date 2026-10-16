-include .env
export

PY ?= python3

.PHONY: setup server sim test build deploy seed logs

setup:            ## install everything for local development
	$(PY) -m pip install -e ../cat-health-signals -r requirements-dev.txt

server:           ## MCP server on http://127.0.0.1:8000/mcp (in-memory, demo household)
	cd server && $(PY) -m uvicorn cat_nanny.app:app --host 127.0.0.1 --port 8000 --reload

sim:              ## Alexa+ simulator on http://127.0.0.1:8080
	cd simulator && $(PY) -m uvicorn sim.app:app --host 127.0.0.1 --port 8080 --reload

test:             ## all tests (server tools, HTTP + Lambda handler, DynamoDB, simulator)
	$(PY) -m pytest

build:            ## package the Lambda into .build/lambda
	./scripts/build_lambda.sh

deploy:           ## build + sam deploy (needs AWS_REGION, BUDGET_EMAIL, MCP_AUTH_TOKEN)
	./scripts/deploy.sh

seed:             ## load the demo household into the deployed table (needs TABLE_NAME, AWS_REGION)
	$(PY) scripts/seed.py

logs:             ## tail the Lambda's tool-call logs
	aws logs tail /aws/lambda/$${STACK_NAME:-cat-nanny}-mcp --follow --region $(AWS_REGION)

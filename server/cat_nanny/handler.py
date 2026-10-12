"""AWS Lambda entry point (API Gateway HTTP API -> ASGI app).

The MCP server (tools, DynamoDB and SNS clients) is built once per Lambda
container. The MCP SDK's HTTP session manager can only be started once, so each
invocation wraps the server in a fresh, cheap HTTP app and Mangum runs its
startup/shutdown around that single stateless request.
"""

from mangum import Mangum

from .app import asgi_app, create_mcp

_mcp, _settings = create_mcp()


def handler(event, context):
    return Mangum(asgi_app(_mcp, _settings), lifespan="auto")(event, context)

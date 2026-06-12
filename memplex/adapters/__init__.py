"""Memplex Agent Adapters -- protocol-specific interfaces.

Provides adapters for different consumption patterns:

- CLI:     command-line interface (``memplex`` command)
- HTTP:    REST API via FastAPI (``create_app`` factory)
- MCP:     Model Context Protocol server over stdio JSON-RPC
"""

from memplex.adapters.cli import main as cli_main
from memplex.adapters.http_api import create_app
from memplex.adapters.mcp_server import MCPServer

__all__ = ["cli_main", "create_app", "MCPServer"]

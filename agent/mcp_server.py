"""MCP server exposing the shared tool layer -- see
docs/agent_ui_architecture_plan.md.

Thin adapter only: registers the same functions the web app's agent loop
will call directly, so tool logic is written once (the shared-tool-layer
pattern from the workforce-data-explorer reference in references/). Starts
with everything exposed, including run_sql -- confirmed reversible later by
trimming which tools get registered here, with no effect on the web app's
own use of the same underlying functions.

Run directly (`python -m agent.mcp_server`) once the tools in agent/tools
are actually implemented -- right now every call will raise
NotImplementedError.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from agent.tools.registry import TOOL_REGISTRY

mcp = MCPServer("epl-fantasy")

for tool in TOOL_REGISTRY:
    mcp.add_tool(tool.fn, name=tool.name, description=tool.description)


if __name__ == "__main__":
    mcp.run()

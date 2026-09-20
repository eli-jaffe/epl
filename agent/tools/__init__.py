"""The shared tool layer -- see docs/agent_ui_architecture_plan.md.

Plain Python functions, written once, called directly by the (future) web
app agent loop and wrapped separately for MCP transport in
agent/mcp_server.py. Import TOOL_REGISTRY from agent.tools.registry.
"""

"""示例 extension：提供演示工具与资源。"""

import json

from mcp.server.fastmcp import FastMCP


def register(server: FastMCP) -> None:
    """注册 extension 的工具和资源。"""

    @server.tool(name="extension_demo_ping")
    async def extension_demo_ping(message: str = "hello") -> str:
        return json.dumps(
            {
                "status": "ok",
                "extension": "demo",
                "echo": message,
            },
            ensure_ascii=False,
            indent=2,
        )

    @server.resource("g1://extensions/demo/info")
    async def extension_demo_info() -> str:
        return json.dumps(
            {
                "id": "demo_extension",
                "description": "用于验证 extension 加载机制的示例扩展",
                "tools": ["extension_demo_ping"],
            },
            ensure_ascii=False,
            indent=2,
        )

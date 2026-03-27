"""
launcher.py
Manastone 多 MCP Server 启动器

读取 config/servers.yaml，根据 enabled 字段启动选中的 MCP Server。
每个 server 监听独立端口，共享同一个 AppState 实例（在同进程内）。

用法：
  manastone-launcher                   # 使用默认 servers.yaml
  manastone-launcher --config path/to/servers.yaml
  manastone-launcher --list            # 列出所有可用 server
  manastone-launcher --enable joints,power,core   # 临时覆盖启用列表
  manastone-launcher --mock            # 强制 mock 模式
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-20s] %(levelname)s %(message)s",
)
logger = logging.getLogger("manastone.launcher")


# ── Server 注册表 ─────────────────────────────────────────────
# 每个 id 对应一个 create_server 工厂函数
SERVER_REGISTRY = {
    "joints":    "manastone_diag.servers.joints",
    "power":     "manastone_diag.servers.power",
    "imu":       "manastone_diag.servers.imu",
    "hand":      "manastone_diag.servers.hand",
    "vision":    "manastone_diag.servers.vision",
    "motion":    "manastone_diag.servers.motion",
    "pid_tuner": "manastone_diag.servers.pid_tuner",
    "core":      "manastone_diag.servers.core",
}


@dataclass
class ServerConfig:
    id: str
    name: str
    enabled: bool
    port: int
    description: str
    required: bool


def load_servers_config(config_path: Path) -> list[ServerConfig]:
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    servers = []
    for s in raw.get("servers", []):
        servers.append(ServerConfig(
            id=s["id"],
            name=s["name"],
            enabled=s.get("enabled", False),
            port=s.get("port", 8080),
            description=s.get("description", ""),
            required=s.get("required", False),
        ))
    return servers


def get_init_kwargs(config_path: Path, mock_mode: bool) -> dict:
    """构造传递给 init_shared_state 的参数"""
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    g = raw.get("global", {})

    robot_id = os.getenv(g.get("robot_id_env", "MANASTONE_ROBOT_ID"), "robot_01")
    schema_rel = g.get("schema_path", "config/robot_schema.yaml")
    storage_rel = g.get("storage_dir", "storage")
    mock_env_var = g.get("mock_mode_env", "MANASTONE_MOCK_MODE")

    if not mock_mode:
        mock_mode = os.getenv(mock_env_var, "false").lower() == "true"

    # 路径相对于 config 文件的父目录
    base = config_path.parent
    schema_path = (base / schema_rel) if not Path(schema_rel).is_absolute() else Path(schema_rel)
    storage_dir = (base / storage_rel) if not Path(storage_rel).is_absolute() else Path(storage_rel)

    return {
        "schema_path":  schema_path,
        "storage_dir":  storage_dir,
        "robot_id":     robot_id,
        "mock_mode":    mock_mode,
    }


async def run_server(server_cfg: ServerConfig, init_kwargs: dict) -> None:
    """在当前 asyncio loop 中运行单个 MCP server"""
    module_path = SERVER_REGISTRY.get(server_cfg.id)
    if not module_path:
        logger.error("Unknown server id: %s", server_cfg.id)
        return

    import importlib
    module = importlib.import_module(module_path)
    mcp = module.create_server(**init_kwargs)

    # 设置 host/port
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = server_cfg.port

    logger.info("Starting %s on port %d  —  %s",
                server_cfg.name, server_cfg.port, server_cfg.description)
    try:
        # FastMCP.run() 是同步的，包装为 asyncio 友好
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: mcp.run(transport="sse"),
        )
    except Exception as e:
        logger.error("Server %s crashed: %s", server_cfg.name, e)
        raise


async def main_async(
    config_path: Path,
    mock_mode: bool,
    enable_override: Optional[list[str]] = None,
) -> None:
    servers = load_servers_config(config_path)

    # 应用命令行覆盖
    if enable_override is not None:
        for s in servers:
            s.enabled = s.id in enable_override

    enabled = [s for s in servers if s.enabled]

    if not enabled:
        logger.error("No servers enabled. Check config/servers.yaml or --enable flag.")
        sys.exit(1)

    # 强制检查必需 server
    required_ids = {s.id for s in servers if s.required}
    enabled_ids  = {s.id for s in enabled}
    missing_required = required_ids - enabled_ids
    if missing_required:
        logger.error("Required servers not enabled: %s", missing_required)
        sys.exit(1)

    init_kwargs = get_init_kwargs(config_path, mock_mode)

    # 打印启动摘要
    print("\n" + "═" * 60)
    print("  Manastone Diagnostic  —  Multi-Server Mode")
    print("═" * 60)
    print(f"  Robot ID  : {init_kwargs['robot_id']}")
    print(f"  Mode      : {'MOCK' if init_kwargs['mock_mode'] else 'REAL DDS'}")
    print(f"  Schema    : {init_kwargs['schema_path']}")
    print()
    print("  Servers:")
    for s in servers:
        status = "✅ ENABLED" if s.enabled else "⬜ disabled"
        print(f"    {status:12}  {s.name:22} :{s.port}  {s.description}")
    print("═" * 60 + "\n")

    # 写 active_servers 供 core server 展示
    # (在 init_shared_state 之后写，这里先准备数据)
    active_server_list = [
        {"id": s.id, "name": s.name, "port": s.port, "description": s.description}
        for s in enabled
    ]

    # 初始化共享状态（只会执行一次）
    from manastone_diag.servers.base import init_shared_state
    state = await init_shared_state(**init_kwargs)
    state.active_servers = active_server_list

    # 并发启动所有 enabled servers
    tasks = [
        asyncio.create_task(
            run_server(s, init_kwargs),
            name=f"server:{s.id}",
        )
        for s in enabled
    ]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        for task in tasks:
            task.cancel()
        from manastone_diag.servers.base import shutdown_shared_state
        await shutdown_shared_state()


def main():
    parser = argparse.ArgumentParser(
        description="Manastone Multi-Server Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  manastone-launcher                         # start all enabled servers
  manastone-launcher --mock                  # force mock mode
  manastone-launcher --enable joints,core    # only start joints + core
  manastone-launcher --list                  # show all available servers
  manastone-launcher --config /path/servers.yaml
        """,
    )
    parser.add_argument("--config", default=None,
                        help="Path to servers.yaml (default: config/servers.yaml)")
    parser.add_argument("--mock", action="store_true",
                        help="Force mock mode (no real DDS)")
    parser.add_argument("--enable", default=None,
                        help="Comma-separated list of server IDs to enable (overrides YAML)")
    parser.add_argument("--list", action="store_true",
                        help="List all available server IDs and exit")

    args = parser.parse_args()

    if args.list:
        print("\nAvailable Manastone MCP Servers:")
        print("-" * 40)
        for sid, module in SERVER_REGISTRY.items():
            print(f"  {sid:12}  ({module})")
        print()
        return

    # Resolve config path
    if args.config:
        config_path = Path(args.config)
    else:
        # Try relative to cwd, then relative to this file
        candidates = [
            Path("config/servers.yaml"),
            Path(__file__).parent.parent.parent / "config" / "servers.yaml",
        ]
        config_path = next((p for p in candidates if p.exists()), candidates[0])

    if not config_path.exists():
        logger.error("servers.yaml not found at %s", config_path)
        logger.error("Create config/servers.yaml or specify --config path")
        sys.exit(1)

    enable_override = None
    if args.enable:
        enable_override = [e.strip() for e in args.enable.split(",")]

    try:
        asyncio.run(main_async(config_path, args.mock, enable_override))
    except KeyboardInterrupt:
        logger.info("Stopped.")

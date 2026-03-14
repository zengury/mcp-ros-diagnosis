"""
server.py — DEPRECATED

这是 v0.1-v0.3 的单体 MCP Server 入口，保留仅供向后兼容。

v0.4 起请使用：
  manastone-launcher          # 启动所有 enabled server
  manastone-core              # 单独启动 core server
  config/servers.yaml         # 控制哪些 server 启动

保留此文件是为了不破坏已有的 Claude Desktop / cursor 配置。
它会直接调用新的 launcher 启动默认配置。
"""
import logging
import os
import sys

logger = logging.getLogger(__name__)


def main():
    logger.warning(
        "manastone-diag is deprecated. "
        "Use 'manastone-launcher' for multi-server mode. "
        "Falling back to launcher..."
    )
    # 直接复用 launcher
    from .launcher import main as launcher_main
    launcher_main()


if __name__ == "__main__":
    main()

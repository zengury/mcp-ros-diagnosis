"""运行时扩展加载与注册。"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from typing import Callable, Iterable

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


RegisterFn = Callable[[FastMCP], None]


@dataclass
class LoadedExtension:
    """已加载扩展信息。"""

    module_name: str
    register_fn: RegisterFn


class ExtensionRegistry:
    """根据环境变量加载并注册 extension。"""

    def __init__(self, env_var: str = "MANASTONE_EXTENSIONS"):
        self.env_var = env_var

    def discover_modules(self) -> list[str]:
        """解析扩展模块列表。"""
        raw = os.getenv(self.env_var, "")
        if not raw.strip():
            return []
        return [m.strip() for m in raw.split(",") if m.strip()]

    def load_extensions(self, module_names: Iterable[str] | None = None) -> list[LoadedExtension]:
        """导入模块并提取 register(server) 函数。"""
        names = list(module_names) if module_names is not None else self.discover_modules()
        loaded: list[LoadedExtension] = []

        for module_name in names:
            module = importlib.import_module(module_name)
            register = getattr(module, "register", None)
            if not callable(register):
                raise ValueError(f"扩展模块 {module_name} 缺少 register(server) 函数")
            loaded.append(LoadedExtension(module_name=module_name, register_fn=register))

        return loaded

    def register_extensions(self, server: FastMCP, module_names: Iterable[str] | None = None) -> list[str]:
        """加载并注册扩展，返回已注册模块名。"""
        loaded = self.load_extensions(module_names)
        for ext in loaded:
            ext.register_fn(server)
            logger.info("✅ Extension 已注册: %s", ext.module_name)
        return [ext.module_name for ext in loaded]

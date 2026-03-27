"""
servers/base.py
所有 MCP Server 共享的 AppState 和 lifespan 工厂。

多个 server 进程通过这里访问同一套底层服务：
  - DDSBridge（数据采集）
  - EventLog（事件持久化）
  - EventDetector（语义化检测）
  - RobotSchema（硬件拓扑）

在同一进程内，AppState 是单例（通过 get_shared_state() 获取）。
在多进程部署时，每个进程独立初始化，通过 Unix socket 或 HTTP 互访。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Shared singleton ─────────────────────────────────────────
_shared: Optional["AppState"] = None


@dataclass
class AppState:
    schema:          object           # RobotSchema
    dds_bridge:      object           # DDSBridge
    event_log:       object           # EventLog
    event_detector:  object           # EventDetector
    orchestrator:    object           # DiagnosticOrchestrator
    robot_id:        str
    mock_mode:       bool
    schema_path:     Path
    storage_dir:     Path
    # 已启动的 server 列表（launcher 写入，core server 读取用于展示）
    active_servers:  list = field(default_factory=list)


async def init_shared_state(
    schema_path: str | Path,
    storage_dir: str | Path,
    robot_id: str,
    mock_mode: bool,
) -> "AppState":
    """
    初始化共享服务并返回 AppState。
    幂等：如果已经初始化过，直接返回已有实例。
    """
    global _shared
    if _shared is not None:
        return _shared

    from ..schema import SchemaLoader
    from ..schema.loader import SchemaRegistry
    from ..dds_bridge import DDSBridge
    from ..event import EventLog, EventDetector
    from ..orchestrator.diagnostic import DiagnosticOrchestrator
    from ..llm import LLMClient
    from ..config import get_config

    config = get_config()
    schema_path = Path(schema_path)
    storage_dir = Path(storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)

    # Schema — 优先用 SchemaRegistry（支持多机器人），回退到直接加载指定文件
    config_dir = schema_path.parent
    registry = SchemaRegistry(config_dir)
    if registry.available_types():
        schema = registry.load()   # 读取 MANASTONE_ROBOT_TYPE 或默认第一个
    else:
        loader = SchemaLoader(schema_path)
        schema = loader.load()
    logger.info("Schema: %s | %d topics | %d components",
                schema.robot_type, len(schema.topics), len(schema.components))

    # DDS Bridge
    dds = DDSBridge(schema=schema, mock_mode=mock_mode)
    await dds.start()

    # EventLog
    db_path = storage_dir / f"{robot_id}_events.db"
    event_log = EventLog(db_path)

    # EventDetector (runs background tasks)
    detector = EventDetector(
        schema=schema, dds_bridge=dds,
        event_log=event_log, robot_id=robot_id,
    )
    await detector.start()

    # LLM + Orchestrator
    llm = LLMClient(config.llm)
    orchestrator = DiagnosticOrchestrator(
        llm=llm, knowledge_dir=config.knowledge_dir,
    )

    _shared = AppState(
        schema=schema,
        dds_bridge=dds,
        event_log=event_log,
        event_detector=detector,
        orchestrator=orchestrator,
        robot_id=robot_id,
        mock_mode=mock_mode,
        schema_path=schema_path,
        storage_dir=storage_dir,
    )
    logger.info("AppState initialized: robot_id=%s mock=%s", robot_id, mock_mode)
    return _shared


async def shutdown_shared_state() -> None:
    global _shared
    if _shared is None:
        return
    logger.info("Shutting down shared state...")
    await _shared.event_detector.stop()
    await _shared.dds_bridge.stop()
    _shared.event_log.close()
    _shared = None


def get_shared_state() -> "AppState":
    """在 MCP tool handler 里调用，获取已初始化的共享状态。"""
    if _shared is None:
        raise RuntimeError("AppState not initialized. Call init_shared_state() first.")
    return _shared

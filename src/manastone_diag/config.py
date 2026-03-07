"""
Manastone Diagnostic 配置模块
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DDSConfig:
    """DDS 配置"""
    domain_id: int = 0
    interface: str = "auto"  # 或指定网卡名如 "eth0"
    topics: dict = field(default_factory=lambda: {
        "joint_leg_state": "/aima/hal/joint/leg/state",
        "joint_waist_state": "/aima/hal/joint/waist/state",
        "joint_arm_state": "/aima/hal/joint/arm/state",
        "joint_head_state": "/aima/hal/joint/head/state",
        "pmu_state": "/aima/hal/pmu/state",
    })


@dataclass
class CacheConfig:
    """缓存配置"""
    max_size: int = 1000  # 最大缓存消息数
    window_seconds: int = 300  # 滑动窗口秒数 (5分钟)


@dataclass
class LLMConfig:
    """LLM 配置"""
    # 本地 LLM（Orin NX 上的 Qwen2.5-7B）
    local_url: str = "http://127.0.0.1:8081/v1"
    local_model: str = "qwen2.5-7b"
    # 远端 LLM（开发环境，OpenAI 兼容 API）
    remote_url: str = field(default_factory=lambda: os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"))
    remote_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    # 通用参数
    max_tokens: int = 800
    temperature: float = 0.3
    timeout: float = 90.0
    # 自动检测：有 API key 则用远端，否则用本地
    @property
    def use_remote(self) -> bool:
        return bool(self.api_key)


@dataclass
class ServerConfig:
    """MCP Server 配置"""
    host: str = "0.0.0.0"
    port: int = 8080
    transport: str = "sse"  # sse 或 stdio


@dataclass
class UIConfig:
    """Web UI 配置"""
    host: str = "0.0.0.0"
    port: int = 7860
    share: bool = False


@dataclass
class Config:
    """全局配置"""
    dds: DDSConfig = field(default_factory=DDSConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    
    # 运行模式
    mock_mode: bool = False  # 模拟数据模式（无真机时使用）
    debug: bool = False
    
    # 路径
    knowledge_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "knowledge"
    ))
    models_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "models"
    ))
    extension_modules: list[str] = field(default_factory=list)


# 全局配置实例
_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置"""
    global _config
    if _config is None:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        _config = Config()
        # 从环境变量读取覆盖
        if os.getenv("MANASTONE_MOCK_MODE"):
            _config.mock_mode = os.getenv("MANASTONE_MOCK_MODE").lower() == "true"
        if os.getenv("MANASTONE_DEBUG"):
            _config.debug = os.getenv("MANASTONE_DEBUG").lower() == "true"
        topic_env_map = {
            "joint_leg_state": "MANASTONE_TOPIC_JOINT_LEG_STATE",
            "joint_waist_state": "MANASTONE_TOPIC_JOINT_WAIST_STATE",
            "joint_arm_state": "MANASTONE_TOPIC_JOINT_ARM_STATE",
            "joint_head_state": "MANASTONE_TOPIC_JOINT_HEAD_STATE",
            "pmu_state": "MANASTONE_TOPIC_PMU_STATE",
        }
        for topic_key, env_name in topic_env_map.items():
            val = os.getenv(env_name)
            if val:
                _config.dds.topics[topic_key] = val
        extension_env = os.getenv("MANASTONE_EXTENSIONS", "")
        if extension_env.strip():
            _config.extension_modules = [m.strip() for m in extension_env.split(",") if m.strip()]
    return _config


def set_config(config: Config) -> None:
    """设置全局配置"""
    global _config
    _config = config

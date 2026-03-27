"""
Manastone Diagnostic — 配置模块 v0.4
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LLMConfig:
    local_url:    str   = "http://127.0.0.1:8081/v1"
    local_model:  str   = "qwen2.5-7b"
    remote_url:   str   = field(default_factory=lambda: os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"))
    remote_model: str   = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    api_key:      str   = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    max_tokens:   int   = 800
    temperature:  float = 0.3
    timeout:      float = 90.0

    @property
    def use_remote(self) -> bool:
        return bool(self.api_key)


@dataclass
class Config:
    llm:              LLMConfig = field(default_factory=LLMConfig)
    mock_mode:        bool = False
    debug:            bool = False
    knowledge_dir:    str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "knowledge"
    ))
    models_dir:       str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "models"
    ))
    # Legacy: kept for backward compatibility with extensions
    extension_modules: list = field(default_factory=list)


_config: Optional[Config] = None


def get_config() -> Config:
    global _config
    if _config is None:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        _config = Config()
        if os.getenv("MANASTONE_MOCK_MODE"):
            _config.mock_mode = os.getenv("MANASTONE_MOCK_MODE", "").lower() == "true"
        # MANASTONE_ROBOT_TYPE 用于 SchemaRegistry.load() 自动选择 schema 文件
        # 例如：export MANASTONE_ROBOT_TYPE=unitree_go2
        if os.getenv("MANASTONE_DEBUG"):
            _config.debug = os.getenv("MANASTONE_DEBUG","").lower() == "true"
        ext_env = os.getenv("MANASTONE_EXTENSIONS","")
        if ext_env.strip():
            _config.extension_modules = [m.strip() for m in ext_env.split(",") if m.strip()]
    return _config


def set_config(config: Config) -> None:
    global _config
    _config = config

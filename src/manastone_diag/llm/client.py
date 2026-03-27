"""
LLM 客户端 - 支持本地 Qwen2.5-7B 和远端 OpenAI 兼容 API

两种调用模式：
  chat()             - 单轮问答（现有功能）
  chat_with_tools()  - 多轮 tool_use（支持 Agent 循环）
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from ..config import LLMConfig

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    async def chat(self, user_message: str, system_prompt: str = "") -> str:
        """发送对话请求，返回文本回复"""
        if self.config.use_remote:
            return await self._call_api(
                url=self.config.remote_url,
                model=self.config.remote_model,
                api_key=self.config.api_key,
                user_message=user_message,
                system_prompt=system_prompt,
            )
        else:
            return await self._call_api(
                url=self.config.local_url,
                model=self.config.local_model,
                api_key="not-required",
                user_message=user_message,
                system_prompt=system_prompt,
            )

    async def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: str = "",
    ) -> Dict[str, Any]:
        """
        多轮 tool_use 对话——支持 Agent 循环架构的核心接口。

        与 chat() 的本质区别：
          chat()           → 单轮，Python 控制循环，LLM 只是子函数
          chat_with_tools() → 返回原始 message（含 tool_calls），
                             让调用方决定是继续对话还是执行工具

        Args:
            messages:      完整对话历史（role: user/assistant/tool）
            tools:         OpenAI 格式的工具定义列表
            system_prompt: 系统提示词

        Returns:
            完整 assistant message dict，可能包含：
              - content: 文本内容（LLM 决定直接回复时）
              - tool_calls: 工具调用列表（LLM 决定调用工具时）
              - finish_reason 通过调用方从 choice 里读取
        """
        url = self.config.remote_url if self.config.use_remote else self.config.local_url
        model = self.config.remote_model if self.config.use_remote else self.config.local_model
        api_key = self.config.api_key if self.config.use_remote else "not-required"

        all_messages = []
        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})
        all_messages.extend(messages)

        payload = {
            "model": model,
            "messages": all_messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }

        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(
                    f"{url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "User-Agent": "claude-code/1.0",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                msg = choice["message"]
                msg["_finish_reason"] = choice.get("finish_reason", "")
                return msg

        except httpx.TimeoutException:
            logger.warning(f"LLM 请求超时 ({self.config.timeout}s)")
            raise
        except Exception as e:
            logger.error(f"LLM tool_use 请求失败: {e}")
            raise

    async def _call_api(
        self,
        url: str,
        model: str,
        api_key: str,
        user_message: str,
        system_prompt: str,
    ) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }

        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(
                    f"{url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "User-Agent": "claude-code/1.0",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()

        except httpx.TimeoutException:
            logger.warning(f"LLM 请求超时 ({self.config.timeout}s)")
            raise
        except Exception as e:
            logger.error(f"LLM 请求失败: {e}")
            raise

    def is_available(self) -> bool:
        """检查 LLM 是否已配置"""
        if self.config.use_remote:
            return bool(self.config.api_key)
        return True  # 本地 LLM 假定可用（运行时检测）


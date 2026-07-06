"""LLM 调用统一抽象服务。

统一管理 LLM 调用、provider 解析、错误处理。
所有模块通过 LLMService 调用 LLM，消除分散在各处的直接 context.llm_generate 调用。
"""

from __future__ import annotations

import asyncio
from typing import Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("active_learner")


class LLMService:
    """LLM 调用服务。

    职责：
    - 统一调用入口，自动提取 completion_text
    - 自动 provider 解析（委托插件现有 _resolve_plugin_provider_id）
    - 超时/异常降级返回空字符串
    """

    def __init__(self, plugin):
        self._plugin = plugin

    async def generate(
        self,
        prompt: str,
        provider_id: Optional[str] = None,
        event=None,
        umo: str = "",
    ) -> str:
        """调用 LLM 生成文本。失败返回空字符串。

        参数：
            prompt: 提示词
            provider_id: 指定 provider（为空则自动解析）
            event: AstrMessageEvent（用于自动解析 provider）
            umo: unified_msg_origin（备用 provider 解析）
        """
        if not provider_id:
            provider_id = await self.resolve_provider_id(event=event, umo=umo)
        if not provider_id:
            return ""

        try:
            resp = await self._plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            text = getattr(resp, "completion_text", "") or ""
            if not text:
                text = str(resp) if resp else ""
            return text
        except asyncio.TimeoutError:
            logger.warning(f"LLMService: provider {provider_id} 超时")
            return ""
        except Exception as e:
            logger.error(f"LLMService: LLM 调用失败 (provider={provider_id}): {e}")
            return ""

    async def resolve_provider_id(self, event=None, umo: str = "") -> str:
        """解析 LLM Provider ID，委托插件现有的 4 层 fallback。"""
        resolved_umo = umo
        if event is not None and not umo:
            try:
                resolved_umo = getattr(event, "unified_msg_origin", "")
            except Exception:
                pass
        try:
            return await self._plugin._resolve_plugin_provider_id(umo=resolved_umo)
        except Exception:
            return ""

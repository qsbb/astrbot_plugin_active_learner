"""LLM 调用统一抽象服务。

统一管理 LLM 调用、provider 解析、错误处理、token 用量统计。
所有模块通过 LLMService 调用 LLM，消除分散在各处的直接 context.llm_generate 调用。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from .plugin_logger import logger


def _estimate_tokens(text: str) -> int:
    """粗略估算文本的 token 数。

    中文按 ~1.5 字符/token，英文按 ~4 字符/token 估算。
    实际值以 provider 返回的 usage 为准（若可用）。
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4)


def _extract_usage(resp) -> dict:
    """尝试从 LLM 响应对象中提取真实 token 用量。

    AstrBot 不同版本/provider 可能用不同字段，按优先级尝试。
    成功返回 {"prompt_tokens": int, "completion_tokens": int}，失败返回 {}。
    """
    if resp is None:
        return {}

    # 1. resp.usage（OpenAI 风格，可能是 dict 或对象）
    usage = getattr(resp, "usage", None)
    if usage is not None:
        if isinstance(usage, dict):
            pt = usage.get("prompt_tokens") or usage.get("input_tokens")
            ct = usage.get("completion_tokens") or usage.get("output_tokens")
            if pt is not None and ct is not None:
                return {"prompt_tokens": int(pt), "completion_tokens": int(ct)}
        else:
            pt = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None)
            ct = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None)
            if pt is not None and ct is not None:
                return {"prompt_tokens": int(pt), "completion_tokens": int(ct)}

    # 2. 直接属性 resp.prompt_tokens / resp.completion_tokens
    pt = getattr(resp, "prompt_tokens", None)
    ct = getattr(resp, "completion_tokens", None)
    if pt is not None and ct is not None:
        return {"prompt_tokens": int(pt), "completion_tokens": int(ct)}

    # 3. resp.total_tokens（只有总数，按 prompt/completion 各半近似）
    tt = getattr(resp, "total_tokens", None)
    if tt is not None:
        half = int(tt) // 2
        return {"prompt_tokens": half, "completion_tokens": int(tt) - half}

    return {}


class LLMService:
    """LLM 调用服务。

    职责：
    - 统一调用入口，自动提取 completion_text
    - 自动 provider 解析（委托插件现有 _resolve_plugin_provider_id）
    - 超时/异常降级返回空字符串
    - 持久化 token 用量统计（按 1天/3天/7天/总计 时间窗口查询）
    """

    def __init__(self, plugin):
        self._plugin = plugin
        # 估算调用计数（运行时内存，仅用于诊断「有多少次是字符估算」）
        self._estimated_calls = 0

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
            logger.warning("LLMService: 无法解析 provider，跳过 LLM 调用")
            return ""

        prompt_preview = prompt.replace("\n", " ")[:60]
        logger.info(f"LLM 调用 [model={provider_id}] prompt={prompt_preview!r}")

        try:
            resp = await self._plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            text = getattr(resp, "completion_text", "") or ""
            if not text:
                text = str(resp) if resp else ""
            logger.info(f"LLM 回复 [model={provider_id}] len={len(text)} preview={text[:60]!r}")

            # token 用量统计
            usage = _extract_usage(resp)
            if usage:
                prompt_tokens = usage["prompt_tokens"]
                completion_tokens = usage["completion_tokens"]
            else:
                # 回退到字符估算
                prompt_tokens = _estimate_tokens(prompt)
                completion_tokens = _estimate_tokens(text)
                self._estimated_calls += 1

            # 持久化到数据库（支持按时间窗口查询）
            try:
                self._plugin.store.record_token_usage(
                    provider_id, prompt_tokens, completion_tokens
                )
            except Exception as db_err:
                logger.debug(f"token 用量记录入库失败（不影响主流程）: {db_err}")

            total = prompt_tokens + completion_tokens
            logger.info(
                f"📊 token 用量 [model={provider_id}] "
                f"prompt={prompt_tokens} completion={completion_tokens} total={total}"
            )
            return text
        except asyncio.TimeoutError:
            logger.warning(f"LLMService: provider {provider_id} 超时")
            return ""
        except Exception as e:
            logger.error(f"LLMService: LLM 调用失败 (provider={provider_id}): {e}")
            return ""

    def get_token_stats(self) -> dict:
        """返回 token 用量统计（按时间窗口：1天/3天/7天/总计 + 近7天按 provider 分组）。"""
        try:
            stats = self._plugin.store.get_token_usage_stats()
            stats["estimated_calls"] = self._estimated_calls
            return stats
        except Exception as e:
            logger.debug(f"获取 token 统计失败: {e}")
            return {
                "1d": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0},
                "3d": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0},
                "7d": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0},
                "total": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0},
                "per_provider": [],
                "estimated_calls": self._estimated_calls,
            }

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

# -*- coding: utf-8 -*-
"""多模型路由器：优先级调度、加权选择与自动降级。"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from openai import APIError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel

from app.infrastructure.llm.circuit_breaker import CircuitBreaker
from app.infrastructure.llm.types import ModelProvider


class LLMResponse(BaseModel):
    """统一 LLM 响应结构。"""

    content: str = ""
    model_id: str = ""
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None


@dataclass
class ModelConfig:
    """单路模型配置。"""

    # 模型 ID，会作为 OpenAI 兼容接口的 model 参数。
    model_id: str
    # 调用该模型服务所需的 API Key。
    api_key: str
    # 自定义 OpenAI 兼容服务地址；为空时使用 SDK 默认地址。
    base_url: str | None = None
    # 模型提供方类型，当前主要用于后续扩展不同供应商适配。
    provider: ModelProvider = ModelProvider.OPENAI
    # 路由优先级，数字越小越优先。
    priority: int = 0
    # 同优先级内的加权随机权重，权重越大越容易被优先尝试。
    weight: float = 1.0
    # 预留扩展配置，例如区域、成本、超时等模型元信息。
    extra: dict[str, Any] = field(default_factory=dict)


class ModelRouter:
    """多模型路由器。

    统一管理多个 OpenAI 兼容模型客户端，并在调用时按优先级、权重和熔断状态
    选择可用模型。上层只需要提交消息列表，不必关心具体模型的降级与重试顺序。
    """

    def __init__(
        self,
        model_configs: list[ModelConfig],
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        if not model_configs:
            raise ValueError("model_configs 不能为空")

        # 优先级数字越小越靠前；后续候选模型选择会在这个顺序基础上分组。
        self._configs = sorted(model_configs, key=lambda c: c.priority)
        self._breakers: dict[str, CircuitBreaker] = {}
        for cfg in self._configs:
            self._breakers[cfg.model_id] = CircuitBreaker(
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                name=f"llm:{cfg.model_id}",
            )
        self._clients: dict[str, AsyncOpenAI] = {}
        for cfg in self._configs:
            # 每个模型维护独立客户端，方便支持不同 API Key 或 OpenAI 兼容 base_url。
            kwargs: dict[str, Any] = {"api_key": cfg.api_key}
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            self._clients[cfg.model_id] = AsyncOpenAI(**kwargs)

    def _select_candidates(
        self,
        model_preference: str | None,
    ) -> list[ModelConfig]:
        """形成本次请求的候选模型列表。

        如果指定的模型存在，则尊重显式偏好；否则先按优先级排序，再在同优先级
        内使用加权随机，避免同一优先级下的流量总是打到固定模型。
        """
        if model_preference:
            exact = [c for c in self._configs if c.model_id == model_preference]
            if exact:
                return exact

        by_prio: dict[int, list[ModelConfig]] = {}
        for cfg in self._configs:
            by_prio.setdefault(cfg.priority, []).append(cfg)

        ordered: list[ModelConfig] = []
        for prio in sorted(by_prio.keys()):
            group = by_prio[prio]
            # 同优先级内按权重做随机排序（权重越大越容易被排到前面）
            scored = [
                (cfg, random.random() ** (1.0 / max(cfg.weight, 0.01)))
                for cfg in group
            ]
            scored.sort(key=lambda x: -x[1])
            ordered.extend(cfg for cfg, _ in scored)
        return ordered or list(self._configs)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model_preference: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """路由到合适模型，失败时按候选顺序自动降级。

        每个模型调用都经过独立熔断器：连续失败的模型会被快速跳过，恢复窗口
        之后再进入半开状态试探。
        """
        candidates = self._select_candidates(model_preference)
        last_error: Exception | None = None

        for cfg in candidates:
            breaker = self._breakers[cfg.model_id]
            try:
                # 记录模型调用成功或失败
                return await breaker.call(
                    self._try_model,
                    cfg.model_id,
                    messages,
                    **kwargs,
                )
            except RuntimeError as exc:
                # 熔断打开
                last_error = exc
                logger.warning("模型 [{}] 被熔断跳过: {}", cfg.model_id, exc)
            except (APIError, RateLimitError, asyncio.TimeoutError) as exc:
                last_error = exc
                logger.warning("模型 [{}] 调用失败，尝试降级: {}", cfg.model_id, exc)
            except Exception as exc:
                last_error = exc
                logger.exception("模型 [{}] 未预期错误: {}", cfg.model_id, exc)

        msg = "所有候选模型均不可用"
        if last_error:
            raise RuntimeError(msg) from last_error
        raise RuntimeError(msg)

    async def _try_model(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        """调用指定模型并转换为统一响应结构。

        这里只负责 OpenAI 兼容接口请求和响应归一化；熔断、降级和候选选择由
        外层 ``chat`` 方法负责。
        """
        client = self._clients[model_id]
        temperature = kwargs.pop("temperature", 0.7)
        max_tokens = kwargs.pop("max_tokens", None)

        # 保留 kwargs 扩展口，允许上层透传 stop、top_p 等 OpenAI 兼容参数。
        params: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        params.update(kwargs)

        try:
            resp = await client.chat.completions.create(**params)
        except Exception:
            logger.exception("OpenAI 兼容接口调用失败 model_id={}", model_id)
            raise

        choice = resp.choices[0] if resp.choices else None
        content = (choice.message.content or "") if choice else ""
        usage = None
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }

        return LLMResponse(
            content=content,
            model_id=model_id,
            usage=usage,
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

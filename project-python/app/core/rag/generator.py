# -*- coding: utf-8 -*-
"""RAG 答案生成：基于检索上下文与对话历史，支持引用标注。"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from app.models.schemas import Citation, Message, RAGResponse, RetrievalResult


@runtime_checkable
class RAGLLMProtocol(Protocol):
    """支持异步生成的 LLM 接口（如 LangChain Runnable）。"""

    async def ainvoke(self, input: Any, **kwargs: Any) -> Any:
        ...


class RAGGenerator:
    """
    RAG 答案生成器，负责把“问题 + 检索结果 + 对话历史”转成最终可返回的回答对象。

    主要做两件事：
    1) 生成答案：将检索上下文按 [1]/[2] 编号组织进提示词，连同系统提示与历史对话一起发送给 LLM。
    2) 生成引用：从模型回答中提取形如 [n] 的引用标记，并映射回对应的检索结果，产出结构化 Citation。

    最终输出为 RAGResponse，包含回答文本、引用列表、原始上下文以及可选模型名，便于前端展示与追溯。
    """

    def __init__(
        self,
        llm: Any,
        system_prompt: str | None = None,
        model_name: str | None = None,
    ) -> None:
        """
        :param llm: 实现 ``ainvoke`` 的模型对象，或兼容 OpenAI 风格的封装
        :param system_prompt: 可选系统提示，强调引用格式
        :param model_name: 写入 RAGResponse.model 的展示名
        """
        self._llm = llm
        self._system_prompt = system_prompt or (
            "你是严谨的知识助手。仅根据提供的上下文作答；若上下文不足请说明。"
            "回答中引用来源时使用 [1]、[2] 等形式，与上下文编号一致。"
        )
        self._model_name = model_name

    def _build_messages(
        self,
        query: str,
        contexts: list[RetrievalResult],
        chat_history: list[Any],
    ) -> list[dict[str, str]]:
        """构造 chat messages（OpenAI 风格）。"""
        ctx_lines = []
        for i, c in enumerate(contexts, start=1):
            ctx_lines.append(f"[{i}] (id={c.id})\n{c.content}")
        context_block = "\n\n".join(ctx_lines) if ctx_lines else "（无检索上下文）"

        history_lines: list[str] = []
        for msg in chat_history:
            if isinstance(msg, Message):
                history_lines.append(f"{msg.role.value}: {msg.content}")
            elif isinstance(msg, dict):
                role = msg.get("role", "user")
                content = msg.get("content", "")
                history_lines.append(f"{role}: {content}")
            else:
                history_lines.append(str(msg))

        user_content = (
            f"## 检索上下文\n{context_block}\n\n"
            f"## 历史对话（摘要参考）\n"
            f"{chr(10).join(history_lines) if history_lines else '（无）'}\n\n"
            f"## 用户问题\n{query}\n\n"
            "请作答并在必要时使用 [1]、[2] 引用上下文编号。"
        )

        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_content},
        ]

    def _extract_citations(self, answer: str, contexts: list[RetrievalResult]) -> list[Citation]:
        """
        从模型回答里抽取形如 ``[1]``、``[2]`` 的引用标记，并与检索结果列表按编号对齐，生成结构化 ``Citation`` 列表。

        流程说明：

        1. 用 ``re.findall`` 匹配回答中所有 ``[`` 与 ``]`` 之间的正整数编号，得到按出现顺序的编号序列（同一编号可出现多次）。
        2. 按该顺序遍历编号；若编号已处理过、小于 1、或大于 ``len(contexts)``，则跳过（越界或重复引用不会写入结果）。
        3. 对有效编号 ``idx``，取 ``contexts[idx - 1]``（与 ``_build_messages`` 里 ``[1]`` 起始于 1 的约定一致），构造 ``Citation``：
           ``index`` 为引用编号，``result_id`` 为对应条目的 ``id``，``snippet`` 为 ``content`` 前 200 字符，超长时追加 ``...``。

        注意：本方法只做“标记 → 检索条”的机械映射，不校验回答内容与片段是否语义一致；若回答中出现非引用用途的 ``[数字]``，也会被当作引用解析。

        :param answer: 模型生成的回答正文，其中可含 ``[n]`` 引用（``n`` 为正整数）。
        :param contexts: 与提示词中 ``[1]``、``[2]`` … 顺序一致的检索结果列表；空列表时任何编号均视为越界而不会产出引用。
        :returns: 按在回答中**首次出现**顺序排列的 ``Citation`` 列表；无有效标记或全部被过滤时为空列表。
        """
        refs = [int(x) for x in re.findall(r"\[(\d+)\]", answer)]
        citations: list[Citation] = []
        seen: set[int] = set()
        for idx in refs:
            if idx in seen or idx < 1 or idx > len(contexts):
                continue
            seen.add(idx)
            r = contexts[idx - 1]
            snippet = r.content[:200] + ("..." if len(r.content) > 200 else "")
            citations.append(
                Citation(index=idx, result_id=r.id, snippet=snippet),
            )
        return citations

    def _parse_llm_output(self, raw: Any) -> str:
        """从 LLM 返回对象中提取文本。"""
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        if hasattr(raw, "content"):
            return str(getattr(raw, "content", ""))
        if isinstance(raw, dict) and "content" in raw:
            return str(raw["content"])
        return str(raw)

    async def generate(
        self,
        query: str,
        contexts: list[RetrievalResult],
        chat_history: list[Any],
    ) -> RAGResponse:
        """基于上下文与历史生成回答，并解析引用。"""
        messages = self._build_messages(query, contexts, chat_history)

        try:
            if isinstance(self._llm, RAGLLMProtocol):
                raw = await self._llm.ainvoke(messages)
            elif callable(getattr(self._llm, "ainvoke", None)):
                raw = await self._llm.ainvoke(messages)  # type: ignore[misc]
            else:
                raise TypeError("llm 需实现异步 ainvoke(messages)")
        except Exception as e:
            logger.exception("RAG 生成调用失败: {}", e)
            raise RuntimeError(f"生成失败: {e}") from e

        answer = self._parse_llm_output(raw).strip()
        citations = self._extract_citations(answer, contexts)

        return RAGResponse(
            answer=answer,
            citations=citations,
            raw_contexts=list(contexts),
            model=self._model_name,
        )

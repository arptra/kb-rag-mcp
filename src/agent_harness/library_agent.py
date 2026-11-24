"""A bounded LangChain agent turn over the Harness model and authorized tool dispatcher."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from contextvars import Context
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.runtime import get_runtime
from langsmith import tracing_context

from agent_harness.contracts import ModelDecisionError

Call = dict[str, Any]
Decide = Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]]
Dispatch = Callable[[list[Call]], Awaitable[list[str] | None]]


@dataclass
class _Round:
    decide: Decide
    dispatch: Dispatch
    allowed: set[str]
    calls: list[Call] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    decision: dict[str, Any] | None = None
    stopped: bool = False

    async def next_message(self, messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            call = self.calls[len(self.observations)]
            if last.name != call["name"] or not isinstance(last.content, str):
                raise ModelDecisionError("Invalid tool observation")
            self.observations.append({**call, "result": last.content})
            if self.stopped:
                self.decision = {"content": "", "tool_calls": [], "agent_round_stopped": True}
            elif len(self.calls) == len(self.observations):
                # The next decision belongs to the outer graph's durable checkpoint.
                self.decision = await self.decide(self.observations)
        else:
            first = await self.decide([])
            calls = first.get("tool_calls", [])
            if not isinstance(calls, list) or len(calls) > 32 or any(
                not isinstance(call, dict) or not isinstance(call.get("name"), str)
                or call["name"] not in self.allowed or not isinstance(call.get("args", {}), dict)
                for call in calls
            ):
                raise ModelDecisionError("Invalid or unavailable tool in the research batch")
            self.calls = calls
            if not calls:
                self.decision = first
        if self.decision is not None:
            return AIMessage(content=self.decision.get("content") or "")
        # create_agent can run independent tool calls concurrently. Present one
        # call at a time so each real ToolMessage precedes the next side effect.
        call = self.calls[len(self.observations)]
        return AIMessage(content="", tool_calls=[{
            "id": uuid4().hex, "name": call["name"], "args": call.get("args", {}),
        }])


class _HarnessChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "harness-decision-port"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> _HarnessChatModel:
        # The Harness decision callback passes the scoped schemas to its provider.
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("The Harness model port is asynchronous")

    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        message = await get_runtime(_Round).context.next_message(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])


def _tool(specification: dict[str, Any]) -> StructuredTool:
    name = specification["name"]

    async def execute(**arguments: Any) -> str:
        state = get_runtime(_Round).context
        requested = state.calls[len(state.observations)]
        if requested["name"] != name or requested.get("args", {}) != arguments:
            raise ModelDecisionError("Tool call differs from the scheduled action")
        observed = await state.dispatch([requested])
        if observed is None:
            state.stopped = True
            return ""
        if len(observed) != 1 or not isinstance(observed[0], str):
            raise ModelDecisionError("Tool dispatcher returned an invalid observation")
        return observed[0]

    return StructuredTool.from_function(
        name=name, description=specification.get("description") or name, coroutine=execute,
        args_schema=specification.get("parameters") or {"type": "object"},
    )


@lru_cache(maxsize=32)
def _agent(catalog: str) -> Any:
    tools = [_tool(item["function"]) for item in json.loads(catalog)]
    return create_agent(_HarnessChatModel(cache=False), tools, context_schema=_Round,
                        checkpointer=False, name="harness_research_agent")


async def run_agent_round(
    *, tools: list[dict[str, Any]], decide: Decide, dispatch: Dispatch,
) -> dict[str, Any]:
    """Execute one provider-proposed batch, then hand off its observation-informed decision.

    The caller owns budgets, authorization, progress and checkpoints. Its dispatch
    callback returns None when it needs control before another action/model call.
    """
    state = _Round(decide, dispatch, {t["function"]["name"] for t in tools})
    agent = _agent(json.dumps(tools, ensure_ascii=False, sort_keys=True))
    # Neither the parent graph's saver nor another invocation's tool/model state
    # belongs to this ephemeral turn. Cancellation still propagates to this task.
    async def invoke() -> None:
        with tracing_context(enabled=False):
            await agent.ainvoke(
                {"messages": [HumanMessage(content="Research step")]}, context=state,
                config={"recursion_limit": 72},
            )

    await asyncio.create_task(invoke(), context=Context())
    if state.decision is None:
        raise RuntimeError("Agent finished without a decision")
    return state.decision

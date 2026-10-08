import logging
import json
import math
import re
import time
from datetime import datetime, timezone
from typing import Any

from akaryon.agents.base import Agent
from akaryon.agents.sessions import AgentSession, AgentSessionManager
from akaryon.core.events import EventBus
from akaryon.core.exceptions import TaskCancelled, ProviderError, ToolsDisabledForTurnError
from akaryon.models.base import ModelRequest
from akaryon.models.router import ModelRouter
from akaryon.tasks.manager import TaskManager
from akaryon.tasks.models import TaskStatus
from akaryon.tools.executor import ToolExecutor
from akaryon.usage.ledger import month_start

logger = logging.getLogger(__name__)

_TOOL_OPTOUT_PATTERNS = (
    re.compile(r"\b(?:do not|don['’]t|dont|should not|should['’]t|never)\s+"
               r"(?:use|call|invoke|run|trigger)\s+"
               r"(?:(?:any|the|available|native|akaryon)\s+){0,2}"
               r"(?:tools?|functions?|tool calls?|function calls?)\b", re.IGNORECASE),
    re.compile(r"\bwithout\s+(?:using|calling|invoking)\s+"
               r"(?:(?:any|the|available|akaryon)\s+)?(?:tools?|functions?)\b", re.IGNORECASE),
    re.compile(r"\bavoid\s+(?:using\s+)?(?:(?:any|the|available|akaryon)\s+)?"
               r"(?:tools?|functions?|tool calls?|function calls?)\b", re.IGNORECASE),
    re.compile(r"\bno\s+(?:tools?|function calls|tool use)\b", re.IGNORECASE),
    re.compile(r"\b(?:do not|don['’]t|dont|should not|should['’]t|never)\s+"
               r"(?:make|allow|enable)\s+(?:(?:any|the|native|akaryon)\s+){0,2}"
               r"(?:tool calls?|function calls?|tool use)\b", re.IGNORECASE),
)


def _user_opted_out_of_tools(prompt: str) -> bool:
    return any(pattern.search(prompt) for pattern in _TOOL_OPTOUT_PATTERNS)

SYSTEM_INSTRUCTIONS = (
    "You are Akaryon, a clear and helpful local AI assistant. Akaryon has a built-in memory_store tool "
    "that saves notes in its configured database; it does not use filesystem paths. Use it only when the "
    "user explicitly asks you to remember or save a durable note. Never store passwords, API keys, access "
    "tokens, private keys, or other credentials. Do not claim a note was saved unless the tool reports "
    "success. Memory writes require operator approval; if approval is pending, tell the user to review it "
    "in the local Approvals dashboard. Tool execution is restricted by Akaryon's permission checks. "
    "Choose tools sparingly: answer questions, explain, calculate, summarize, and draft directly without "
    "tools. A tool being available is not permission to use it. Call a tool only when the user clearly asks "
    "you to perform an action that requires that tool; do not infer an action request from a hypothetical, "
    "example, or a request that merely discusses a tool or action. If the requested action or target is "
    "unclear, ask a brief clarifying question before calling a tool. Follow the user's explicit per-turn "
    "tool setting; if Akaryon tools are off, do not call or imitate a tool. If the user's message explicitly "
    "asks you not to use tools, answer directly even when tools are enabled in the composer."
    " Windows app actions are available only through configured aliases; both opening and closing "
    "require approval. A close can target only an app launched by Akaryon in this session and sends "
    "a graceful window-close message that may leave a save prompt visible. Never use the terminal or "
    "a guessed process ID to control an app window."
)


class ManagerAgent:
    def __init__(self, router: ModelRouter, tasks: TaskManager, events: EventBus,
                 tool_executor: ToolExecutor | None = None,
                 sessions: AgentSessionManager | None = None, usage_ledger=None) -> None:
        self.agent = Agent("manager", "ManagerAgent", "Initial Akaryon system agent",
                           SYSTEM_INSTRUCTIONS)
        self.router, self.tasks, self.events = router, tasks, events
        self.tool_executor = tool_executor
        self.sessions = sessions
        self.usage_ledger = usage_ledger
        self.max_tool_rounds = 6

    def run(self, prompt: str, history: list[dict[str, str]] | None = None,
            conversation_id: str | None = None, provider_id: str | None = None,
            model: str | None = None, *, task_description: str | None = None,
            task_type: str | None = None, tools_enabled: bool = True,
            image_inputs: list[dict[str, str]] | None = None) -> tuple[str, str]:
        task = self.tasks.create("Chat request", task_description or prompt)
        result = self.run_existing(task, history, conversation_id,
                                   provider_id=provider_id, model=model, prompt=prompt,
                                   task_type=task_type, tools_enabled=tools_enabled,
                                   image_inputs=image_inputs)
        return result, task.id

    def run_existing(self, task, history: list[dict[str, str]] | None = None,
                     conversation_id: str | None = None, session: AgentSession | None = None,
                     provider_id: str | None = None, model: str | None = None,
                     *, prompt: str | None = None, task_type: str | None = None,
                     tools_enabled: bool = True,
                     image_inputs: list[dict[str, str]] | None = None) -> str:
        if task.status is not TaskStatus.PENDING:
            raise ValueError(f"Only pending tasks can run; current state is {task.status.value}")
        session = session or (self.sessions.create(self.agent.id, task.id, conversation_id)
                              if self.sessions else None)
        task = self.tasks.executor.execute(
            task, lambda: self._generate(prompt if prompt is not None else task.description,
                                         history or [], task, conversation_id,
                                         session, provider_id, model, tools_enabled, image_inputs,
                                         task_type)
        )
        self._sync_session(session, task)
        if task.error:
            if task.error.startswith("The model tried to use a tool, but Akaryon tools are off"):
                raise ToolsDisabledForTurnError(task.error)
            raise RuntimeError(task.error)
        return task.result or ""

    def _generate(self, prompt: str, history: list[dict[str, Any]], task,
                  conversation_id: str | None, session: AgentSession | None,
                  provider_id: str | None = None, model: str | None = None,
                  tools_enabled: bool = True,
                  image_inputs: list[dict[str, str]] | None = None,
                  task_type: str | None = None) -> str:
        tools_enabled = tools_enabled and not _user_opted_out_of_tools(prompt)
        provider, model = self.router.select(task_type=task_type,
                                             provider_id=provider_id, model=model)
        if session:
            session.model, session.provider, session.status = model, provider.provider_id, "running"
            self.sessions.save(session)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.agent.instructions},
            *history, {"role": "user", "content": prompt},
        ]
        return self._continue(messages, task, model, provider.provider_id, conversation_id, session,
                              tools_enabled=tools_enabled, image_inputs=image_inputs)

    def resume_approved(self, task, approval, tool_result: dict[str, Any]) -> str:
        task.status = TaskStatus.RUNNING
        task.completed_at = None
        task.error = None
        self.tasks._save(task)
        self.events.publish("TaskStarted", task_id=task.id, resumed=True)
        session = self.sessions.get_for_task(task.id) if self.sessions else None
        if session:
            session.status = "running"
            self.sessions.save(session)
        messages = list(approval.messages)
        messages.append({"role": "tool", "tool_call_id": approval.tool_call_id,
                         "content": json.dumps(tool_result, default=str)})
        try:
            result = self._continue(messages, task, approval.model, approval.provider,
                                    approval.conversation_id, session)
            task.result = result
            if task.status is not TaskStatus.WAITING_FOR_APPROVAL:
                task.status = TaskStatus.COMPLETED
                from datetime import datetime, timezone
                task.completed_at = datetime.now(timezone.utc)
                self.events.publish("TaskCompleted", task_id=task.id)
            self.tasks._save(task)
            self._sync_session(session, task)
            return result
        except Exception as exc:
            task.status, task.error = TaskStatus.FAILED, str(exc)
            from datetime import datetime, timezone
            task.completed_at = datetime.now(timezone.utc)
            self.tasks._save(task)
            self._sync_session(session, task)
            self.events.publish("TaskFailed", task_id=task.id, error=str(exc))
            raise

    def stream_existing(self, task, history: list[dict[str, Any]], prompt: str,
                        conversation_id: str | None, session: AgentSession,
                        provider_id: str, model: str, *, tools_enabled: bool = True,
                        image_inputs: list[dict[str, str]] | None = None):
        """Stream an agent turn while preserving the normal tool and approval loop."""
        self.agent.state = "running"
        tools_enabled = tools_enabled and not _user_opted_out_of_tools(prompt)
        self.events.publish("AgentStarted", agent_id=self.agent.id, task_id=task.id,
                            session_id=session.id)
        try:
            provider, model = self.router.select(provider_id=provider_id, model=model)
            session.model, session.provider, session.status = model, provider.provider_id, "running"
            self.sessions.save(session)
            messages = [{"role": "system", "content": self.agent.instructions},
                        *history, {"role": "user", "content": prompt}]
            for _ in range(self.max_tool_rounds + 1):
                self._raise_if_cancelled(task)
                output_limit, reservation_id = self._prepare_request_budget(
                    provider, model, session, messages,
                    self._next_output_token_limit(provider, session),
                    image_count=len(image_inputs or []))
                request = ModelRequest(messages=messages, model=model,
                                       max_output_tokens=output_limit,
                                       tools=self.tool_executor.schemas() if self.tool_executor and tools_enabled else None)
                request.image_inputs = image_inputs or []
                response_text: list[str] = []
                tool_calls = []
                try:
                    for update in provider.stream_events(request):
                        self._raise_if_cancelled(task)
                        if update.type == "text" and update.text:
                            response_text.append(update.text)
                            yield {"type": "text", "text": update.text}
                        elif update.type == "tool_calls":
                            tool_calls.extend(update.tool_calls)
                        elif update.type == "usage":
                            self._record_usage(session, provider.provider_id, model, update.usage,
                                               reservation_id=reservation_id)
                            reservation_id = None
                    if reservation_id:
                        self.usage_ledger.settle_estimate(reservation_id)
                        reservation_id = None
                except BaseException:
                    if reservation_id:
                        self.usage_ledger.settle_estimate(reservation_id)
                    raise
                if not tool_calls:
                    result = "".join(response_text)
                    if not result:
                        raise ProviderError("Model returned an empty stream")
                    task.result, task.status = result, TaskStatus.COMPLETED
                    task.completed_at = datetime.now(timezone.utc)
                    self.tasks._save(task)
                    session.status, session.result, session.error = "completed", result, None
                    self.sessions.save(session)
                    self.events.publish("TaskCompleted", task_id=task.id, session_id=session.id)
                    yield {"type": "done", "task_id": task.id, "result": result}
                    return
                if not tools_enabled:
                    raise ToolsDisabledForTurnError(
                        "The model tried to use a tool, but Akaryon tools are off for this turn. No action was run.")
                if self.tool_executor is None:
                    raise ProviderError("Provider requested a tool but tool execution is unavailable")
                if len(tool_calls) != 1:
                    raise ProviderError("Provider returned multiple tool calls; only one is supported")
                call = tool_calls[0]
                messages.append({"role": "assistant", "content": "".join(response_text) or None,
                                 "tool_calls": [{"id": call.get("id", ""), "type": "function",
                                                 "function": {"name": call["name"],
                                                              "arguments": call["arguments"]}}]})
                self._raise_if_cancelled(task)
                tool_result = self.tool_executor.execute(
                    self.agent.id, call["name"], call["arguments"], task.id, messages=messages,
                    model=model, provider=provider.provider_id, conversation_id=conversation_id,
                    tool_call_id=call.get("id", ""),
                )
                if tool_result["status"] == "approval_required":
                    approval_text = json.dumps({"status": "approval_required", **{
                        key: tool_result.get(key) for key in ("tool", "capability", "approval_id")
                    }, "task_id": task.id})
                    task.status, task.result = TaskStatus.WAITING_FOR_APPROVAL, approval_text
                    self.tasks._save(task)
                    session.status, session.result = "waiting_for_approval", approval_text
                    self.sessions.save(session)
                    self.events.publish("ApprovalRequired", task_id=task.id,
                                        session_id=session.id, tool=tool_result.get("tool"))
                    yield {"type": "approval_required", "task_id": task.id,
                           "tool": tool_result.get("tool"), "capability": tool_result.get("capability"),
                           "approval_id": tool_result.get("approval_id")}
                    return
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps(tool_result, default=str)})
            raise ProviderError("Model exceeded the maximum tool-call rounds")
        except GeneratorExit:
            self._finish_stream(task, session, TaskStatus.CANCELLED,
                                error="Client disconnected during streaming")
            raise
        except TaskCancelled:
            self._finish_stream(task, session, TaskStatus.CANCELLED,
                                error="Task cancellation requested")
            yield {"type": "cancelled", "task_id": task.id}
        except ToolsDisabledForTurnError as exc:
            self._finish_stream(task, session, TaskStatus.FAILED, error="tools_disabled")
            yield {"type": "error", "task_id": task.id, "detail": str(exc)}
        except Exception as exc:
            self._finish_stream(task, session, TaskStatus.FAILED, error="Agent streaming failed")
            logger.error("agent streaming failed", extra={"agent_id": self.agent.id,
                         "task_id": task.id, "session_id": session.id,
                         "provider": provider_id, "model": model,
                         "error_type": type(exc).__name__})
            yield {"type": "error", "task_id": task.id, "detail": "Chat stream failed"}
        finally:
            self.agent.state = "idle"
            self.events.publish("AgentFinished", agent_id=self.agent.id, task_id=task.id,
                                session_id=session.id)

    def _finish_stream(self, task, session: AgentSession, status: TaskStatus,
                       *, error: str | None = None) -> None:
        task.status, task.error = status, error
        task.completed_at = datetime.now(timezone.utc)
        self.tasks._save(task)
        session.status, session.error = status.value, error
        self.sessions.save(session)
        self.events.publish({TaskStatus.FAILED: "TaskFailed", TaskStatus.CANCELLED: "TaskCancelled"}[status],
                            task_id=task.id, session_id=session.id)

    def _continue(self, messages: list[dict[str, Any]], task, model: str,
                  provider_id: str, conversation_id: str | None,
                  session: AgentSession | None = None, *, tools_enabled: bool = True,
                  image_inputs: list[dict[str, str]] | None = None) -> str:
        self.agent.state = "running"
        self.events.publish("AgentStarted", agent_id=self.agent.id)
        started = time.monotonic()
        try:
            provider, model = self.router.select(provider_id=provider_id, model=model)
            for _ in range(self.max_tool_rounds + 1):
                self._raise_if_cancelled(task)
                output_limit, reservation_id = self._prepare_request_budget(
                    provider, model, session, messages,
                    self._next_output_token_limit(provider, session),
                    image_count=len(image_inputs or []))
                model_request = ModelRequest(messages=messages, model=model,
                                             max_output_tokens=output_limit,
                                             tools=self.tool_executor.schemas() if self.tool_executor and tools_enabled else None)
                model_request.image_inputs = image_inputs or []
                try:
                    response = provider.generate(model_request)
                    self._raise_if_cancelled(task)
                    self._record_usage(session, response.provider, response.model, response.usage,
                                       reservation_id=reservation_id)
                    reservation_id = None
                except BaseException:
                    if reservation_id:
                        self.usage_ledger.settle_estimate(reservation_id)
                    raise
                if not response.tool_calls:
                    logger.info("agent response completed", extra={
                        "agent_id": self.agent.id, "task_id": task.id,
                        "session_id": session.id if session else None,
                        "provider": response.provider, "model": response.model,
                        "duration_seconds": time.monotonic() - started,
                    })
                    return response.text
                if not tools_enabled:
                    raise ToolsDisabledForTurnError(
                        "The model tried to use a tool, but Akaryon tools are off for this turn. No action was run.")
                if self.tool_executor is None:
                    return "Tool execution is not configured."
                if len(response.tool_calls) != 1:
                    raise RuntimeError("Provider returned multiple tool calls; only one call can be approved at a time")
                messages.append({"role": "assistant", "content": response.text or None,
                                 "tool_calls": [{"id": call.get("id", ""), "type": "function",
                                                 "function": {"name": call["name"],
                                                              "arguments": call["arguments"]}}
                                                for call in response.tool_calls]})
                for call in response.tool_calls:
                    self._raise_if_cancelled(task)
                    result = self.tool_executor.execute(
                        self.agent.id, call["name"], call["arguments"], task.id,
                        messages=messages, model=response.model, provider=response.provider,
                        conversation_id=conversation_id, tool_call_id=call.get("id", ""),
                    )
                    if result["status"] == "approval_required":
                        task.status = TaskStatus.WAITING_FOR_APPROVAL
                        return json.dumps({"status": "approval_required", "tool": result["tool"],
                                           "capability": result["capability"], "approval_id": result["approval_id"],
                                           "task_id": task.id})
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                     "content": json.dumps(result, default=str)})
            raise RuntimeError("Model exceeded the maximum tool-call rounds")
        finally:
            self.agent.state = "idle"
            self.events.publish("AgentFinished", agent_id=self.agent.id)

    def _raise_if_cancelled(self, task) -> None:
        if self.tasks.is_cancel_requested(task.id):
            raise TaskCancelled("Task cancelled")

    def _next_output_token_limit(self, provider, session: AgentSession | None) -> int:
        per_request = int(getattr(provider, "max_output_tokens", getattr(provider, "max_tokens", 4096)))
        per_task = getattr(self.router, "max_output_tokens_per_task", None)
        if per_task is None or session is None:
            return per_request
        used = int((session.state or {}).get("usage", {}).get("output_tokens", 0))
        remaining = per_task - used
        if remaining <= 0:
            raise ProviderError("Per-task output token budget was reached")
        return min(per_request, remaining)

    def _record_usage(self, session: AgentSession | None, provider: str,
                      model: str, usage: dict[str, Any], *, reservation_id: str | None = None) -> None:
        monthly_limit = getattr(self.router, "max_estimated_cost_per_month_usd", None)
        if not session or not self.sessions:
            return
        if not usage:
            if monthly_limit is not None:
                if reservation_id and self.usage_ledger:
                    self.usage_ledger.settle_estimate(reservation_id)
                raise ProviderError("Monthly cost enforcement requires provider-reported usage data")
            return
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", usage.get("prompt_eval_count", 0)))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens", usage.get("eval_count", 0)))
        if (not isinstance(input_tokens, (int, float)) or isinstance(input_tokens, bool) or
                not isinstance(output_tokens, (int, float)) or isinstance(output_tokens, bool) or
                not math.isfinite(input_tokens) or not math.isfinite(output_tokens) or
                input_tokens < 0 or output_tokens < 0):
            if monthly_limit is not None:
                if reservation_id and self.usage_ledger:
                    self.usage_ledger.settle_estimate(reservation_id)
                raise ProviderError("Monthly cost enforcement requires provider-reported usage data")
            return
        state = dict(session.state or {})
        totals = dict(state.get("usage") or {})
        totals["requests"] = totals.get("requests", 0) + 1
        totals["input_tokens"] = totals.get("input_tokens", 0) + int(input_tokens)
        totals["output_tokens"] = totals.get("output_tokens", 0) + int(output_tokens)
        totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
        totals["last_provider"] = provider
        totals["last_model"] = model
        prices = getattr(self.router, "model_prices", {}).get(f"{provider}:{model}")
        if prices:
            estimated = (int(input_tokens) * prices["input_usd_per_million"] +
                         int(output_tokens) * prices["output_usd_per_million"]) / 1_000_000
            totals["estimated_cost_usd"] = round(totals.get("estimated_cost_usd", 0.0) + estimated, 8)
        if monthly_limit is not None:
            if not self.usage_ledger or not session.task_id:
                raise ProviderError("Monthly cost ledger is unavailable for this task")
            try:
                self.usage_ledger.record(session.task_id, session.id, provider, model,
                                         int(input_tokens), int(output_tokens),
                                         reservation_id=reservation_id)
            except ValueError as exc:
                raise ProviderError(str(exc)) from exc
        state["usage"] = totals
        session.state = state
        self.sessions.save(session)

    def _budget_output_token_limit(self, provider, model: str, session: AgentSession | None,
                                   messages: list[dict[str, Any]], requested: int,
                                   image_count: int = 0) -> int:
        task_limit = getattr(self.router, "max_estimated_cost_per_task_usd", None)
        month_limit = getattr(self.router, "max_estimated_cost_per_month_usd", None)
        if task_limit is None and month_limit is None:
            return requested
        if session is None:
            raise ProviderError("Estimated cost caps require a tracked agent session")
        prices = getattr(self.router, "model_prices", {}).get(f"{provider.provider_id}:{model}")
        if not prices:
            if task_limit is not None and month_limit is None:
                raise ProviderError("A per-task cost cap requires configured pricing for this provider and model")
            raise ProviderError("An estimated cost cap requires configured pricing for this provider and model")
        input_rate = prices["input_usd_per_million"]
        output_rate = prices["output_usd_per_million"]
        usage = (session.state or {}).get("usage", {})
        token_limit = requested
        # A conservative rough input estimate reserves budget before the call;
        # provider-reported token counts replace this estimate after each call.
        serialized = json.dumps(messages, ensure_ascii=False, default=str)
        estimated_input_tokens = math.ceil(len(serialized) / 4)
        estimated_input_cost = estimated_input_tokens * input_rate / 1_000_000
        image_reservation = image_count * float(getattr(
            self.router, "image_input_cost_reservation_usd", 0.0))
        budgets = []
        if task_limit is not None:
            budgets.append((task_limit - float(usage.get("estimated_cost_usd", 0.0)),
                            "Estimated per-task cost cap"))
        if month_limit is not None:
            if not self.usage_ledger:
                raise ProviderError("Monthly usage ledger is unavailable")
            budgets.append((month_limit - self.usage_ledger.month_to_date(),
                            "Estimated monthly cost cap"))
        for remaining_budget, label in budgets:
            remaining = remaining_budget - estimated_input_cost - image_reservation
            if remaining <= 0:
                raise ProviderError(f"{label} would be exceeded by the input and image reservation")
            if output_rate > 0:
                affordable_output_tokens = math.floor(remaining * 1_000_000 / output_rate)
                if affordable_output_tokens < 1:
                    raise ProviderError(f"{label} does not allow another output token")
                token_limit = min(token_limit, affordable_output_tokens)
        return token_limit

    def _prepare_request_budget(self, provider, model: str, session: AgentSession | None,
                                messages: list[dict[str, Any]], requested: int,
                                image_count: int = 0) -> tuple[int, str | None]:
        image_reservation = image_count * float(getattr(
            self.router, "image_input_cost_reservation_usd", 0.0))
        token_limit = self._budget_output_token_limit(
            provider, model, session, messages, requested, image_count=image_count)
        monthly_limit = getattr(self.router, "max_estimated_cost_per_month_usd", None)
        if monthly_limit is None:
            return token_limit, None
        if not self.usage_ledger or not session or not session.task_id:
            raise ProviderError("Monthly usage ledger is unavailable for this task")
        key = f"{provider.provider_id}:{model}"
        prices = getattr(self.router, "model_prices", {}).get(key)
        if not prices:
            raise ProviderError("An estimated cost cap requires configured pricing for this provider and model")
        estimated_input_tokens = math.ceil(len(json.dumps(messages, ensure_ascii=False, default=str)) / 4)
        try:
            return self.usage_ledger.reserve(session.task_id, session.id, provider.provider_id, model,
                                             estimated_input_tokens, token_limit, monthly_limit,
                                             extra_cost_usd=image_reservation)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc

    def _sync_session(self, session: AgentSession | None, task) -> None:
        if not session or not self.sessions:
            return
        session.status = task.status.value
        session.result = task.result
        session.error = task.error
        self.sessions.save(session)

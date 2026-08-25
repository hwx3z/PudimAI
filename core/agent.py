"""Agent Core do PudimAI — o único cérebro da plataforma.

Ciclo ReAct:

    ANALISAR -> PLANEJAR -> AGIR (tool) -> OBSERVAR -> RACIONAR ->
    AGIR DE NOVO -> VALIDAR -> RESPONDER

Garantias implementadas:
    * limite máximo de iterações (com resumo forçado ao estourar);
    * detecção de repetição de chamadas idênticas;
    * cancelamento cooperativo via TaskCancelled;
    * recuperação: erros de tool/comando voltam ao modelo como observação;
    * independente de interface (CLI hoje; Web amanhã via EventBus).
"""
from __future__ import annotations

import hashlib
import logging
from collections import deque
from dataclasses import dataclass, field

from config.settings import Settings
from core.context import ContextManager
from core.events import EventBus, EventType
from core.exceptions import LLMError, TaskCancelled
from core.llm import ChunkCallback, LLMProvider
from core.memory import Message
from core.planner import Planner, needs_planning
from tools.base import ToolContext, ToolRegistry

logger = logging.getLogger("pudimai.agent")

REPETITION_WARN_AT = 3     # 3ª chamada idêntica consecutiva -> aviso
REPETITION_STOP_AT = 5     # 5ª -> aborta o loop com resumo
MAX_STREAM_CHARS = 20_000  # trava de segurança para respostas gigantes


@dataclass
class AgentResult:
    """Resultado completo de uma tarefa executada pelo agente."""

    text: str
    completed: bool = True
    cancelled: bool = False
    iterations: int = 0
    plan: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class AgentOutcome:
    """Resultado + histórico final de mensagens para persistir."""

    result: AgentResult
    messages: list[Message]


class Agent:
    """Executor autônomo de tarefas sobre o workspace ativo."""

    def __init__(self,
                 settings: Settings,
                 llm: LLMProvider,
                 registry: ToolRegistry,
                 context: ContextManager,
                 planner: Planner,
                 bus: EventBus,
                 tool_ctx: ToolContext) -> None:
        self.settings = settings
        self.llm = llm
        self.registry = registry
        self.context = context
        self.planner = planner
        self.bus = bus
        self.tool_ctx = tool_ctx
        self._cancel_requested = False
        self._streamed_chars = 0

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        """Solicita cancelamento cooperativo (Ctrl+C na CLI, botão no Web)."""
        self._cancel_requested = True

    def run(self, task: str, prior_messages: list[Message] | None = None,
            force_plan: bool = False) -> AgentOutcome:
        """Executa uma tarefa completa e devolve resultado + histórico."""
        task = task.strip()
        self._cancel_requested = False
        self.bus.publish(EventType.AGENT_STARTED, task=task)

        plan = self._maybe_plan(task, force_plan)
        schemas = self.registry.schemas()
        system_prompt = self.context.build_system_prompt(schemas, plan=plan)

        messages: list[Message] = [{"role": "system",
                                    "content": system_prompt}]
        messages.extend(prior_messages or [])
        messages.append({"role": "user", "content": task})
        messages = self.context.trim(messages)

        result = AgentResult(text="", completed=False, plan=plan)
        recent_calls: deque[str] = deque(maxlen=8)

        try:
            for iteration in range(1, self.settings.max_iterations + 1):
                self._check_cancelled()
                result.iterations = iteration
                self.bus.publish(EventType.AGENT_ITERATION,
                                 iteration=iteration)

                response = self._chat(messages, schemas)
                self._check_cancelled()

                if response.has_tool_calls:
                    messages = self._handle_tool_calls(
                        messages, response, recent_calls, result)
                    continue

                # -------- resposta final do modelo -------- #
                if not response.content.strip():
                    messages.append({
                        "role": "user",
                        "content": ("Sua resposta veio vazia. Continue "
                                    "trabalhando na tarefa ou apresente o "
                                    "resultado.")})
                    continue

                result.text = response.content.strip()
                result.completed = True
                break
            else:
                # estourou max_iterations sem concluir
                result.text, _ = self._force_summary(messages)
                result.error = (
                    f"Limite de {self.settings.max_iterations} iterações "
                    "atingido antes da conclusão total.")

            self._repetition_stop(recent_calls, messages, result)

        except TaskCancelled:
            result.cancelled = True
            result.completed = False
            self.bus.publish(EventType.TASK_CANCELLED, task=task)
            logger.info("Tarefa cancelada: %.60s", task)
        except LLMError as exc:
            result.error = str(exc)
            result.completed = False
            self.bus.publish(EventType.AGENT_ERROR, error=str(exc))
            logger.exception("Erro de LLM durante a tarefa")
        except Exception as exc:  # noqa: BLE001 - nunca derruba a interface
            result.error = f"Erro inesperado no agente: {exc}"
            result.completed = False
            self.bus.publish(EventType.AGENT_ERROR, error=result.error)
            logger.exception("Erro inesperado no agente")

        if result.cancelled:
            result.text = result.text or \
                "Tarefa cancelada pelo usuário antes da conclusão."
        elif not result.text:
            result.text = result.error or \
                "Não foi possível produzir uma resposta."

        self._ensure_final_assistant(messages, result.text)
        self.bus.publish(EventType.AGENT_COMPLETED,
                         completed=result.completed,
                         iterations=result.iterations,
                         tools=len(result.tools_used))
        return AgentOutcome(result=result, messages=messages)

    # ------------------------------------------------------------------ #
    # Etapas internas
    # ------------------------------------------------------------------ #
    def _maybe_plan(self, task: str, force: bool) -> list[str]:
        if not (force or (self.settings.auto_plan and needs_planning(task))):
            return []
        self.bus.publish(EventType.AGENT_PLANNING, task=task)
        plan = self.planner.create_plan(task)
        if plan:
            self.bus.publish(EventType.PLAN_CREATED, plan=plan)
        else:
            logger.info("Plano vazio; seguindo sem plano explícito.")
        return plan

    def _chat(self, messages: list[Message],
              schemas: list[dict]) -> object:
        self._streamed_chars = 0
        self.bus.publish(EventType.AGENT_THINKING)
        on_chunk = self._make_chunk_callback()
        return self.llm.chat(
            messages=messages,
            tools=schemas or None,
            temperature=0.2,
            on_chunk=on_chunk,
            cancel_check=lambda: self._cancel_requested,
        )

    def _make_chunk_callback(self) -> ChunkCallback | None:
        def _on_chunk(delta: str) -> None:
            if not delta:
                return
            self._streamed_chars += len(delta)
            if self._streamed_chars <= MAX_STREAM_CHARS:
                self.bus.publish(EventType.LLM_CHUNK, text=delta)
        return _on_chunk

    def _handle_tool_calls(self, messages: list[Message], response,
                           recent_calls: deque[str], result: AgentResult
                           ) -> list[Message]:
        """Executa as ferramentas pedidas e devolve mensagens atualizadas."""
        assistant_message: Message = {
            "role": "assistant", "content": response.content or ""}
        assistant_message["tool_calls"] = [
            {"function": {"name": call.name, "arguments": call.arguments}}
            for call in response.tool_calls
        ]
        messages.append(assistant_message)

        for call in response.tool_calls:
            self._check_cancelled()
            fingerprint = hashlib.sha1(
                (call.name + repr(sorted(call.arguments.items())))
                .encode()).hexdigest()
            repeated_streak = self._streak(recent_calls, fingerprint)

            if repeated_streak + 1 >= REPETITION_WARN_AT and \
                    repeated_streak + 1 < REPETITION_STOP_AT:
                messages.append({
                    "role": "user",
                    "content": ("ATENÇÃO: você está repetindo exatamente a "
                                "mesma ação sem progresso. Mude de "
                                "abordagem ou finalize com um resumo.")})

            self.bus.publish(EventType.TOOL_STARTED,
                             name=call.name, arguments=call.arguments)
            tool_result = self.registry.execute(call.name, call.arguments,
                                                self.tool_ctx)
            result.tools_used.append(call.name)
            event_type = (EventType.TOOL_COMPLETED if tool_result.success
                          else EventType.TOOL_FAILED)
            self.bus.publish(event_type, name=call.name,
                             success=tool_result.success,
                             summary=tool_result.output[:400])

            messages.append({
                "role": "tool",
                "content": tool_result.output,
                "tool_name": call.name,
                "name": call.name,
            })

            recent_calls.append(fingerprint)
            self._check_cancelled()

        messages = self.context.trim(messages)
        return messages

    @staticmethod
    def _streak(recent_calls: deque[str], fingerprint: str) -> int:
        count = 0
        for item in reversed(recent_calls):
            if item == fingerprint:
                count += 1
            else:
                break
        return count

    @staticmethod
    def _ensure_final_assistant(messages: list[Message], text: str) -> None:
        """Registra a resposta final no histórico para conversas futuras."""
        if messages and messages[-1].get("role") == "assistant" and \
                messages[-1].get("content") == text:
            return
        messages.append({"role": "assistant", "content": text})

    def _repetition_stop(self, recent_calls: deque[str],
                         messages: list[Message], result: AgentResult) -> None:
        """Se travou em loop de repetição, encerra com resumo parcial."""
        if len(recent_calls) < REPETITION_STOP_AT:
            return
        last = recent_calls[-1]
        if all(item == last for item in list(recent_calls)[-REPETITION_STOP_AT:]):
            summary, _ = self._force_summary(messages)
            result.text = summary
            result.completed = False
            result.error = "Loop detectado: mesma ação repetida " \
                           f"{REPETITION_STOP_AT} vezes."

    def _force_summary(self, messages: list[Message]) -> tuple[str, list]:
        """Chamada final SEM ferramentas: obriga o modelo a relatar estado."""
        nudged = messages + [{
            "role": "user",
            "content": ("Encerre agora: relate em markdown o que foi "
                        "concluído, o que falta e os próximos passos. "
                        "Sem chamar ferramentas."),
        }]
        try:
            response = self.llm.chat(messages=self.context.trim(nudged),
                                     tools=None, temperature=0.2,
                                     on_chunk=self._make_chunk_callback(),
                                     cancel_check=lambda: self._cancel_requested)
            text = response.content.strip() or \
                "(sem resposta do modelo durante o resumo)"
        except LLMError as exc:
            text = f"Falha ao gerar resumo final: {exc}"
        return text, messages

    def _check_cancelled(self) -> None:
        if self._cancel_requested:
            raise TaskCancelled("Cancelamento solicitado.")

"""Planner independente do PudimAI.

Para tarefas complexas, produz um plano numerado em uma chamada única de
LLM antes do loop agir. O plano é injetado no system prompt e o agente o
segue passo a passo. Tarefas simples pulam o planejamento (latência zero).
"""
from __future__ import annotations

import json
import logging
import re

from core.llm import LLMError, LLMProvider
from core.workspace import Workspace

logger = logging.getLogger("pudimai.planner")

PLAN_SYSTEM = (
    "Você é o planejador do PudimAI. Para a tarefa informada, produza um "
    "plano curto e acionável de ATÉ 8 passos, em português. Cada passo deve "
    "começar com um verbo (Analisar, Criar, Executar, Corrigir, Validar...). "
    "Responda SOMENTE com JSON no formato {\"plan\": [\"passo 1\", ...]} "
    "e nada além disso."
)

_COMPLEX_KEYWORDS = (
    "crie", "criar", "implemente", "implementar", "refatore", "refatorar",
    "construa", "construir", "desenvolva", "adicione", "adicionar", "migre",
    "integre", "sistema", "aplicação", "aplicacao", "api ", "autenticação",
    "autenticacao", "testes para", "corrija todos",
)

_MAX_PLAN_CHARS = 4000


def needs_planning(task: str) -> bool:
    """Heurística leve: tarefas longas ou com verbos de construção."""
    lowered = task.lower()
    if len(task.split()) >= 12:
        return True
    return any(keyword in lowered for keyword in _COMPLEX_KEYWORDS) \
        and len(lowered) >= 20


class Planner:
    """Gera planos estruturados usando o mesmo provedor de LLM."""

    def __init__(self, llm: LLMProvider, workspace: Workspace) -> None:
        self._llm = llm
        self._workspace = workspace

    def create_plan(self, task: str) -> list[str]:
        prompt = (
            f"TAREFA:\n{task.strip()}\n\n"
            f"CONTEXTO DO WORKSPACE:\n{self._short_workspace()}\n\n"
            'Responda apenas: {"plan": [...]}'
        )
        try:
            response = self._llm.chat(
                messages=[{"role": "system", "content": PLAN_SYSTEM},
                          {"role": "user", "content": prompt}],
                tools=None,
                temperature=0.1,
            )
        except LLMError:
            logger.exception("Planner falhou; seguindo sem plano.")
            return []

        return self.parse_plan(response.content)

    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_plan(content: str) -> list[str]:
        """Extração robusta: aceita JSON puro ou cercado por ```json."""
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text,
                      flags=re.MULTILINE).strip()
        steps: list[str] = []
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                data = json.loads(match.group(0))
                raw = data.get("plan")
                if isinstance(raw, list):
                    steps = [str(item).strip()
                             for item in raw if str(item).strip()]
            except json.JSONDecodeError:
                steps = []
        if not steps:
            array = re.search(r"\[[\s\S]*\]", text)
            if array:
                try:
                    steps = [str(item).strip() for item in
                             json.loads(array.group(0))
                             if str(item).strip()]
                except json.JSONDecodeError:
                    steps = []
        if not steps:  # último recurso: linhas numeradas/por marcador
            for line in text.splitlines():
                stripped = line.strip()
                cleaned = re.sub(r"^\s*(?:\d+[.)]|[-*])\s+", "", stripped)
                if cleaned != stripped and len(cleaned) >= 4:
                    steps.append(cleaned)
        return Planner.dedupe(steps)[:8]

    @staticmethod
    def dedupe(steps: list[str]) -> list[str]:
        seen: set[str] = set()
        unique = []
        for step in steps:
            key = step.lower()
            if key not in seen:
                seen.add(key)
                unique.append(step)
        return unique

    def _short_workspace(self) -> str:
        summary = self._workspace.summary()
        return summary[:_MAX_PLAN_CHARS]

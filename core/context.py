"""Context Manager: monta e poda as mensagens enviadas ao LLM.

Responsabilidades:
    * system prompt com identidade, regras, workspace e plano;
    * seleção de contexto (não despeja o projeto inteiro no modelo);
    * poda por nº de mensagens e por orçamento de caracteres.
"""
from __future__ import annotations

from datetime import date

from config.settings import Settings
from core.workspace import Workspace

SYSTEM_TEMPLATE = """Você é o PudimAI, um agente autônomo de engenharia de software que roda localmente.

IDENTIDADE
- Você é um engenheiro de software sênior: analisa, planeja, executa, testa e corrige.
- Você tem acesso a ferramentas REAIS (arquivos, terminal, git, segurança) dentro do workspace.

REGRAS DE TRABALHO
1. NUNCA invente conteúdo de arquivos: leia antes de editar.
2. Prefira mudanças pequenas e verificáveis; edite apenas o necessário.
3. Depois de criar/alterar código, VALIDE: execute a aplicação ou os testes.
4. Se um comando falhar, leia stdout/stderr, entenda a causa raiz e corrija.
5. Use as ferramentas para tudo que afeta o mundo real. Não simule resultados.
6. Quando a tarefa estiver CONCLUÍDA, responda em markdown com um resumo:
   o que foi feito, arquivos alterados, como validar. Sem chamar ferramentas.
7. Se for impossível concluir, explique claramente o impedimento.

SEGURANÇA
- Você atua SOMENTE dentro do workspace informado abaixo.
- Comandos destrutivos são bloqueados pela política {security_mode}.
- Execuções sensíveis podem pedir confirmação ao usuário; não contorne.

WORKSPACE ATUAL ({today})
{workspace}

FERRAMENTAS DISPONÍVEIS
{tools}{plan_block}
Responda sempre na língua do usuário."""


class ContextManager:
    """Constrói e mantém o contexto da conversa."""

    def __init__(self, settings: Settings, workspace: Workspace) -> None:
        self.settings = settings
        self.workspace = workspace

    # ------------------------------------------------------------------ #
    def build_system_prompt(self, tool_schemas: list[dict],
                            plan: list[str] | None = None,
                            extra_rules: str = "") -> str:
        tool_lines = "\n".join(
            f"- {schema['function']['name']}: "
            f"{schema['function']['description']}"
            for schema in tool_schemas
        )
        plan_block = ""
        if plan:
            steps = "\n".join(f"  [{i}] {step}"
                              for i, step in enumerate(plan, start=1))
            plan_block = ("\n\nPLANO APROVADO PARA A TAREFA ATUAL\n"
                          f"{steps}\n"
                          "Siga o plano em ordem; marque progresso ao avançar.")
        if extra_rules:
            plan_block += f"\n\n{extra_rules}"

        return SYSTEM_TEMPLATE.format(
            security_mode=self.settings.security_mode.upper(),
            today=date.today().isoformat(),
            workspace=self.workspace.summary(),
            tools=tool_lines or "- (nenhuma)",
            plan_block=plan_block,
        )

    # ------------------------------------------------------------------ #
    def trim(self, messages: list[dict]) -> list[dict]:
        """Mantém o sistema + as mensagens mais recentes dentro do orçamento.

        Nunca remove a última mensagem nem a primeira (system).
        """
        max_messages = self.settings.context_messages
        max_chars = self.settings.context_max_chars

        def total_size(items: list[dict]) -> int:
            return sum(len(str(item.get("content") or "")) +
                       len(str(item.get("tool_calls") or ""))
                       for item in items)

        kept = messages[:]
        while len(kept) > 1 and (len(kept) > max_messages or
                                 total_size(kept) > max_chars):
            if len(kept) <= 5:  # protege o final da conversa
                break
            del kept[1]
        return kept

    @staticmethod
    def estimate_chars(messages: list[dict]) -> int:
        return sum(len(str(message.get("content") or "")) +
                   len(str(message.get("tool_calls") or ""))
                   for message in messages)

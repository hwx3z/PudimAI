"""Camada de permissões do PudimAI.

Classifica comandos de terminal em três decisões:

    AUTO  - executa sem interação (comandos de leitura/build/teste comuns)
    ASK   - pede confirmação ao usuário antes de executar
    DENY  - bloqueia incondicionalmente (destrutivo / irreversível)

O workspace é a fronteira padrão de atuação. Os modos de política são:

    relaxed  - desconhecidos executam automaticamente (exceto DENY)
    standard - desconhecidos pedem confirmação (padrão)
    strict   - apenas a allowlist explícita roda em AUTO
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Decision(Enum):
    AUTO = "AUTO"
    ASK = "ASK"
    DENY = "DENY"


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    decision: Decision
    reason: str


def _rule(pattern: str, decision: Decision, reason: str) -> Rule:
    return Rule(pattern=re.compile(pattern), decision=decision, reason=reason)


# ---------------------------------------------------------------------- #
# DENY - bloqueio incondicional
# ---------------------------------------------------------------------- #
DENY_RULES: tuple[Rule, ...] = (
    _rule(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", Decision.DENY,
          "fork bomb"),
    _rule(r"\bmkfs(\.\w+)?\b", Decision.DENY, "formatação de sistema de arquivos"),
    _rule(r"\bdd\b[^\n]*\bof=/dev/(sd|hd|nvme|vd|mmcblk)", Decision.DENY,
          "escrita direta em dispositivo de bloco"),
    _rule(r">\s*/dev/(sd|hd|nvme|vd)", Decision.DENY,
          "sobrescrita de dispositivo de bloco"),
    _rule(r"\b(shutdown|reboot|halt|poweroff|init\s+[06])\b", Decision.DENY,
          "desligamento/reinício do sistema"),
    _rule(r"\bsudo\s+(rm|dd|mkfs|shutdown|reboot|halt)\b", Decision.DENY,
          "operação destrutiva via sudo"),
    _rule(r"\brm\b[^;\n]*\s(-\w*r\w*f|\w*f\w*r\w*)\b[^;\n]*\s"
          r"(/|/\*|~|\$HOME|\"\~?/?(usr|etc|var|home|boot|bin|sbin|lib|opt|dev|proc|sys))",
          Decision.DENY, "remoção recursiva de raiz/home/diretório de sistema"),
    _rule(r"\brm\b[^;\n]*\s-\w*r\w*\s+/?$", Decision.DENY,
          "remoção recursiva da raiz"),
    _rule(r"\bchmod\s+-R\s+\S+\s*/\s*$", Decision.DENY,
          "chmod recursivo na raiz"),
    _rule(r"\bchown\s+-R\s+\S+\s+/\s*$", Decision.DENY, "chown recursivo na raiz"),
    _rule(r"(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b",
          Decision.DENY, "execução remota de script via pipe"),
)

# ---------------------------------------------------------------------- #
# ASK - exige confirmação humana
# ---------------------------------------------------------------------- #
ASK_RULES: tuple[Rule, ...] = (
    _rule(r"\brm\b", Decision.ASK, "remoção de arquivos"),
    _rule(r"\bmv\b[^;\n]*\s/dev/", Decision.ASK, "movimentação para /dev"),
    _rule(r"\b(npm|pnpm|yarn|bun)\s+(i|install|add|rm|remove|uninstall|ci|update)\b",
          Decision.ASK, "instalação/remoção de pacotes Node"),
    _rule(r"\bpip3?\s+(install|uninstall|download)\b", Decision.ASK,
          "instalação/remoção de pacotes Python"),
    _rule(r"\bapt(-get)?\s+(install|remove|purge|upgrade|dist-upgrade|autoremove)\b",
          Decision.ASK, "gerenciamento de pacotes do sistema"),
    _rule(r"\b(dnf|yum|pacman|brew)\s+(install|remove|erase|-S|-R)\b",
          Decision.ASK, "gerenciamento de pacotes do sistema"),
    _rule(r"\bcargo\s+(install|add|remove|uninstall)\b", Decision.ASK,
          "instalação de crates"),
    _rule(r"\bgem\s+(install|uninstall)\b", Decision.ASK, "instalação de gems"),
    _rule(r"\bdocker\s+(run|rm|rmi|system\s+prune|volume\s+rm|stop|kill)\b",
          Decision.ASK, "operação destrutiva no Docker"),
    _rule(r"\bgit\s+(push|reset|clean|rebase|filter-branch)\b", Decision.ASK,
          "operação que altera histórico/remoto do Git"),
    _rule(r"\bgit\s+checkout\s+--\b|\bgit\s+restore\b", Decision.ASK,
          "descarte de alterações no Git"),
    _rule(r"\b(kill|pkill|killall)\b", Decision.ASK, "encerramento de processos"),
    _rule(r"\bfind\b[^\n]*(\s-delete|\s-exec|\s-ok)\b", Decision.ASK,
          "find com deleção/execução"),
    _rule(r"\bchmod\b|\bchown\b", Decision.ASK, "alteração de permissões/dono"),
    _rule(r"\bsudo\b", Decision.ASK, "elevação de privilégios"),
    _rule(r"\b(truncate|shred|unlink)\b", Decision.ASK, "destruição de conteúdo"),
    _rule(r">\s*/etc/", Decision.ASK, "sobrescrita em /etc"),
)

# ---------------------------------------------------------------------- #
# AUTO - allowlist de leitura/build/teste (ancorada no início do comando)
# ---------------------------------------------------------------------- #
_AUTO_PREFIX = (
    r"ls( |\Z)", r"pwd( |\Z)", r"echo ", r"cat ", r"head ", r"tail ",
    r"wc ", r"file ", r"stat ", r"du ", r"df( |\Z)", r"tree( |\Z)",
    r"which ", r"whoami( |\Z)", r"date( |\Z)", r"env( |\Z)",
    r"printenv( |\Z)", r"realpath ", r"dirname ", r"basename ",
    r"sort( |\Z)", r"uniq( |\Z)", r"diff ", r"comm ", r"jq ",
    r"grep ", r"rg ", r"sed -n ", r"awk '/", r"cut -d",
    r"git status", r"git log", r"git diff", r"git show", r"git branch",
    r"git remote -v", r"git rev-parse", r"git ls-files",
    r"python3? -m pytest", r"python3? -m unittest", r"python3? -m pip list",
    r"python3? -m http.server", r"pytest", r"tox( |\Z)", r"ruff check",
    r"node --version", r"npm test", r"npm run", r"npx tsc",
    r"cargo (test|check|build|clippy)", r"go (test|build|vet|fmt)",
    r"make( |\Z)", r"cmake( |\Z)",
    r"python3? \S+", r"python3? -(c|m) ", r"node \S+\.m?js",
)
AUTO_PATTERN = re.compile(
    r"^\s*(?:env \S+=\S+\s+)*(" + "|".join(_AUTO_PREFIX) + ")"
)

_COMMAND_SPLIT = re.compile(r"[;|&]{1,2}|\$\(|`")


def evaluate(command: str,
             mode: str = "standard") -> tuple[Decision, str]:
    """Classifica um comando de shell.

    Retorna ``(Decision, motivo)``. A varredura DENY roda sobre o comando
    INTEIRO (padrões como fork bomb e `curl | sh` dependem do pipe);
    depois cada segmento separado por ``; | &`` é avaliado e prevalece
    a decisão mais restritiva.
    """
    segments = [segment.strip()
                for segment in _COMMAND_SPLIT.split(command or "")
                if segment.strip()]
    if not segments:
        return Decision.ASK, "comando vazio"

    # 1) DENY tem prioridade absoluta e enxerga o comando completo,
    #    pois o split por ; | & destruiria padrões compostos.
    for rule in DENY_RULES:
        if rule.pattern.search(command or ""):
            return rule.decision, rule.reason

    worst = Decision.AUTO
    reason = "permitido pela allowlist"

    for segment in segments:
        decision, why = _evaluate_single(segment, mode)
        if decision == Decision.DENY:
            return decision, why
        if decision == Decision.ASK and worst != Decision.DENY:
            worst, reason = Decision.ASK, why
        elif decision == Decision.AUTO and worst == Decision.AUTO:
            worst, reason = Decision.AUTO, why

    return worst, reason


def _evaluate_single(segment: str, mode: str) -> tuple[Decision, str]:
    for rule in DENY_RULES:
        if rule.pattern.search(segment):
            return rule.decision, rule.reason
    if AUTO_PATTERN.match(segment):
        return Decision.AUTO, "allowlist de comandos seguros"
    for rule in ASK_RULES:
        if rule.pattern.search(segment):
            return rule.decision, rule.reason
    if mode == "relaxed":
        return Decision.AUTO, "modo relaxed: comando não reconhecido liberado"
    if mode == "strict":
        return Decision.ASK, "modo strict: somente a allowlist executa auto"
    return Decision.ASK, "comando fora da allowlist (modo standard)"


class PermissionManager:
    """Fachada usada pelas tools para decidir/confirmar execuções."""

    def __init__(self, mode: str = "standard",
                 confirm_callback=None) -> None:
        self.mode = mode
        self.confirm_callback = confirm_callback

    def evaluate(self, command: str) -> tuple[Decision, str]:
        return evaluate(command, self.mode)

    def authorize(self, command: str) -> bool:
        """Fluxo completo: decide e, se necessário, confirma com o usuário.

        Sem callback configurado, comandos ASK são negados (falha segura).
        """
        decision, reason = self.evaluate(command)
        if decision is Decision.DENY:
            return False
        if decision is Decision.AUTO:
            return True
        if self.confirm_callback is None:
            return False
        return bool(self.confirm_callback(command, reason))

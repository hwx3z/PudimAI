"""REPL da CLI do PudimAI.

A CLI não contém inteligência: recebe um ``PudimAIRuntime`` já montado,
lê a entrada do usuário, delega tarefas ao Agent Core e assina o
EventBus para exibir o trabalho do agente em tempo real.

Comandos de barra são locais; qualquer outra entrada vai direto ao agente.
"""
from __future__ import annotations

import getpass
import logging
import signal
import threading
from pathlib import Path

from cli.ui import UI
from config.settings import Settings
from core.agent import AgentOutcome
from core.auth import AuthError
from core.events import Event, EventType
from core.exceptions import (
    ModelNotFoundError,
    OllamaUnavailableError,
    PudimAIError,
)
from core.runtime import PudimAIRuntime

logger = logging.getLogger("pudimai.cli")

VERSION = "0.4.0"

HELP_ROWS: list[list[str]] = [
    ["/help", "mostra esta ajuda"],
    ["/status", "estado atual (modelo, workspace, segurança)"],
    ["/config", "mostra as configurações ativas e suas origens"],
    ["/model [nome]", "lista ou troca o modelo do Ollama"],
    ["/workspace [caminho]", "mostra ou troca o projeto ativo"],
    ["/tools", "lista as ferramentas registradas (inclui plugins)"],
    ["/plan", "mostra o último plano; /plan on|off alterna planejamento"],
    ["/memory", "fatos memorizados; /memory add <texto>; /memory clear"],
    ["/clear", "inicia nova conversa (limpa o histórico)"],
    ["/sessions", "lista conversas salvas deste workspace"],
    ["/plugins", "plugins carregados e falhas de carga"],
    ["/login", "autentica no workspace (mesmas contas do Web)"],
    ["/logout", "encerra a sessão local"],
    ["/web", "como iniciar e acessar a interface Web"],
    ["/exit", "encerra o PudimAI"],
]


class CLI:
    """Interface de terminal construída sobre o PudimAI Core."""

    def __init__(self, runtime: PudimAIRuntime,
                 ui: UI | None = None) -> None:
        self.ui = ui or UI()
        self._attach(runtime)

    # ------------------------------------------------------------------ #
    def _attach(self, runtime: PudimAIRuntime) -> None:
        """Liga a CLI a um runtime (usado no boot e no /workspace)."""
        self.rt = runtime
        self.settings = runtime.settings
        self.workspace = runtime.workspace
        self.bus = runtime.bus
        self.registry = runtime.registry
        self.agent = runtime.default_agent
        self.session = runtime.sessions.create(
            model=self.settings.model, workspace=str(self.workspace.root))
        self.current_user: str | None = None
        self.current_token: str | None = None
        self.last_plan: list[str] = []
        self._running = True
        self._outcome: AgentOutcome | None = None
        self._ctrl_c_count = 0
        self._agent_thread: threading.Thread | None = None

        self.bus.subscribe(self._on_event)
        runtime.setup_file_logging()
        runtime.memory.update_project_info(runtime.workspace.info)

    # ------------------------------------------------------------------ #
    # Exibição dos eventos do agente (o mesmo bus servirá ao WebSocket)
    # ------------------------------------------------------------------ #
    def _on_event(self, event: Event) -> None:
        payload = event.payload
        if event.type is EventType.AGENT_STARTED:
            self.ui.dim(f"⟳ trabalhando: {str(payload.get('task', ''))[:70]}")
        elif event.type is EventType.AGENT_PLANNING:
            self.ui.dim("⟳ elaborando plano…")
        elif event.type is EventType.PLAN_CREATED:
            plan = payload.get("plan", [])
            self.last_plan = plan
            self.ui.info("Plano criado:")
            for index, step in enumerate(plan, start=1):
                self.ui.dim(f"   [{index}] {step}")
        elif event.type is EventType.TOOL_STARTED:
            args = ", ".join(f"{k}={_clip_value(v)}"
                             for k, v in payload.get("arguments", {}).items())
            self.ui.dim(f"⟳ tool {payload.get('name')}({args})")
        elif event.type is EventType.TOOL_COMPLETED:
            summary = str(payload.get("summary", "")).splitlines()
            first = summary[0][:90] if summary else ""
            if payload.get("name") == "plugin_loader":
                return  # ruído de boot não vira linha de chat
            self.ui.success(f"{payload.get('name')} ok — {first}")
        elif event.type is EventType.TOOL_FAILED:
            self.ui.error(f"{payload.get('name')} falhou — "
                          f"{str(payload.get('summary', ''))[:120]}")
        elif event.type is EventType.TOOL_DENIED:
            self.ui.warn(f"bloqueado pela segurança: "
                         f"{payload.get('command', '')[:80]} "
                         f"({payload.get('reason')})")
        elif event.type is EventType.PERMISSION_REQUIRED:
            self.ui.warn(f"permissão necessária: "
                         f"{payload.get('command', '')[:70]}")
        elif event.type is EventType.FILE_CHANGED:
            self.ui.info(f"arquivo alterado: {payload.get('path', '')}")
        elif event.type is EventType.AGENT_ERROR:
            self.ui.error(payload.get("error", "erro desconhecido"))
        elif event.type is EventType.TASK_CANCELLED:
            self.ui.warn("tarefa cancelada pelo usuário")

    def _on_llm_chunk(self, event: Event) -> None:
        if event.type is EventType.LLM_CHUNK:
            self.ui.stream_delta(event.payload.get("text", ""))

    # ------------------------------------------------------------------ #
    # Loop principal
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        try:
            installed = self.rt.llm.health_check()
            self.rt.llm.ensure_model(installed)
            models_note = f"{len(installed)} modelo(s) disponível(is)"
        except (OllamaUnavailableError, ModelNotFoundError) as exc:
            self.ui.error(str(exc))
            return 2

        self.ui.banner(self.settings.model, self.settings.ollama_url,
                       str(self.workspace.root),
                       self.settings.security_mode.upper(), VERSION)
        self.ui.dim(models_note)
        if self.rt.plugins.loaded:
            names = ", ".join(p.name for p in self.rt.plugins.loaded)
            self.ui.dim(f"plugins: {names}")

        try:
            import readline  # noqa: F401 - histórico de edição no POSIX
        except ImportError:
            pass

        while self._running:
            try:
                prefix = self.ui.prompt()
                line = input(prefix).strip()
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                self.ui.dim("(Ctrl+C novamente ou /exit para sair)")
                continue

            if not line:
                continue
            if line.startswith("/"):
                self._handle_command(line)
            else:
                self._run_task(line)

        self.ui.info("Até logo!")
        return 0

    # ------------------------------------------------------------------ #
    # Tarefas naturais -> Agent Core (único caminho, sem lógica própria)
    # ------------------------------------------------------------------ #
    def _run_task(self, task: str, force_plan: bool = False) -> None:
        if self.settings.cli_require_login and not self.current_user:
            self.ui.error("Esta instância exige login para executar "
                          "tarefas (cli_require_login=true). Use /login.")
            return
        if not self.session.title or self.session.title == "Nova conversa":
            self.session.title = task[:48]

        unsubscribe = self.bus.subscribe(self._on_llm_chunk)
        self._outcome = None
        worker = threading.Thread(
            target=self._task_worker, args=(task, force_plan), daemon=True)
        self._agent_thread = worker

        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._interrupt_during_task)
        try:
            worker.start()
            worker.join()
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            unsubscribe()
            self.ui.plain()

        outcome = self._outcome
        if outcome is None:
            return

        result = outcome.result
        self.session.messages = outcome.messages[-400:]
        self.session.model = self.settings.model
        if self.current_user:
            self.session.meta["user"] = self.current_user
        from core.sessions import SessionManager
        SessionManager.record_result(self.session, result)
        self.rt.sessions.save(self.session)

        status = ("concluída" if result.completed and not result.cancelled
                  else "cancelada" if result.cancelled else "parcial")
        icon = {"concluída": "✓", "cancelada": "⨯", "parcial": "~"}[status]
        details = [f"{result.iterations} iteração(ões)",
                   f"{len(result.tools_used)} uso(s) de ferramenta"]
        if result.error:
            details.append(result.error[:100])
        if self.ui.is_rich:
            self.ui.dim("─" * 56)
        self.ui.info(f"[{icon}] tarefa {status} · " + " · ".join(details))

    def _task_worker(self, task: str, force_plan: bool) -> None:
        try:
            prior = [message for message in self.session.messages
                     if message.get("role") in ("user", "assistant")]
            self._outcome = self.agent.run(task, prior_messages=prior,
                                           force_plan=force_plan)
        except Exception as exc:  # noqa: BLE001 - última linha de defesa
            logger.exception("Falha catastrófica na thread da tarefa")
            from core.agent import AgentResult
            self._outcome = AgentOutcome(
                result=AgentResult(text=f"Erro interno: {exc}",
                                   completed=False, error=str(exc)),
                messages=[])

    def _interrupt_during_task(self, signum, frame):  # noqa: ANN001
        """1º Ctrl+C cancela cooperativamente; 2º força saída da espera."""
        self._ctrl_c_count += 1
        self.agent.cancel()
        self.ui.warn("\ncancelando… (Ctrl+C novamente para forçar)")
        if self._ctrl_c_count >= 2:
            raise KeyboardInterrupt

    # ------------------------------------------------------------------ #
    # Comandos de barra
    # ------------------------------------------------------------------ #
    def _handle_command(self, line: str) -> None:
        parts = line[1:].split(maxsplit=1)
        name = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""

        handlers = {
            "help": lambda _: self.ui.table(["Comando", "Descrição"],
                                            HELP_ROWS),
            "h": lambda _: self.ui.table(["Comando", "Descrição"], HELP_ROWS),
            "?": lambda _: self.ui.table(["Comando", "Descrição"], HELP_ROWS),
            "clear": lambda _: self._cmd_clear(),
            "new": lambda _: self._cmd_clear(),
            "status": lambda _: self._cmd_status(),
            "config": lambda _: self._cmd_config(),
            "model": self._cmd_model,
            "workspace": self._cmd_workspace,
            "tools": lambda _: self._cmd_tools(),
            "plan": self._cmd_plan,
            "memory": self._cmd_memory,
            "sessions": lambda _: self._cmd_sessions(),
            "plugins": lambda _: self._cmd_plugins(),
            "login": self._cmd_login,
            "logout": self._cmd_logout,
            "whoami": lambda _: self._cmd_whoami(),
            "web": self._cmd_web,
            "exit": lambda _: self._stop(),
            "quit": lambda _: self._stop(),
            "q": lambda _: self._stop(),
        }
        handler = handlers.get(name)
        if handler is None:
            self.ui.error(f"Comando desconhecido: /{name}. Tente /help.")
            return
        handler(argument)

    def _stop(self) -> None:
        self._running = False

    def _cmd_clear(self) -> None:
        self.session = self.rt.sessions.create(
            title="Nova conversa", model=self.settings.model,
            workspace=str(self.workspace.root))
        self.ui.success("Nova conversa iniciada.")

    def _cmd_config(self) -> None:
        rows = [
            ["model", self.settings.model],
            ["ollama_url", self.settings.ollama_url],
            ["security_mode", self.settings.security_mode.upper()],
            ["max_iterations", str(self.settings.max_iterations)],
            ["command_timeout", f"{self.settings.command_timeout}s"],
            ["permission_timeout",
             f"{self.settings.permission_timeout}s (web)"],
            ["auto_plan", "on" if self.settings.auto_plan else "off"],
            ["context_messages", str(self.settings.context_messages)],
            ["context_max_chars", f"{self.settings.context_max_chars:,}"],
            ["web", f"{self.settings.web_host}:{self.settings.web_port}"],
            ["model_roles", ", ".join(f"{k}={v}" for k, v in
                                      sorted(self.settings.model_roles.items()))
             or "-"],
        ]
        self.ui.table(["Configuração", "Valor"], rows)
        self.ui.dim("Arquivo: "
                    + str(Settings.config_path(self.workspace.root)))

    def _cmd_status(self) -> None:
        reachable = "sim"
        models: list[str] = []
        try:
            models = self.rt.llm.list_models()
        except PudimAIError:
            reachable = "NÃO"
        rows = [
            ["Modelo", self.settings.model],
            ["Usuário", self.current_user or "- (use /login)"],
            ["Ollama", f"{self.settings.ollama_url} (acessível: {reachable})"],
            ["Workspace", str(self.workspace.root)],
            ["Stack detectada", ", ".join(self.workspace.info.languages)
             or "-"],
            ["Git", "sim" if self.workspace.info.is_git_repo else "não"],
            ["Segurança", self.settings.security_mode.upper()],
            ["Max iterações", str(self.settings.max_iterations)],
            ["Sessão atual", f"{self.session.id} · "
                             f"{len(self.session.messages)} mensagem(ns)"],
            ["Fatos memorizados", str(len(self.rt.memory.facts()))],
            ["Ferramentas", f"{len(self.registry)} registradas"],
            ["Plugins", ", ".join(p.name for p in self.rt.plugins.loaded)
             or "-"],
            ["Modelos instalados", ", ".join(models[:6]) or "-"],
        ]
        self.ui.table(["Item", "Valor"], rows)

    def _cmd_model(self, argument: str) -> None:
        try:
            models = self.rt.llm.list_models()
        except PudimAIError as exc:
            self.ui.error(str(exc))
            return
        if not argument:
            for model in models:
                marker = " ← atual" if model == self.settings.model else ""
                self.ui.plain(f"  {model}{marker}")
            self.ui.dim("/model <nome> para trocar")
            return
        if argument not in models:
            self.ui.warn(f"'{argument}' não está instalado. Instale com: "
                         f"ollama pull {argument}")
            return
        self.settings.model = argument
        self.settings.save(self.workspace.root)
        self.session.model = argument
        self.ui.success(f"Modelo alterado para {argument}")

    def _cmd_workspace(self, argument: str) -> None:
        if not argument:
            self.ui.markdown(self.workspace.summary())
            return
        path = Path(argument).expanduser().resolve()
        if not path.is_dir():
            self.ui.error(f"Diretório inválido: {argument}")
            return
        settings = Settings.load(path)
        settings.model = self.settings.model  # mantém escolha do usuário
        settings.save(path)
        # migração controlada: novo runtime = mesmo Core em outro projeto
        self._attach(PudimAIRuntime(path, settings=settings))
        self.ui.success(f"Workspace ativo: {self.workspace.root}")

    def _cmd_tools(self) -> None:
        schemas = {schema["function"]["name"]: schema["function"]
                   for schema in self.registry.schemas()}
        rows = [[name, schemas[name]["description"][:80]]
                for name in sorted(schemas)]
        self.ui.table(["Tool", "Descrição"], rows)

    def _cmd_plan(self, argument: str) -> None:
        lowered = argument.lower()
        if lowered in ("on", "off"):
            self.settings.auto_plan = lowered == "on"
            self.settings.save(self.workspace.root)
            self.ui.success(f"Planejamento automático: {lowered.upper()}")
            return
        if self.last_plan:
            self.ui.info("Último plano:")
            for index, step in enumerate(self.last_plan, start=1):
                self.ui.plain(f"  [{index}] {step}")
        else:
            self.ui.dim("Nenhum plano gerado ainda nesta sessão.")

    def _cmd_memory(self, argument: str) -> None:
        sub = argument.split(maxsplit=1)
        action = sub[0].lower() if sub else "show"
        text = sub[1].strip() if len(sub) > 1 else ""
        if action == "add":
            if not text:
                self.ui.error("Uso: /memory add <texto>")
                return
            added = self.rt.memory.remember(text)
            self.ui.success("Memorizado." if added
                            else "Este fato já estava memorizado.")
        elif action == "clear":
            removed = self.rt.memory.forget_all()
            self.ui.success(f"{removed} fato(s) removido(s).")
        else:
            facts = self.rt.memory.facts()
            if not facts:
                self.ui.dim("Nenhum fato memorizado. Use /memory add <texto>.")
                return
            self.ui.info("Fatos memorizados:")
            for fact in facts:
                self.ui.plain(f"  • {fact}")

    def _cmd_sessions(self) -> None:
        sessions = self.rt.sessions.list(limit=12)
        if not sessions:
            self.ui.dim("Nenhuma conversa salva ainda.")
            return
        rows = [[
            ("→ " if session.id == self.session.id else "") + session.id,
            session.title[:40],
            session.updated_at[:19].replace("T", " "),
            str(len(session.messages)),
        ] for session in sessions]
        self.ui.table(["ID", "Título", "Atualizada", "Msgs"], rows)

    def _cmd_plugins(self) -> None:
        loaded = [(p.name, f"v{p.version}", p.description[:50])
                  for p in self.rt.plugins.loaded]
        failed = [(name, reason[:60]) for name, reason in
                  self.rt.plugins.failed]
        if loaded:
            self.ui.table(["Plugin", "Versão", "Descrição"], loaded)
        else:
            self.ui.dim("Nenhum plugin carregado.")
        for name, reason in failed:
            self.ui.warn(f"falha ao carregar '{name}': {reason}")
        self.ui.dim("Diretórios: <workspace>/.pudimai/plugins, "
                    "<pudimai>/plugins")

    def _cmd_web(self, _: str = "") -> None:
        host = self.settings.web_host
        port = self.settings.web_port
        self.ui.info("A interface Web usa o MESMO PudimAI Core desta CLI:")
        self.ui.plain(f"  python pudim.py --web --host {host} --port {port}")
        self.ui.plain(f"  depois abra http://{host}:{port}")
        self.ui.warn("Nota: cada processo tem estado próprio por enquanto; "
                     "daemon compartilhado está no roadmap (Próxima Evolução).")

    # ------------------------------------------------------------------ #
    # Autenticação local (mesmas contas do Web — users.json do workspace)
    # ------------------------------------------------------------------ #
    def _ask_credentials(self) -> tuple[str, str] | None:
        try:
            username = input("Usuário: ").strip().lower()
            if not username:
                return None
            password = getpass.getpass("Senha: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        return username, password

    def _cmd_login(self, _: str = "") -> None:
        if self.current_user:
            self.ui.warn(f"Já autenticado como '{self.current_user}'. "
                         "Use /logout primeiro.")
            return

        credentials = self._ask_credentials()
        if credentials is None:
            return
        username, password = credentials

        try:
            self.current_token = self.rt.auth.login(username, password)
        except AuthError as exc:
            answer = input(f"{exc} — criar conta para '{username}'? [s/N] ")
            if answer.strip().lower() not in ("s", "sim", "y", "yes"):
                return
            try:
                new_password = getpass.getpass("Defina uma senha (6+): ")
                self.rt.auth.register(username, new_password)
                self.current_token = self.rt.auth.login(username,
                                                        new_password)
            except AuthError as register_error:
                self.ui.error(str(register_error))
                return
        except OSError as exc:
            self.ui.error(f"Base de usuários inacessível: {exc}")
            return

        self.current_user = username
        logger.info("auth.login user=%s via=cli workspace=%s",
                    username, self.workspace.root)
        self.ui.success(f"Autenticado como '{username}'. Ações desta "
                        "sessão ficam registradas em seu nome.")

    def _cmd_logout(self, _: str = "") -> None:
        if not self.current_user:
            self.ui.warn("Nenhuma sessão ativa nesta CLI.")
            return
        self.rt.auth.revoke(self.current_token)
        logger.info("auth.logout user=%s via=cli", self.current_user)
        self.ui.success(f"Sessão de '{self.current_user}' encerrada.")
        self.current_user = None
        self.current_token = None

    def _cmd_whoami(self) -> None:
        if self.current_user:
            self.ui.info(f"logado como: {self.current_user}")
        else:
            self.ui.dim("sem sessão ativa — use /login")


# ---------------------------------------------------------------------- #
def _clip_value(value, limit: int = 60):  # noqa: ANN001
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"

# PudimAI

**Plataforma local de engenharia de software com IA — CLI + Web sobre um único Agent Core.**

O PudimAI não é um chatbot: é a fundação de uma **AI Development Platform**.
Você entrega uma tarefa ("crie uma API FastAPI", "encontre e corrija os erros",
"analise este código em busca de vulnerabilidades") e o agente:

```text
ENTENDER → PLANEJAR → INSPECIONAR → AGIR → OBSERVAR
→ RACIONAR → CORRIGIR → TESTAR → VALIDAR → RESPONDER
```

tudo localmente, via [Ollama](https://ollama.com), sem enviar seu código para nenhum serviço externo.

---

## MVP+ — o que já funciona (v0.2)

- Agent Core único com loop ReAct — usado **igualmente pela CLI e pelo Web**
- Tool Calling estruturado (schemas JSON) sobre a API `/api/chat` do Ollama
- Tools: filesystem (ler/escrever/**editar cirúrgica**/listar/buscar), terminal,
  git (leitura) e análise defensiva de segurança
- Camada de permissões **AUTO / ASK / DENY** — na Web, o pedido ASK aparece
  como modal no navegador e é resolvido por REST, com timeout fail-safe
- Workspace-aware: detecção de stack, isolamento por caminho (anti path-traversal)
- **Web App oficial**: FastAPI + WebSocket (streaming de eventos em tempo real)
  + SPA própria dark-first (chat, histórico, atividade do agente, permissões)
- **Computador da IA** (estilo Manus): aba com explorador de arquivos,
  editor (abrir/editar/salvar com Ctrl+S) e **terminal interativo real**
  no mesmo ambiente do agente — você assume o teclado quando quiser
- **Login local**: usuários com senha hasheada (PBKDF2) + tokens; toda a API
  exige autenticação; menu de contexto nas conversas (renomear/excluir)
- **Sistema de plugins**: manifest.json + loader isolado por falha
  (`plugins/` empacotados e `<workspace>/.pudimai/plugins` por projeto)
- Session Manager compartilhado; memória persistente por projeto
- Planner independente; Event Bus único para toda interface
- Abstrações prontas: MCP (`core/mcp.py`), multi-modelo (`core/models.py`)
- Cancelamento cooperativo (Ctrl+C ou botão parar), limites de iteração,
  detecção de loops, timeouts, logging estruturado por workspace
- **72 testes automatizados**, incluindo API REST/WebSocket com LLM falso

## Arquitetura (um único cérebro)

```text
                    ┌───────────────────────┐
                    │     PUDIMAI CORE      │
                    ├───────────────────────┤
                    │ Agent (ReAct)         │
                    │ LLM Provider          │
                    │ Planner               │
                    │ Context / Memory      │
                    │ Workspace             │
                    │ Tools + Security      │
                    │ Event Bus             │
                    └───────────┬───────────┘
                                │
                      ┌─────────┴─────────┐
                      │                   │
                     CLI                 WEB
                      │                   │
                  Terminal            Browser
```

Terminal e Website compartilham o mesmo Agent Core, tools, memória e segurança.
Nenhuma interface fala direto com o LLM — apenas o Core. A CLI **não contém
inteligência**: apenas assina eventos.

### Estrutura

```text
pudimai/
├── pudim.py              # entrypoint (CLI | --web | --check)
├── core/                 # agent, llm, planner, context, memory, sessions,
│                         # workspace, events, plugins, mcp, models, runtime
├── tools/                # base/registry, filesystem, terminal, git, security
├── security/             # permissions (AUTO/ASK/DENY), analyzer (Security AI)
├── cli/                  # interface REPL + renderização
├── web/
│   ├── backend/          # FastAPI: REST + WebSocket sobre o mesmo Core
│   └── frontend/         # SPA própria (HTML/CSS/JS, sem build step)
├── plugins/              # plugins empacotados (ex.: hello) + loader
├── config/               # settings (config.json + env vars)
└── tests/                # 72 testes (unitários, integração, API+WS)
```

---

## Instalação

Requisitos: **Python 3.10+** e **[Ollama](https://ollama.com/download)**.

```bash
# 1) Dependências Python
pip install -r requirements.txt

# 2) Ollama (Linux)
curl -fsSL https://ollama.com/install.sh | sh
ollama serve          # se ainda não estiver rodando

# 3) Modelo padrão (~4,7 GB)
ollama pull qwen2.5:7b

# 4) Diagnóstico do ambiente
python pudim.py --check

# 5) Executar
python pudim.py                     # CLI (workspace = diretório atual)
python pudim.py --workspace ~/meu-projeto
python pudim.py --web               # interface Web em http://127.0.0.1:8000
python pudim.py --web --port 9000   # porta customizada
```

> **Interface Web**: as dependências `fastapi` e `uvicorn` vêm no
> `requirements.txt`. O frontend é uma SPA própria servida pelo backend
> (sem build step, sem Node). A API REST+WS está desacoplada — migrar o
> frontend para React/TS não exige nenhuma mudança no backend nem no Core.


Configuração opcional em `<workspace>/.pudimai/config.json` ou variáveis de
ambiente (`PUDIMAI_MODEL`, `PUDIMAI_OLLAMA_URL`, `PUDIMAI_MAX_ITERATIONS`,
`PUDIMAI_COMMAND_TIMEOUT`, `PUDIMAI_SECURITY_MODE`, `PUDIMAI_LOG_LEVEL`, ...).

---

## Utilização

```text
╭──────────────────────────────────────────────────────╮
│ PudimAI v0.1.0                                       │
│ Agente Autônomo de Engenharia de Software (IA local) │
│ Modelo   : qwen2.5:7b      Segurança : STANDARD      │
╰──────────────────────────────────────────────────────╯

PudimAI › analise meu projeto
PudimAI › crie um script que organize downloads por extensão e teste-o
PudimAI › encontre os erros deste projeto e corrija-os
PudimAI › analise este código em busca de vulnerabilidades
PudimAI › /help | /status | /tools | /plan | /model | /workspace | /exit
```

Durante a execução você vê o agente trabalhar em tempo real:

```text
⟳ elaborando plano…
   [1] Analisar estrutura atual do projeto
   [2] Implementar módulo de autenticação
   ...
⟳ tool escrever_arquivo(caminho=src/auth.py, …)
• arquivo alterado: /projeto/src/auth.py
⟳ tool executar_terminal(comando=pytest -q)
✓ executar_terminal ok — EXIT CODE: 0
✓ [✓] tarefa concluída · 7 iteração(ões) · 5 uso(s) de ferramenta
```

## Segurança

Toda execução de comando passa pela classificação **antes** de rodar:

| Comando                  | Decisão | Observação                          |
|--------------------------|---------|-------------------------------------|
| `pytest -q`              | AUTO    | allowlist de leitura/build/teste    |
| `pip install requests`   | ASK     | pede confirmação ao usuário         |
| `rm arquivo.txt`         | ASK     | idem                                |
| `rm -rf /`               | DENY    | bloqueado incondicionalmente        |
| `curl ... \| sh`          | DENY    | execução remota de script           |

- O **workspace é a fronteira**: path traversal é bloqueado nas tools de arquivos.
- Modos de política: `relaxed` (desconhecidos liberados), `standard` (padrão:
  desconhecidos perguntam), `strict` (só a allowlist roda em AUTO).
- A Security AI (`analisar_seguranca`) faz varredura defensiva de segredos
  expostos (chaves AWS/tokens/senhas hardcoded) no seu próprio código.
- Log de auditoria completo em `.pudimai/logs/pudimai.log`.

## Testes

```bash
pip install -r requirements-dev.txt           # httpx2 (TestClient), ruff, pytest
python3 -m unittest discover -s tests -t .    # stdlib; API Web pula se
#                                              fastapi não estiver instalado
# ou: pytest tests
ruff check .                                  # lint (nenhum erro F real)
```

Cobrem permissões, tools de FS/terminal/edição, fronteira do workspace,
memória/sessões, plugins, MCP, planner, analyzer, context manager, o **loop
do agente inteiro** (LLM falso — nenhum teste unitário exige Ollama online)
e a **API REST+WebSocket** end-to-end.

## Estendendo

- **Nova tool** → `tools/minha_tool.py` com `register(registry)` + registro em
  `tools/registry.py` (guia em `CONTRIBUTING.md`)
- **Novo plugin** → crie `plugins/meu-plugin/{manifest.json,plugin.py}` expondo
  `register(api)` e chame `api.register_tool(...)`; falhas isolam sozinhas
- **Novo provider LLM** → implemente o protocolo em `core/llm.py`, registre em
  `PROVIDERS`; multi-modelo por papel via `"model_roles"` no config.json

## Limitações conhecidas (Web)

- O terminal do "Computador da IA" é **line-based** (Enter envia, ↑↓ histórico):
  comandos e saídas funcionam normalmente, mas apps full-screen (vim, htop)
  não renderizam — para isso use um terminal real na mesma máquina.
  Desligável com `PUDIMAI_WEB_TERMINAL=0`.
- Login protege o **acesso** à instância; dados continuam por workspace
  (separação por usuário fica para a fase multiusuário).

## Próxima Evolução

1. **Daemon compartilhado** — CLI e Web conectados ao MESMO processo do Core
   (hoje: mesmo Core, processos distintos)
2. **Autenticação multiusuário** — usuários/projetos no backend Web
3. **React/TS frontend** — troca da SPA mantendo a API atual intacta
4. **MCP real** — bridges stdio/http consumindo servidores externos
5. **RAG + memória semântica**, **multi-agentes**, **Web IDE** (explorador +
   editor + terminal), **desktop app** — todos sobre este mesmo Core

## Licença

MIT — veja [LICENSE](LICENSE).

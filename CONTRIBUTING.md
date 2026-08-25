# Contribuir com o PudimAI

Obrigado pelo interesse! O PudimAI é projetado para extensão **sem reescrita**:
quase tudo que você pode querer adicionar é um módulo novo, não uma mudança no núcleo.

## Ambiente de desenvolvimento

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests -t .
python pudim.py --check
```

Padrões de código: Python moderno com type hints, docstrings nos pontos de
extensão, erros sempre convertidos em resultados estruturados (nunca derrubando
a interface), e testes para toda lógica nova. Sem `TODO`s onde deveria haver
implementação.

## Como adicionar uma ferramenta (tool)

1. Crie `tools/minha_tool.py` com uma função `register(registry)`:
   ```python
   from tools.base import ToolContext, ToolParameter, ToolResult

   def register(registry) -> None:
       registry.register_function(
           name="minha_tool",
           description="O que a ferramenta faz (o LLM lê isto).",
           parameters=[ToolParameter("entrada", "string", "Descrição.")],
           handler=_executar,
       )

   def _executar(ctx: ToolContext, entrada: str) -> ToolResult:
       ...
       return ToolResult.ok("resultado")
   ```
2. Registre em `tools/registry.py::build_default_registry`.
3. Adicione testes. Pronto — o agente já descobre e usa a tool via schema.

Regras: valide caminhos contra o workspace (`Workspace.resolve_path`),
classifique comandos pela camada de permissões, publique eventos no
`ctx.bus`, trunque saídas longas (`tools.base.clip`).

## Como adicionar um provedor de modelo

Implemente o protocolo `LLMProvider` (método `chat`) em `core/llm.py`,
registre em `PROVIDERS` e estenda `create_provider`. O Agent Core não muda.

## Reportando bugs

Abra uma issue com: comando executado, versão do Python/Ollama, trecho
relevante de `.pudimai/logs/pudimai.log` (remova segredos).

## Como adicionar um plugin

```text
plugins/meu-plugin/
├── manifest.json   {"name": "meu", "version": "1.0",
│                     "entrypoint": "plugin.py"}
└── plugin.py       def register(api): api.register_tool(...)
```

O loader varre `plugins/` (empacotados) e `<workspace>/.pudimai/plugins`
(por projeto), isola falhas e prefixa tools com `nome__tool`. Veja
`plugins/hello/` como gabarito.

## Como rodar os testes com a suíte Web incluída

A API REST/WebSocket usa `fastapi.testclient`; sem fastapi instalado, esses
testes são pulados automaticamente:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
```

## Segurança

Vulnerabilidades seguem o fluxo de [SECURITY.md](SECURITY.md).

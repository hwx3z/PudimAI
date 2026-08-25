"""Tools de sistema de arquivos do PudimAI.

Regras de segurança:
    * todo caminho passa por Workspace.resolve_path() (anti path-traversal);
    * leitura limitada por tamanho; binários são detectados, não despejados;
    * escrita cria diretórios-pai e publica evento file.changed;
    * erros retornam ToolResult estruturado (o LLM lê e se corrige).
"""
from __future__ import annotations

from pathlib import Path

from core.events import EventType
from tools.base import ToolContext, ToolParameter, ToolRegistry, ToolResult, clip


def _register(registry: ToolRegistry) -> None:
    registry.register_function(
        name="ler_arquivo",
        description="Lê o conteúdo textual de um arquivo dentro do workspace. "
                    "Use antes de editar qualquer arquivo.",
        parameters=[ToolParameter("caminho", "string",
                                  "Caminho relativo ao workspace ou absoluto "
                                  "dentro dele.")],
        handler=_read_file,
    )
    registry.register_function(
        name="escrever_arquivo",
        description="Cria ou sobrescreve um arquivo com o conteúdo informado. "
                    "Diretórios-pai são criados automaticamente.",
        parameters=[
            ToolParameter("caminho", "string",
                          "Caminho relativo ao workspace."),
            ToolParameter("conteudo", "string", "Conteúdo completo do arquivo."),
        ],
        handler=_write_file,
    )
    registry.register_function(
        name="editar_arquivo",
        description="Edita um arquivo EXISTENTE substituindo um trecho "
                    "exato por outro. 'antigo' deve ser único no arquivo; "
                    "se aparecer mais de uma vez, informe 'ocorrencia' ou "
                    "inclua mais contexto. Prefira isto a reescrever o "
                    "arquivo inteiro.",
        parameters=[
            ToolParameter("caminho", "string",
                          "Arquivo existente a editar."),
            ToolParameter("antigo", "string",
                          "Trecho exato a substituir (com indentação)."),
            ToolParameter("novo", "string", "Novo conteúdo do trecho."),
            ToolParameter("ocorrencia", "integer",
                          "Índice 1-based da ocorrência quando 'antigo' "
                          "aparece mais de uma vez.", required=False),
        ],
        handler=_edit_file,
    )
    registry.register_function(
        name="listar_diretorio",
        description="Lista arquivos e pastas de um diretório do workspace.",
        parameters=[ToolParameter("caminho", "string",
                                  "Diretório a listar ('.' para a raiz).",
                                  required=False)],
        handler=_list_dir,
    )
    registry.register_function(
        name="buscar_arquivos",
        description="Busca arquivos por nome (glob como *.py ou substring).",
        parameters=[
            ToolParameter("consulta", "string",
                          "Padrão glob ou substring do nome."),
            ToolParameter("limite", "integer",
                          "Máximo de resultados (padrão 50).", required=False),
        ],
        handler=_search_files,
    )


# ---------------------------------------------------------------------- #
def _read_file(ctx: ToolContext, caminho: str) -> ToolResult:
    workspace_root = ctx.workspace
    try:
        path = _resolve(workspace_root, caminho)
    except PermissionError as exc:
        return ToolResult.fail(str(exc))

    if not path.exists():
        return ToolResult.fail(f"Arquivo não existe: {caminho}")
    if path.is_dir():
        return ToolResult.fail(f"'{caminho}' é um diretório. "
                               f"Use listar_diretorio.")

    max_bytes = ctx.settings.context_max_chars // 2
    try:
        raw = path.read_bytes()
    except PermissionError:
        return ToolResult.fail(f"Permissão negada: {caminho}")
    except OSError as exc:
        return ToolResult.fail(f"Erro de leitura em {caminho}: {exc}")

    truncated_note = ""
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        truncated_note = f"\n...[arquivo truncado em {max_bytes} bytes]"
    if b"\x00" in raw[:1024]:
        return ToolResult.ok(
            f"[binário] {caminho} ({path.stat().st_size} bytes) — "
            "não exibido.")

    text = raw.decode("utf-8", errors="replace")
    ctx.bus.publish(EventType.FILE_READ, path=str(path))
    header = f"# {caminho} ({len(raw)} bytes)\n"
    return ToolResult.ok(header + clip(text + truncated_note))


def _write_file(ctx: ToolContext, caminho: str, conteudo: str) -> ToolResult:
    try:
        path = _resolve(ctx.workspace, caminho)
    except PermissionError as exc:
        return ToolResult.fail(str(exc))
    if path.is_dir():
        return ToolResult.fail(f"'{caminho}' é um diretório existente.")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(conteudo, encoding="utf-8")
    except PermissionError:
        return ToolResult.fail(f"Permissão negada ao escrever: {caminho}")
    except OSError as exc:
        return ToolResult.fail(f"Erro ao escrever {caminho}: {exc}")

    size = len(conteudo.encode("utf-8"))
    ctx.bus.publish(EventType.FILE_CHANGED, path=str(path), bytes=size)
    return ToolResult.ok(f"Arquivo escrito: {caminho} ({size} bytes)",
                         path=str(path), bytes=size)


def _edit_file(ctx: ToolContext, caminho: str, antigo: str, novo: str,
               ocorrencia: int | None = None) -> ToolResult:
    """Substituição exata — falha por padrão em alvos ambíguos."""
    try:
        path = _resolve(ctx.workspace, caminho)
    except PermissionError as exc:
        return ToolResult.fail(str(exc))
    if not path.exists() or path.is_dir():
        return ToolResult.fail(
            f"editar_arquivo exige arquivo existente; '{caminho}' não é um "
            "arquivo. Use ler_arquivo + escrever_arquivo para criar.")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ToolResult.fail(f"Erro ao ler {caminho}: {exc}")

    if not antigo:
        return ToolResult.fail("'antigo' não pode ser vazio.")
    count = text.count(antigo)
    if count == 0:
        snippet = clip(antigo, 160)
        return ToolResult.fail(
            f"Trecho não encontrado em {caminho}:\n---\n{snippet}\n---\n"
            "Releia o arquivo e copie o trecho EXATAMENTE.")
    if count > 1 and not ocorrencia:
        return ToolResult.fail(
            f"'antigo' aparece {count}x em {caminho}. Informe "
            "'ocorrencia' (1-based) ou inclua mais contexto para "
            "tornar o trecho único.")

    occurrence = max(1, int(ocorrencia or 1))
    if count > 1 and occurrence > count:
        return ToolResult.fail(
            f"'antigo' aparece {count}x e 'ocorrencia'={occurrence} é "
            f"inválido. Inclua mais contexto para torná-lo único.")

    index = -1
    for _ in range(occurrence):
        index = text.find(antigo, index + 1)
    updated = text[:index] + novo + text[index + len(antigo):]

    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return ToolResult.fail(f"Erro ao escrever {caminho}: {exc}")

    ctx.bus.publish(EventType.FILE_CHANGED, path=str(path),
                    bytes=len(updated.encode('utf-8')))
    note = f" (ocorrência {occurrence}/{count})" if count > 1 else ""
    return ToolResult.ok(f"Editado com sucesso{note}: {caminho}",
                         path=str(path))


def _list_dir(ctx: ToolContext, caminho: str = ".") -> ToolResult:
    try:
        path = _resolve(ctx.workspace, caminho)
    except PermissionError as exc:
        return ToolResult.fail(str(exc))
    if not path.exists():
        return ToolResult.fail(f"Diretório não existe: {caminho}")
    if not path.is_dir():
        return ToolResult.fail(f"'{caminho}' não é um diretório. "
                               f"Use ler_arquivo.")

    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
    except PermissionError:
        return ToolResult.fail(f"Permissão negada: {caminho}")

    limit = 500
    lines: list[str] = []
    for index, entry in enumerate(entries):
        if index >= limit:
            lines.append(f"...[{len(entries) - limit} entradas omitidas]")
            break
        if entry.is_dir():
            lines.append(f"{entry.name}/")
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = -1
            lines.append(f"{entry.name}  ({size} bytes)")
    body = "\n".join(lines) or "[vazio]"
    return ToolResult.ok(f"{caminho}\n{body}")


def _search_files(ctx: ToolContext, consulta: str,
                  limite: int = 50) -> ToolResult:
    # import tardio evita ciclo tools <-> core
    from core.workspace import Workspace
    workspace = Workspace(ctx.workspace)
    matches = workspace.find_files(consulta.strip(), max_results=max(1, int(limite)))
    if not matches:
        return ToolResult.ok(f"Nenhum arquivo encontrado para '{consulta}'.")
    relative = [str(match.relative_to(workspace.root)) for match in matches]
    listing = "\n".join(relative)
    note = "" if len(matches) < max(1, int(limite)) else \
        "\n...[limite atingido; refine a consulta]"
    return ToolResult.ok(f"{len(matches)} resultado(s):\n{listing}{note}")


def _resolve(root: Path, raw: str) -> Path:
    from core.workspace import Workspace
    return Workspace(root).resolve_path(raw)


def register(registry: ToolRegistry) -> None:
    """Ponto de extensão usado pelo registro padrão."""
    _register(registry)

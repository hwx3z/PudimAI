"""Workspace: representação do projeto atual.

O PudimAI nunca "adivinha" o projeto: ele inspeciona o diretório raiz,
detecta arquivos-marca (package.json, pyproject.toml, ...) e constrói um
resumo estruturado injetado no contexto do modelo.
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("pudimai.workspace")

#: Diretórios sempre ignorados em varreduras (buscas, árvore, scanner).
DEFAULT_IGNORES = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".pudimai", "dist", "build", "target", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".idea", ".vscode", "coverage", ".next", ".cache",
}

#: Arquivos que revelam a stack do projeto.
MARKER_FILES = {
    "pyproject.toml": "Python (pyproject)",
    "requirements.txt": "Python (pip)",
    "setup.py": "Python (setup.py)",
    "Pipfile": "Python (Pipenv)",
    "poetry.lock": "Python (Poetry)",
    "package.json": "Node.js",
    "tsconfig.json": "TypeScript",
    "deno.json": "Deno",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java/Gradle",
    "build.gradle.kts": "Kotlin/Gradle",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
    "docker-compose.yml": "Docker Compose",
    "Dockerfile": "Docker",
}

EXTENSION_LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "React JSX",
    ".ts": "TypeScript", ".tsx": "React TSX", ".rs": "Rust", ".go": "Go",
    ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP",
    ".c": "C", ".h": "C/C++", ".cpp": "C++", ".cs": "C#", ".sh": "Shell",
}


@dataclass
class WorkspaceInfo:
    """Instantâneo do estado do workspace."""

    root: Path
    markers: dict[str, str] = field(default_factory=dict)
    languages: list[str] = field(default_factory=list)
    is_git_repo: bool = False


class Workspace:
    """Representa o projeto ativo e produz resumos para o contexto do LLM."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._info = self.scan()

    # ------------------------------------------------------------------ #
    # Varredura
    # ------------------------------------------------------------------ #
    def scan(self) -> WorkspaceInfo:
        """(Re)varre o workspace e devolve um WorkspaceInfo."""
        markers = {name: desc for name, desc in MARKER_FILES.items()
                   if (self.root / name).exists()}
        languages: dict[str, int] = {}
        for path in self.iter_files(max_files=4000):
            lang = EXTENSION_LANGUAGES.get(path.suffix.lower())
            if lang:
                languages[lang] = languages.get(lang, 0) + 1
        ranked = [lang for lang, _ in
                  sorted(languages.items(), key=lambda kv: -kv[1])]
        return WorkspaceInfo(
            root=self.root,
            markers=markers,
            languages=ranked,
            is_git_repo=(self.root / ".git").exists(),
        )

    @property
    def info(self) -> WorkspaceInfo:
        return self._info

    def rescan(self) -> WorkspaceInfo:
        self._info = self.scan()
        return self._info

    def iter_files(self, max_files: int = 2000) -> list[Path]:
        """Lista arquivos do workspace ignorando DEFAULT_IGNORES."""
        found: list[Path] = []
        for path in self.root.rglob("*"):
            if len(found) >= max_files:
                break
            if not path.is_file():
                continue
            relative = path.relative_to(self.root)
            if any(part in DEFAULT_IGNORES or part.endswith(".egg-info")
                   for part in relative.parts):
                continue
            found.append(path)
        return found

    # ------------------------------------------------------------------ #
    # Representações textuais para o contexto do LLM
    # ------------------------------------------------------------------ #
    def tree(self, max_depth: int = 2, max_lines: int = 120) -> str:
        """Árvore ASCII do projeto (pastas primeiro, limite de linhas)."""
        lines: list[str] = [self.root.name + "/"]

        def walk(directory: Path, prefix: str, depth: int) -> None:
            if depth > max_depth or len(lines) >= max_lines:
                return
            try:
                entries = sorted(directory.iterdir(),
                                 key=lambda p: (p.is_file(), p.name))
            except OSError:
                return
            entries = [e for e in entries
                       if e.name not in DEFAULT_IGNORES
                       and not e.name.endswith(".egg-info")]
            for index, entry in enumerate(entries):
                if len(lines) >= max_lines:
                    lines.append(prefix + "...")
                    return
                connector = "`-- " if index == len(entries) - 1 else "|-- "
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"{prefix}{connector}{entry.name}{suffix}")
                if entry.is_dir():
                    walk(entry,
                         prefix + ("    " if index == len(entries) - 1
                                   else "|   "),
                         depth + 1)

        walk(self.root, "", 1)
        return "\n".join(lines)

    def summary(self) -> str:
        """Resumo textual usado no system prompt do agente."""
        info = self.info
        parts = [
            f"WORKSPACE: {self.root}",
            f"Git: {'sim' if info.is_git_repo else 'não'}",
            "Stack detectada: "
            + (", ".join(info.languages) if info.languages
               else "não identificada"),
        ]
        if info.markers:
            parts.append("Arquivos de projeto: "
                         + ", ".join(sorted(info.markers)))
        parts.append("Estrutura:\n" + self.tree())
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    # Utilitários de caminho
    # ------------------------------------------------------------------ #
    def resolve_path(self, raw: str | Path) -> Path:
        """Converte caminho relativo em absoluto DENTRO do workspace.

        Levanta PermissionError em caso de path traversal — a fronteira
        de segurança fundamental das tools de sistema de arquivos.
        """
        candidate = Path(raw).expanduser()
        absolute = (candidate if candidate.is_absolute()
                    else self.root / candidate)
        resolved = absolute.resolve()
        root_resolved = self.root.resolve()
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise PermissionError(
                f"Caminho fora do workspace é proibido: {raw} "
                f"(workspace: {root_resolved})"
            )
        return resolved

    def find_files(self, pattern: str, max_results: int = 50) -> list[Path]:
        """Busca por glob/substring dentro do workspace."""
        matches: list[Path] = []
        for path in self.iter_files(max_files=8000):
            if fnmatch.fnmatch(path.name, pattern) or \
                    pattern.lower() in path.name.lower():
                matches.append(path)
                if len(matches) >= max_results:
                    break
        return matches

    def storage_dir(self) -> Path:
        """Diretório de estado persistente (.pudimai/) deste workspace."""
        directory = self.root / ".pudimai"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

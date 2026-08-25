"""Security AI — análise estática defensiva de código (SecurityAnalyzer).

Escopo MVP: detecção de segredos expostos e riscos comuns em arquivos do
workspace. Análise autorizada e defensiva, apenas em código local.

IMPLEMENTADO AGORA : SecretScanner (regexes curadas) + relatório textual.
PREPARADO PARA O FUTURO: DependencyScanner (pip-audit/npm audit),
                     CodeAuditor (fluxo/autenticação), SecurityReport (PDF/HTML).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from core.workspace import DEFAULT_IGNORES

MAX_FILE_BYTES = 256_000
MAX_FINDINGS = 100

SCAN_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".env", ".sh", ".bash", ".rb", ".go", ".rs", ".java",
    ".php", ".txt", "",
}


@dataclass(frozen=True)
class Finding:
    file: Path
    line: int
    rule: str
    severity: str  # high | medium | low
    snippet: str


def _mask(text: str) -> str:
    text = text.strip()
    return text[:6] + "***" if len(text) > 6 else "***"


# (nome, severidade, regex compilada)
_SECRET_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("aws_access_key", "high",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", "high",
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", "high",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", "high",
     re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private_key_header", "high",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", "medium",
     re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b")),
    ("hardcoded_password", "medium",
     re.compile(
         r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*"
         r"[\"']([^\"'\s{}$]{8,})[\"']")),
    ("db_url_with_credentials", "medium",
     re.compile(r"(?i)\b\w+://[^/\s:]+:[^@\s/]+@[^\s\"']+")),
]

_PLACEHOLDER = re.compile(
    r"(?i)(example|sample|changeme|change_me|your_|xxx+|placeholder|"
    r"dummy|\{\{|\$\{|os\.environ|process\.env|getenv|<[^>]+>)")


class SecurityAnalyzer:
    """Varre arquivos do workspace procurando segredos e riscos óbvios."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ #
    def scan(self, relative_target: str = ".") -> list[Finding]:
        target = self.root / relative_target
        findings: list[Finding] = []
        files = ([target] if target.is_file()
                 else sorted(self._iter_scan_files(target)))
        for path in files:
            findings.extend(self._scan_file(path))
            if len(findings) >= MAX_FINDINGS:
                break
        return findings[:MAX_FINDINGS]

    # ------------------------------------------------------------------ #
    def _iter_scan_files(self, directory: Path):
        if not directory.exists():
            return
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root)
            if any(part in DEFAULT_IGNORES for part in relative.parts):
                continue
            suffix = path.suffix.lower()
            name_ok = path.name.startswith(".env") or path.name.endswith(
                (".env",))
            if suffix in SCAN_EXTENSIONS or name_ok:
                yield path

    def _scan_file(self, path: Path) -> list[Finding]:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return []
            raw = path.read_bytes()
        except OSError:
            return []
        if b"\x00" in raw[:1024]:  # binário
            return []
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except UnicodeDecodeError:
                return []

        findings: list[Finding] = []
        display = path.relative_to(self.root) if path.is_relative_to(
            self.root) else path
        for number, line in enumerate(text.splitlines(), start=1):
            for rule, severity, pattern in _SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                token = match.group(2) if match.lastindex and \
                    match.lastindex >= 2 else match.group(0)
                if _PLACEHOLDER.search(line):
                    continue
                snippet = line.strip()[:120]
                start = match.start()
                snippet = (snippet[:start] + _mask(token) +
                           snippet[start + len(token):])
                findings.append(Finding(file=display, line=number,
                                        rule=rule, severity=severity,
                                        snippet=snippet))
                break  # uma regra por linha evita duplicar o mesmo trecho
        return findings


def format_report(findings: list[Finding], scanned_root: Path) -> str:
    """Gera um relatório markdown simples a partir dos achados."""
    if not findings:
        return ("Relatório de segurança\n"
                f"Escopo: {scanned_root}\n\n"
                "Nenhuma exposição óbvia encontrada pelas verificações "
                "estáticas do MVP.\n")
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    lines = [
        "Relatório de segurança",
        f"Escopo: {scanned_root}",
        f"Achados: {len(findings)} "
        f"(alta: {counts['high']}, média: {counts['medium']}, "
        f"baixa: {counts['low']})",
        "",
        "| Arquivo | Linha | Regra | Severidade | Trecho |",
        "|---|---|---|---|---|",
    ]
    for finding in findings:
        lines.append(
            f"| `{finding.file}` | {finding.line} | {finding.rule} | "
            f"{finding.severity} | `{finding.snippet}` |"
        )
    lines.append("")
    lines.append("Revise cada item e mova segredos para variáveis de "
                 "ambiente ou cofre de segredos.")
    return "\n".join(lines)

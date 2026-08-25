# Política de Segurança — PudimAI

## Escopo

O PudimAI executa comandos e modifica arquivos **na máquina local do usuário**,
com o workspace como fronteira padrão. Reporte vulnerabilidades que envolvam:

- Escape da fronteira do workspace (path traversal, symlink escape)
- Bypass das políticas AUTO/ASK/DENY (`security/permissions.py`)
- Execução de comandos bloqueados sem confirmação do usuário
- Vazamento de segredos locais via logs, memória ou telemetria (não deve haver)
- Injeção de prompt que induza o agente a violar as regras de segurança

## Fora de escopo

O PudimAI é uma ferramenta de uso **autorizado e defensivo** no próprio
ambiente do usuário. Abusos por operadores legítimos com acesso local não são
vulnerabilidades da plataforma.

## Como reportar

1. Abra um relatório privado com os mantenedores (GitHub Security Advisories
   quando o repositório público existir).
2. Inclua: versão, modo de segurança configurado, passos de reprodução e
   impacto estimado.
3. Não divulgue publicamente até a correção ser lançada.

Tratamos relatórios com resposta inicial em até 7 dias.

## Boas práticas para usuários

- Prefira `security_mode=standard` ou `strict` em projetos sensíveis
- Leia o log de auditoria em `.pudimai/logs/pudimai.log`
- Nunca coloque segredos reais em arquivos varridos pelo analyzer para teste

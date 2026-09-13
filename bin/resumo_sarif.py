#!/usr/bin/env python3
"""SARIF -> resumo no painel do run, e portao opcional por severidade.

POR QUE ESTE ARQUIVO EXISTE
---------------------------
No `simulator-ccb` cada job de seguranca termina com um bloco de sessenta linhas
de `echo` montando a mesma tabela no `$GITHUB_STEP_SUMMARY`, e antes dele quatro
`python3 -c` de uma linha contando achados no mesmo JSON. Dez jobs, dez copias.
Mudar a forma da tabela e mudar dez lugares; corrigir a contagem e corrigir em
quatro lugares dentro de cada um.

Todas aquelas ferramentas — semgrep, trivy, gitleaks, checkov, scorecard, codeql —
falam **SARIF**. Entao a leitura, a contagem e a tabela sao UM problema, nao dez.
Este script e esse um.

O PORTAO
--------
`--bloquear error` faz o script sair 1 se houver achado naquele nivel ou acima.
Existe porque um scanner que so escreve relatorio nao e um controle: ele e um
aviso que todo mundo aprende a ignorar. E a saida 1 vem DEPOIS de escrever o
resumo, para o painel mostrar o que derrubou.

REPOSITORIO PRIVADO
-------------------
O upload de SARIF para a aba Security exige GitHub Advanced Security, que num
repositorio privado e pago. Sem ele o `upload-sarif` FALHA — e a tentacao e
marcar o passo com `continue-on-error`, o que devolve o pior dos mundos: nao ha
aba, nao ha resumo, e o job fica verde. Por isso este script e a fonte de
verdade do resultado, e nao um enfeite: num repositorio privado ele e a UNICA
tela onde o achado aparece, e o portao continua valendo igual.

USO
    python bin/resumo_sarif.py semgrep.sarif --titulo "SAST - Semgrep" \
        --bloquear error --explicacao "..."

Roda sozinho, sem dependencia: `python bin/resumo_sarif.py arquivo.sarif` no
terminal imprime o mesmo relatorio no stdout. E assim que se prova o portao
vermelho e verde sem abrir um pull request.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

# A ordem importa: e por ela que se decide o que esta "acima" do limiar.
NIVEIS = ["none", "note", "info", "warning", "error"]
ROTULO = {
    "error": "erro",
    "warning": "aviso",
    "note": "nota",
    "info": "info",
    "none": "-",
}


def nivel_do_resultado(resultado: dict, regras: dict) -> str:
    """Severidade de um achado, com os dois lugares onde ela pode estar.

    O SARIF permite que o resultado nao traga `level` e herde o `defaultLevel`
    da regra. Ferramenta que faz isso (o Trivy faz) aparecia como `warning`
    generico em leitor ingenuo — e um CRITICAL virava aviso.
    """
    nivel = resultado.get("level")
    if nivel:
        return nivel

    regra_id = resultado.get("ruleId") or ""
    regra = regras.get(regra_id, {})
    padrao = (regra.get("defaultConfiguration") or {}).get("level")
    if padrao:
        return padrao

    # security-severity e numerico (CVSS) e e como o CodeQL e o Trivy expressam
    # gravidade quando nao usam `level`.
    gravidade = (regra.get("properties") or {}).get("security-severity")
    try:
        nota = float(gravidade)
    except (TypeError, ValueError):
        return "warning"
    if nota >= 7.0:
        return "error"
    if nota >= 4.0:
        return "warning"
    return "note"


def local_do_resultado(resultado: dict) -> str:
    for local in resultado.get("locations") or []:
        fisico = local.get("physicalLocation") or {}
        arquivo = (fisico.get("artifactLocation") or {}).get("uri")
        if not arquivo:
            continue
        linha = (fisico.get("region") or {}).get("startLine")
        return f"{arquivo}:{linha}" if linha else arquivo
    return "-"


def texto_do_resultado(resultado: dict) -> str:
    msg = (resultado.get("message") or {}).get("text") or ""
    return " ".join(msg.split())


def ler(caminho: Path) -> tuple[list[dict], list[str]]:
    """Achados e nomes das ferramentas. Arquivo ausente nao e erro aqui.

    Quem decide se a ausencia e legitima e quem chamou: um scanner que nao
    encontrou nada as vezes nao escreve arquivo nenhum, e derrubar o job por
    isso ensina o time a desligar o portao.
    """
    if not caminho.exists():
        return [], []

    with caminho.open(encoding="utf-8-sig") as fh:
        sarif = json.load(fh)

    achados: list[dict] = []
    ferramentas: list[str] = []

    for execucao in sarif.get("runs") or []:
        driver = ((execucao.get("tool") or {}).get("driver")) or {}
        nome = driver.get("name") or "?"
        versao = driver.get("semanticVersion") or driver.get("version")
        ferramentas.append(f"{nome} {versao}" if versao else nome)

        regras = {r.get("id"): r for r in (driver.get("rules") or []) if r.get("id")}
        # Extensoes do SARIF trazem regras proprias; sem juntar, o `defaultLevel`
        # delas some e todo achado da extensao vira `warning`.
        for extensao in execucao.get("extensions") or []:
            for regra in extensao.get("rules") or []:
                if regra.get("id"):
                    regras.setdefault(regra["id"], regra)

        for resultado in execucao.get("results") or []:
            if resultado.get("suppressions"):
                continue
            achados.append(
                {
                    "nivel": nivel_do_resultado(resultado, regras),
                    "regra": resultado.get("ruleId") or "-",
                    "local": local_do_resultado(resultado),
                    "texto": texto_do_resultado(resultado),
                }
            )

    return achados, ferramentas


def montar(
    achados: list[dict],
    ferramentas: list[str],
    titulo: str,
    limiar: str | None,
    explicacao: str | None,
    arquivo_ausente: bool,
    linhas_detalhe: int,
) -> list[str]:
    contagem = Counter(a["nivel"] for a in achados)
    bloqueantes = (
        [a for a in achados if NIVEIS.index(a["nivel"]) >= NIVEIS.index(limiar)] if limiar else []
    )

    saida = [f"## {titulo}", ""]

    if arquivo_ausente:
        saida += [
            "> A ferramenta nao produziu relatorio SARIF.",
            "",
            "Isto normalmente significa varredura limpa, mas nao da para provar pelo",
            "arquivo que nao existe. Se a ferramenta deveria ter escrito um, o passo",
            "anterior falhou em silencio.",
            "",
        ]
        return saida

    if ferramentas:
        saida += [f"`{', '.join(sorted(set(ferramentas)))}`", ""]

    saida += ["| Severidade | Achados |", "|---|---|"]
    for nivel in reversed(NIVEIS):
        if contagem.get(nivel):
            marca = "**" if limiar and NIVEIS.index(nivel) >= NIVEIS.index(limiar) else ""
            saida.append(f"| {marca}{ROTULO[nivel]}{marca} | {marca}{contagem[nivel]}{marca} |")
    if not achados:
        saida.append("| - | nenhum |")
    saida.append("")

    if achados and linhas_detalhe:
        # Por gravidade e depois por regra: quem le o painel quer ver primeiro o
        # que derruba, e achados da mesma regra juntos.
        ordenados = sorted(
            achados,
            key=lambda a: (-NIVEIS.index(a["nivel"]), a["regra"], a["local"]),
        )
        saida += ["<details><summary>Achados</summary>", ""]
        saida += ["| Nivel | Regra | Onde | O que |", "|---|---|---|---|"]
        for a in ordenados[:linhas_detalhe]:
            texto = a["texto"][:160].replace("|", "\\|")
            saida.append(f"| {ROTULO[a['nivel']]} | `{a['regra']}` | `{a['local']}` | {texto} |")
        if len(ordenados) > linhas_detalhe:
            saida.append(
                f"| ... | | | mais {len(ordenados) - linhas_detalhe} achado(s) "
                "— ver o SARIF nos artefatos |"
            )
        saida += ["", "</details>", ""]

    if explicacao:
        saida += [explicacao.strip(), ""]

    if limiar:
        if bloqueantes:
            saida.append(
                f"> **Reprovado:** {len(bloqueantes)} achado(s) em `{ROTULO[limiar]}` "
                "ou acima. Este portao derruba o job."
            )
        else:
            saida.append(f"> **Aprovado:** nenhum achado em `{ROTULO[limiar]}` ou acima.")
        saida.append("")

    return saida


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sarif", type=Path, nargs="+", help="um ou mais arquivos SARIF")
    ap.add_argument("--titulo", default="Varredura")
    ap.add_argument(
        "--bloquear",
        choices=NIVEIS,
        default=None,
        help="sai 1 se houver achado neste nivel ou acima; sem isto, so relata",
    )
    ap.add_argument("--explicacao", default=None, help="paragrafo com o porque do teste")
    ap.add_argument("--detalhe", type=int, default=40, help="linhas na tabela de achados")
    args = ap.parse_args()

    todos: list[dict] = []
    ferramentas: list[str] = []
    ausentes = 0
    for caminho in args.sarif:
        achados, ferr = ler(caminho)
        if not caminho.exists():
            ausentes += 1
        todos += achados
        ferramentas += ferr

    linhas = montar(
        todos,
        ferramentas,
        args.titulo,
        args.bloquear,
        args.explicacao,
        arquivo_ausente=(ausentes == len(args.sarif)),
        linhas_detalhe=args.detalhe,
    )
    texto = "\n".join(linhas) + "\n"

    # Sempre no stdout: rodando local nao ha GITHUB_STEP_SUMMARY, e e assim que
    # se conferem as duas cores do portao antes de confiar nele.
    sys.stdout.write(texto)
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if destino:
        with open(destino, "a", encoding="utf-8") as fh:
            fh.write(texto)

    if args.bloquear:
        limite = NIVEIS.index(args.bloquear)
        bloqueantes = [a for a in todos if NIVEIS.index(a["nivel"]) >= limite]
        if bloqueantes:
            print(
                f"::error::{args.titulo}: {len(bloqueantes)} achado(s) em "
                f"{ROTULO[args.bloquear]} ou acima."
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

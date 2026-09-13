#!/usr/bin/env python3
"""Cobertura de testes: soma os relatorios, escreve o painel e aplica o piso.

POR QUE ESTE ARQUIVO EXISTE
---------------------------
No `simulator-ccb` esta conta e um bloco de sessenta linhas dentro do YAML:
bash chamando um `python3 -c` com programa embutido, devolvendo seis numeros por
posicao de linha (`sed -n '1p'`, `sed -n '2p'`, ...), depois `bc -l` para
comparar com o limiar. Nao da para rodar fora do CI, nao da para testar, e a
unica forma de saber se o portao funciona e quebrar um pull request de verdade.

Aqui e um arquivo. Roda no terminal com os XML na mao, imprime a mesma tabela e
devolve o mesmo codigo de saida — entao da para provar vermelho e verde antes de
confiar nele.

FORMATOS
--------
Um piso de cobertura so serve se existir em toda linguagem, senao vira regra de
um projeto so. Le os tres que aparecem aqui:

    cobertura.xml   coverage.py (Python), coverlet (`--collect:"XPlat Code
                    Coverage"`), gocover-cobertura (Go)
    opencover.xml   coverlet em `Format=opencover` — e o que o SonarQube .NET le
    lcov.info       node:test, jest, vitest, c8

CUIDADO QUE JA CUSTOU CARO
--------------------------
Somar as porcentagens de varios relatorios e dividir pelo numero deles da um
numero ERRADO: um projeto de 20 linhas com 100% e um de 2000 com 50% nao dao
75%. O que se soma sao as linhas — cobertas e totais — e a divisao vem no fim.
E o que este script faz, e e por isso que ele aceita varios arquivos de uma vez
em vez de pedir um merge previo.

USO
    python bin/cobertura.py coverage/**/*.xml --minimo 80
    python bin/cobertura.py TestResults/lcov.info --minimo 0   # so relata
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Self


class Soma:
    """Linhas e ramos, somados — nunca porcentagens."""

    def __init__(self) -> None:
        self.linhas_cobertas = 0
        self.linhas_total = 0
        self.ramos_cobertos = 0
        self.ramos_total = 0
        self.modulos: list[tuple[str, int, int]] = []  # nome, cobertas, total

    def __iadd__(self, outra: Soma) -> Self:
        self.linhas_cobertas += outra.linhas_cobertas
        self.linhas_total += outra.linhas_total
        self.ramos_cobertos += outra.ramos_cobertos
        self.ramos_total += outra.ramos_total
        self.modulos += outra.modulos
        return self

    @property
    def pct_linhas(self) -> float:
        return (
            100.0 * self.linhas_cobertas / self.linhas_total
            if self.linhas_total
            else 0.0
        )

    @property
    def pct_ramos(self) -> float:
        return (
            100.0 * self.ramos_cobertos / self.ramos_total if self.ramos_total else 0.0
        )


def _int(no, *chaves: str) -> int:
    for chave in chaves:
        valor = no.get(chave)
        if valor is not None:
            try:
                return int(float(valor))
            except ValueError:
                pass
    return 0


def ler_cobertura(raiz: ET.Element) -> Soma:
    s = Soma()
    s.linhas_cobertas = _int(raiz, "lines-covered")
    s.linhas_total = _int(raiz, "lines-valid")
    s.ramos_cobertos = _int(raiz, "branches-covered")
    s.ramos_total = _int(raiz, "branches-valid")

    # Nem todo gerador escreve lines-covered/lines-valid no topo; alguns so
    # trazem line-rate. Sem este resgate o total daria zero e o portao passaria
    # com cobertura nenhuma — falso verde, que e o pior resultado possivel.
    if not s.linhas_total:
        cobertas = total = 0
        for linha in raiz.iter("line"):
            total += 1
            if _int(linha, "hits") > 0:
                cobertas += 1
        s.linhas_cobertas, s.linhas_total = cobertas, total

    for pacote in raiz.iter("package"):
        nome = pacote.get("name") or "?"
        cobertas = total = 0
        for linha in pacote.iter("line"):
            total += 1
            if _int(linha, "hits") > 0:
                cobertas += 1
        if total:
            s.modulos.append((nome, cobertas, total))
    return s


def ler_opencover(raiz: ET.Element) -> Soma:
    s = Soma()
    resumo = raiz.find("Summary")
    if resumo is not None:
        s.linhas_cobertas = _int(resumo, "visitedSequencePoints")
        s.linhas_total = _int(resumo, "numSequencePoints")
        s.ramos_cobertos = _int(resumo, "visitedBranchPoints")
        s.ramos_total = _int(resumo, "numBranchPoints")

    for modulo in raiz.iter("Module"):
        if modulo.get("skippedDueTo"):
            continue
        nome = modulo.findtext("ModuleName") or "?"
        r = modulo.find("Summary")
        if r is not None and _int(r, "numSequencePoints"):
            s.modulos.append(
                (nome, _int(r, "visitedSequencePoints"), _int(r, "numSequencePoints"))
            )
    return s


def ler_lcov(texto: str) -> Soma:
    """lcov e por registro: SF: abre um arquivo, end_of_record o fecha.

    LF (linhas instrumentadas) e LH (linhas atingidas) vem sempre nessa ordem
    dentro do registro, mas nao se depende disso: os dois ficam guardados ate o
    fim do registro, quando o modulo e fechado com os dois numeros.
    """
    s = Soma()
    arquivo = None
    lf = lh = 0

    def fechar() -> None:
        nonlocal arquivo, lf, lh
        if arquivo and lf:
            s.modulos.append((arquivo, lh, lf))
        arquivo, lf, lh = None, 0, 0

    for linha in texto.splitlines():
        linha = linha.strip()
        if linha.startswith("SF:"):
            fechar()
            arquivo = linha[3:]
        elif linha.startswith("LF:"):
            lf = int(linha[3:] or 0)
            s.linhas_total += lf
        elif linha.startswith("LH:"):
            lh = int(linha[3:] or 0)
            s.linhas_cobertas += lh
        elif linha.startswith("BRF:"):
            s.ramos_total += int(linha[4:] or 0)
        elif linha.startswith("BRH:"):
            s.ramos_cobertos += int(linha[4:] or 0)
        elif linha == "end_of_record":
            fechar()
    fechar()
    return s


def ler(caminho: Path) -> Soma:
    texto = caminho.read_text(encoding="utf-8-sig", errors="replace")
    if caminho.suffix.lower() in (".info", ".lcov") or re.match(
        r"^\s*(TN:|SF:)", texto
    ):
        return ler_lcov(texto)
    raiz = ET.fromstring(texto)
    if raiz.tag == "CoverageSession":
        return ler_opencover(raiz)
    if raiz.tag == "coverage":
        return ler_cobertura(raiz)
    raise ValueError(f"{caminho}: formato nao reconhecido (raiz <{raiz.tag}>)")


def painel(s: Soma, minimo: float, arquivos: list[Path]) -> str:
    aprovado = s.pct_linhas >= minimo if minimo else True
    linhas = [
        "## Cobertura de testes",
        "",
        "| Metrica | Valor |",
        "|---|---|",
        f"| **Linhas** | **{s.pct_linhas:.2f}%** ({s.linhas_cobertas}/{s.linhas_total}) |",
    ]
    if s.ramos_total:
        linhas.append(
            f"| Ramos | {s.pct_ramos:.2f}% ({s.ramos_cobertos}/{s.ramos_total}) |"
        )
    linhas += [f"| Relatorios somados | {len(arquivos)} |", ""]

    if s.modulos:
        piores = sorted(s.modulos, key=lambda m: m[1] / m[2] if m[2] else 1.0)[:15]
        linhas += [
            "<details><summary>Por modulo (os quinze menos cobertos)</summary>",
            "",
            "| Modulo | Cobertura | Linhas |",
            "|---|---|---|",
        ]
        for nome, cobertas, total in piores:
            pct = 100.0 * cobertas / total if total else 0.0
            linhas.append(f"| `{nome}` | {pct:.1f}% | {cobertas}/{total} |")
        linhas += ["", "</details>", ""]

    if minimo:
        if aprovado:
            linhas.append(
                f"> **Aprovado:** {s.pct_linhas:.2f}% >= piso de {minimo:g}%."
            )
        else:
            falta = (minimo / 100.0 * s.linhas_total) - s.linhas_cobertas
            linhas.append(
                f"> **Reprovado:** {s.pct_linhas:.2f}% abaixo do piso de {minimo:g}%. "
                f"Faltam ~{max(1, round(falta))} linhas cobertas."
            )
    else:
        linhas.append("> Piso desligado (`--minimo 0`): este passo so relata.")
    return "\n".join(linhas) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("relatorios", type=Path, nargs="+")
    ap.add_argument("--minimo", type=float, default=0.0, help="piso em %%; 0 desliga")
    args = ap.parse_args()

    encontrados = [p for p in args.relatorios if p.exists()]
    if not encontrados:
        # NAO e sucesso silencioso. Zero relatorio com piso ligado significa que
        # os testes nao geraram cobertura — e passar verde aqui e exatamente o
        # modo de falha que o piso existe para impedir.
        print(
            "::error::Nenhum relatorio de cobertura encontrado em: "
            + ", ".join(str(p) for p in args.relatorios)
        )
        return 1 if args.minimo else 0

    total = Soma()
    for caminho in encontrados:
        try:
            total += ler(caminho)
        except Exception as erro:  # noqa: BLE001 - qualquer erro aqui e do arquivo
            print(f"::error::{caminho}: {erro}")
            return 1

    texto = painel(total, args.minimo, encontrados)
    sys.stdout.write(texto)
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if destino:
        with open(destino, "a", encoding="utf-8") as fh:
            fh.write(texto)

    if args.minimo and total.pct_linhas < args.minimo:
        print(
            f"::error::Cobertura {total.pct_linhas:.2f}% abaixo do piso de {args.minimo:g}%."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

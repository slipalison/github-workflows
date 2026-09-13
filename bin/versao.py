#!/usr/bin/env python3
"""Decide a proxima versao a partir dos commits, no padrao Conventional Commits.

POR QUE ESTE ARQUIVO EXISTE
---------------------------
Ninguem escreve numero de versao em lugar nenhum. A esteira le os commits desde
a ultima tag `vX.Y.Z` alcancavel, classifica cada um pela mensagem e decide o
salto:

    `!` no cabecalho ou rodape `BREAKING CHANGE:`   -> major
    feat                                            -> minor
    qualquer outro tipo aceito                      -> patch

O maior salto vence. Sem tag nenhuma, a primeira versao e `--inicial`, seja qual
for o conteudo dos commits. Nao ha regra especial para 0.x: uma quebra em 0.4.2
vira 1.0.0, como no git-cliff.

O QUE A ESTEIRA NAO CONSEGUE FAZER SOZINHA
------------------------------------------
Saber se uma mudanca quebra contrato. Nao existe ferramenta que descubra isso
para qualquer linguagem; o unico lugar de onde essa informacao pode vir e a
mensagem do commit. Por isso o padrao e ESTRITO: commit fora do formato REPROVA
o run, em vez de virar patch em silencio. `--sem-tipo patch` e `--sem-tipo
ignorar` afrouxam, para quem preferir.

REPROVA O QUE E NOVO, LISTA O QUE E VELHO
-----------------------------------------
Com commit direto na main, um commit ruim que ja entrou nao sai sem reescrever
historico. Entao a reprovacao vale para os commits DESTE push (ou deste pull
request), a faixa passada em --novos. Os anteriores, que ja reprovaram um run
quando entraram, aparecem nas notas como "sem tipo", nao contam para o salto e
nao travam a esteira para sempre. Sem --novos, todos os commits desde a tag
contam como novos.

O MESMO CODIGO NO HOOK E NA ESTEIRA
-----------------------------------
`conferir` recebe o arquivo da mensagem e e o que hooks/commit-msg chama. A
regra que reprova na maquina de quem commita e a mesma que reprova no CI, e
esta escrita uma vez so.

USO
    python bin/versao.py calcular [--novos antes..depois] [--sem-tipo reprovar|patch|ignorar]
                                  [--inicial 1.0.0] [--notas notas.md] [--esperada 1.4.0]
    python bin/versao.py conferir .git/COMMIT_EDITMSG

`calcular` escreve versao, tag, anterior, salto, commits e sem_tipo em
$GITHUB_OUTPUT quando ele existe, e o painel em $GITHUB_STEP_SUMMARY. Mesmo
quando reprova: o painel e o que explica a reprovacao.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Os tipos do config-conventional do commitlint. Um tipo fora desta lista conta
# como "sem tipo": `feta:` e erro de digitacao, nao um tipo novo.
TIPOS = (
    "feat",
    "fix",
    "perf",
    "refactor",
    "docs",
    "test",
    "build",
    "ci",
    "chore",
    "style",
    "revert",
)

# Cabecalho: tipo, escopo opcional entre parenteses, `!` opcional, dois pontos,
# UM espaco, assunto.
CABECALHO = re.compile(
    r"^(?P<tipo>[a-z]+)(?:\((?P<escopo>[^()]+)\))?(?P<quebra>!)?: (?P<descricao>\S.*)$"
)
# A especificacao exige maiusculas e aceita as duas grafias.
RODAPE_QUEBRA = re.compile(r"^BREAKING[ -]CHANGE: ", re.MULTILINE)
# Mensagens que o proprio git escreve. Nenhum linter de commit as reprova, e
# `git revert` sem editar a mensagem tem de continuar funcionando.
AUTOMATICAS = ("Merge ", 'Revert "', "fixup! ", "squash! ")
# Linha de tesoura do `git commit -v`: dali para baixo e o diff, nao a mensagem.
TESOURA = "# ------------------------ >8 ------------------------"
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
VERSAO = re.compile(r"^\d+\.\d+\.\d+$")
PESO = {"nenhum": 0, "patch": 1, "minor": 2, "major": 3}
ORDEM_DAS_SECOES = (
    "Quebras de compatibilidade",
    "Novidades",
    "Correções",
    "Manutenção",
    "Sem tipo",
)


@dataclass
class Commit:
    sha: str
    assunto: str
    descricao: str
    tipo: str | None
    escopo: str | None
    quebra: bool
    novo: bool = True

    @property
    def convencional(self) -> bool:
        return self.tipo is not None

    @property
    def salto(self) -> str:
        if not self.convencional:
            return "nenhum"
        if self.quebra:
            return "major"
        if self.tipo == "feat":
            return "minor"
        return "patch"

    @property
    def secao(self) -> str:
        if not self.convencional:
            return "Sem tipo"
        if self.quebra:
            return "Quebras de compatibilidade"
        if self.tipo == "feat":
            return "Novidades"
        if self.tipo in ("fix", "perf"):
            return "Correções"
        return "Manutenção"


def classificar(assunto: str, corpo: str = "") -> tuple[str, str | None, bool, str] | None:
    """(tipo, escopo, quebra, descricao) se a mensagem segue o padrao; None se nao."""
    if assunto.startswith('Revert "'):
        return "revert", None, False, assunto
    m = CABECALHO.match(assunto)
    if not m or m["tipo"] not in TIPOS:
        return None
    quebra = bool(m["quebra"]) or bool(RODAPE_QUEBRA.search(corpo))
    return m["tipo"], m["escopo"], quebra, m["descricao"]


def git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", cwd=cwd)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} falhou: {r.stderr.strip()}")
    return r.stdout


def ultima_tag(cwd: Path | None = None) -> tuple[str, tuple[int, int, int]] | None:
    """A MAIOR tag vX.Y.Z alcancavel de HEAD, comparada como numero.

    `git describe` devolveria a mais PROXIMA, e ordenar como texto poria v1.9.0
    acima de v1.10.0. Tags que nao sao versao completa (`v1`, a tag movel de
    major) ficam de fora.
    """
    candidatas = []
    for linha in git("tag", "--merged", "HEAD", "--list", "v*", cwd=cwd).splitlines():
        nome = linha.strip()
        m = TAG.match(nome)
        if m:
            candidatas.append(((int(m[1]), int(m[2]), int(m[3])), nome))
    if not candidatas:
        return None
    numeros, nome = max(candidatas)
    return nome, numeros


def commits(faixa: str, cwd: Path | None = None) -> list[Commit]:
    """Commits da faixa, sem merges, do mais antigo ao mais novo."""
    bruto = git("log", "--no-merges", "--reverse", "--format=%H%x00%s%x00%b%x01", faixa, cwd=cwd)
    lista: list[Commit] = []
    for registro in bruto.split("\x01"):
        registro = registro.strip("\n")
        if not registro.strip():
            continue
        sha, assunto, corpo = registro.split("\x00", 2)
        classe = classificar(assunto, corpo)
        if classe is None:
            lista.append(Commit(sha, assunto, assunto, None, None, False))
        else:
            tipo, escopo, quebra, descricao = classe
            lista.append(Commit(sha, assunto, descricao, tipo, escopo, quebra))
    return lista


def proxima(anterior: tuple[int, int, int], salto: str) -> str:
    maior, menor, correcao = anterior
    if salto == "major":
        return f"{maior + 1}.0.0"
    if salto == "minor":
        return f"{maior}.{menor + 1}.0"
    if salto == "patch":
        return f"{maior}.{menor}.{correcao + 1}"
    return f"{maior}.{menor}.{correcao}"


def notas(versao: str, tag_anterior: str | None, lista: list[Commit]) -> str:
    linhas = [f"## {versao}", ""]
    if tag_anterior:
        linhas.append(f"Desde `{tag_anterior}`: {len(lista)} commit(s).")
    else:
        linhas.append(f"Primeira versão: {len(lista)} commit(s) desde o início do repositório.")
    grupos: dict[str, list[Commit]] = {}
    for c in lista:
        grupos.setdefault(c.secao, []).append(c)
    for nome in ORDEM_DAS_SECOES:
        if nome not in grupos:
            continue
        linhas += ["", f"### {nome}", ""]
        for c in grupos[nome]:
            escopo = f"**{c.escopo}**: " if c.escopo else ""
            linhas.append(f"- {escopo}{c.descricao} ({c.sha[:7]})")
    return "\n".join(linhas) + "\n"


def anexar(variavel: str, texto: str) -> None:
    caminho = os.environ.get(variavel)
    if caminho:
        with open(caminho, "a", encoding="utf-8") as f:
            f.write(texto)


def calcular(args: argparse.Namespace) -> int:
    if not VERSAO.match(args.inicial):
        print(f"::error::--inicial precisa ser X.Y.Z; recebi {args.inicial!r}")
        return 1

    tag = ultima_tag(args.repo)
    lista = commits(f"{tag[0]}..HEAD" if tag else "HEAD", args.repo)
    if args.novos:
        novos = set(git("rev-list", "--no-merges", args.novos, cwd=args.repo).split())
        for c in lista:
            c.novo = c.sha in novos

    sem_tipo = [c for c in lista if not c.convencional]
    salto = max((c.salto for c in lista), key=PESO.__getitem__, default="nenhum")
    if args.sem_tipo == "patch" and sem_tipo:
        salto = max(salto, "patch", key=PESO.__getitem__)

    if tag is None:
        anterior, versao, salto = "", args.inicial, "inicial"
    else:
        anterior, versao = tag[0], proxima(tag[1], salto)

    reprovados = [c for c in sem_tipo if c.novo] if args.sem_tipo == "reprovar" else []
    divergiu = bool(args.esperada) and args.esperada != versao

    # Saidas e painel ANTES de reprovar: e o painel que explica a reprovacao.
    anexar(
        "GITHUB_OUTPUT",
        f"versao={versao}\ntag=v{versao}\nanterior={anterior}\nsalto={salto}\n"
        f"commits={len(lista)}\nsem_tipo={len(sem_tipo)}\n",
    )
    texto_notas = notas(versao, anterior or None, lista)
    if args.notas:
        Path(args.notas).write_text(texto_notas, encoding="utf-8", newline="\n")

    painel = [
        "## Versão",
        "",
        "| | |",
        "|---|---|",
        f"| Anterior | {anterior or 'nenhuma tag'} |",
        f"| Salto | {salto} |",
        f"| Próxima | **{versao}** |",
        f"| Commits | {len(lista)} ({len(sem_tipo)} sem tipo) |",
        "",
    ]
    if reprovados:
        painel += [
            "### Reprovado: commit fora do Conventional Commits",
            "",
            *(f"- `{c.sha[:7]}` {c.assunto}" for c in reprovados),
            "",
            "Formato: `tipo(escopo)!: assunto`. Tipos aceitos: " + ", ".join(TIPOS) + ".",
            "",
        ]
    if divergiu:
        painel += [f"### Divergência: esperava {args.esperada}, calculei {versao}", ""]
    anexar("GITHUB_STEP_SUMMARY", "\n".join(painel) + "\n" + texto_notas)

    print(f"anterior={anterior or '(nenhuma)'} salto={salto} versao={versao}")
    print(f"{len(lista)} commit(s), {len(sem_tipo)} sem tipo")
    for c in reprovados:
        print(f'::error::Commit fora do Conventional Commits: {c.sha[:7]} "{c.assunto}"')
    if reprovados:
        print(
            "::error::Formato: tipo(escopo)!: assunto. Tipos aceitos: "
            + ", ".join(TIPOS)
            + ". Para afrouxar: commits_sem_tipo = patch ou ignorar."
        )
    if divergiu:
        print(
            f"::error::Esperava {args.esperada} e calculei {versao}. Entrou tag ou commit "
            "entre o calculo e a publicacao; a esteira precisa rodar de novo."
        )
    return 1 if reprovados or divergiu else 0


def conferir(args: argparse.Namespace) -> int:
    texto = Path(args.arquivo).read_text(encoding="utf-8", errors="replace")
    linhas = []
    for linha in texto.splitlines():
        if linha.startswith(TESOURA):
            break
        if linha.startswith("#"):
            continue
        linhas.append(linha)
    mensagem = "\n".join(linhas).strip("\n")
    if not mensagem.strip():
        print("commit-msg: mensagem vazia.", file=sys.stderr)
        return 1
    cabecalho, _, corpo = mensagem.partition("\n")
    if cabecalho.startswith(AUTOMATICAS) or classificar(cabecalho, corpo) is not None:
        return 0
    print(
        f'commit-msg: fora do Conventional Commits: "{cabecalho}"\n'
        "  formato:  tipo(escopo)!: assunto\n"
        "  tipos:    " + ", ".join(TIPOS) + "\n"
        "  exemplos: feat(api): aceitar filtro por data\n"
        "            fix: nao perder o cabecalho no retry\n"
        "            feat!: remover o parametro legado\n"
        "  A esteira reprova o push com a mesma regra. Pular uma vez: --no-verify",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="comando", required=True)

    c = sub.add_parser("calcular", help="proxima versao a partir dos commits desde a ultima tag")
    c.add_argument("--novos", default="", help="faixa antes..depois dos commits deste push ou PR")
    c.add_argument("--sem-tipo", choices=("reprovar", "patch", "ignorar"), default="reprovar")
    c.add_argument("--inicial", default="1.0.0", help="primeira versao, quando nao ha tag")
    c.add_argument("--notas", default="", help="arquivo Markdown para as notas da versao")
    c.add_argument("--esperada", default="", help="reprova se a versao calculada for outra")
    c.add_argument(
        "--repo", type=Path, default=None, help="diretorio do repositorio (padrao: atual)"
    )
    c.set_defaults(funcao=calcular)

    v = sub.add_parser("conferir", help="valida a mensagem de um commit (hook commit-msg)")
    v.add_argument("arquivo", help="arquivo com a mensagem, como .git/COMMIT_EDITMSG")
    v.set_defaults(funcao=conferir)

    args = ap.parse_args()
    return args.funcao(args)


if __name__ == "__main__":
    sys.exit(main())

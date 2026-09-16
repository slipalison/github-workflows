#!/usr/bin/env python3
"""Prepara os repositorios de uma vez: chave de deploy, secrets e conferencia.

POR QUE ESTE ARQUIVO EXISTE
---------------------------
O `semear_secret.py` grava um secret muito bem, mas nao sabe de onde o valor
veio. Colocar um repositorio novo no ar exigia uma sequencia de cinco comandos
em duas linguagens de shell — e a primeira tentativa em PowerShell tropecou nas
duas coisas que bash e PowerShell escrevem diferente: `chmod`, que nao existe no
Windows, e `-N ""`, que o PowerShell entrega ao `ssh-keygen` como uma passphrase
literal de dois caracteres. O resultado e uma chave com senha que o Actions nao
consegue usar, falhando la na frente com um erro que nao diz isso.

Aqui a sequencia inteira e um comando, e roda igual nos dois.

ELE NAO REIMPLEMENTA O SEMEADOR
-------------------------------
A gravacao continua inteira no `semear_secret.py`, chamado como subprocesso. E
de la que vem a descoberta de quem consome cada secret, a impressao digital, a
validacao na origem e a regra de o valor ir por STDIN. Duas copias dessa logica
seria uma para esquecer de corrigir.

O SEGREDO NUNCA VEM POR ARGUMENTO
---------------------------------
`argv` e legivel por qualquer processo da maquina. Entao nao ha
`--sonar-token <valor>`: o valor vem do ambiente, de um arquivo, ou e digitado
sem eco. Os parametros sao para as ESCOLHAS, nao para os segredos.

USO
    # tudo: gera a chave, sobe como deploy key, grava os dois secrets, confere
    python bin/preparar_repositorios.py --tudo

    # so os secrets, com a chave que ja existe
    python bin/preparar_repositorios.py --secrets --chave C:/Users/eu/.ssh/gitops_ci

    # so a chave de deploy
    python bin/preparar_repositorios.py --chave-nova

    # o que falta, sem gravar nada
    python bin/preparar_repositorios.py --conferir
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

AQUI = Path(__file__).resolve().parent
SEMEADOR = AQUI / "semear_secret.py"

NOME_DE_CONTA = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPO_COMPLETO = re.compile(r"^[A-Za-z0-9][\w.-]{0,99}/[A-Za-z0-9][\w.-]{0,99}$")


def roda(*args: str, entrada: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), input=entrada, capture_output=True, text=True, encoding="utf-8"
    )


def exige_ferramentas() -> None:
    """Falha cedo se faltar `gh` ou `ssh-keygen`.

    Descobrir que o `ssh-keygen` nao esta no PATH depois de ja ter gravado o
    token do Sonar em dezenas de repositorios seria descobrir tarde.
    """
    faltando = [f for f in ("gh", "ssh-keygen") if shutil.which(f) is None]
    if faltando:
        raise SystemExit(
            f"::error::nao encontrei no PATH: {', '.join(faltando)}. "
            "O `gh` vem do GitHub CLI; o `ssh-keygen` vem do OpenSSH "
            "(no Windows, Configuracoes > Recursos opcionais > Cliente OpenSSH)."
        )
    if roda("gh", "auth", "status").returncode != 0:
        raise SystemExit("::error::`gh` sem sessao. Rode `gh auth login` antes.")


def chave_de_deploy(gitops_repo: str, destino: Path, titulo: str) -> Path:
    """Gera um par ed25519 e sobe a publica com ESCRITA. Devolve a privada.

    `-N ""` e passado como argumento de lista, e nao por linha de shell: e
    exatamente aqui que o PowerShell transformava a senha vazia em `""` literal.
    Sem shell no meio, o problema deixa de existir nos dois sistemas.
    """
    privada = destino / "gitops_ci"
    if privada.exists():
        raise SystemExit(f"::error::{privada} ja existe; nao vou sobrescrever uma chave.")

    print(f"Gerando chave ed25519 em {privada} ...")
    r = roda("ssh-keygen", "-t", "ed25519", "-N", "", "-C", titulo, "-f", str(privada), "-q")
    if r.returncode != 0:
        raise SystemExit(f"::error::ssh-keygen falhou: {r.stderr.strip()}")

    print(f"Subindo a publica como deploy key COM ESCRITA em {gitops_repo} ...")
    # `--allow-write` e o passo que mais se esquece. Sem ele a chave entra, o
    # checkout funciona, e o `git push` falha no fim com um erro que nao diz
    # "falta permissao".
    r = roda(
        "gh",
        "repo",
        "deploy-key",
        "add",
        str(privada) + ".pub",
        "--repo",
        gitops_repo,
        "--title",
        titulo,
        "--allow-write",
    )
    if r.returncode != 0:
        raise SystemExit(f"::error::gh repo deploy-key add falhou: {r.stderr.strip()}")
    print("  ok: deploy key criada com escrita.")
    return privada


def le_segredo(nome: str, arquivo: Path | None) -> str:
    """Do arquivo, do ambiente, ou digitado sem eco. Nunca de `argv`."""
    if arquivo:
        # strip: editor quase sempre deixa uma quebra de linha no fim, e um
        # token com \\n colado atras e um token diferente — que a origem recusa
        # sem explicar por que.
        valor = arquivo.read_text(encoding="utf-8").strip()
        print(f"`{nome}` lido de {arquivo}.")
    elif os.environ.get(nome):
        valor = os.environ[nome].strip()
        print(f"`{nome}` lido do ambiente.")
    elif sys.stdin.isatty():
        valor = getpass.getpass(f"Valor de {nome} (nao aparece na tela): ").strip()
    else:
        raise SystemExit(
            f"::error::sem terminal para pedir `{nome}`. Use a variavel de "
            f"ambiente {nome} ou --sonar-token-arquivo."
        )
    if not valor:
        raise SystemExit(f"::error::`{nome}` vazio.")
    return valor


def semeia(pasta: Path, secrets: list[str], dono: str, todos: bool) -> int:
    """Chama o semeador. A gravacao mora la, e so la."""
    cmd = [sys.executable, str(SEMEADOR), "--aplicar", "--de-diretorio", str(pasta)]
    for s in secrets:
        cmd += ["--secret", s]
    cmd += ["--dono", dono]
    if todos:
        # `--sim` porque a confirmacao ja foi dada ao rodar este script com
        # --todos; perguntar duas vezes ensina a responder sem ler.
        cmd += ["--todos", "--sim"]
    print(f"\n$ {' '.join(cmd[1:])}\n", flush=True)
    return subprocess.run(cmd).returncode


def confere(dono: str) -> int:
    cmd = [sys.executable, str(SEMEADOR), "--conferir", "--dono", dono]
    print(f"\n$ {' '.join(cmd[1:])}\n", flush=True)
    return subprocess.run(cmd).returncode


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    acao = ap.add_mutually_exclusive_group(required=True)
    acao.add_argument("--tudo", action="store_true", help="chave + secrets + conferencia")
    acao.add_argument("--chave-nova", action="store_true", help="so a chave de deploy")
    acao.add_argument("--secrets", action="store_true", help="so os secrets")
    acao.add_argument("--conferir", action="store_true", help="so o relatorio, sem gravar")

    ap.add_argument("--dono", default="slipalison")
    ap.add_argument("--gitops-repo", default="slipalison/homelab-gitops")
    ap.add_argument(
        "--chave",
        type=Path,
        help="chave privada JA existente, para --secrets. Sem isto, --tudo gera uma.",
    )
    ap.add_argument(
        "--sonar-token-arquivo",
        type=Path,
        help="arquivo com o token do Sonar. Sem isto: variavel SONAR_TOKEN, ou pergunta.",
    )
    ap.add_argument(
        "--sem-sonar",
        action="store_true",
        help="nao mexe no SONAR_TOKEN (util quando so a chave do GitOps mudou).",
    )
    ap.add_argument(
        "--todos",
        action="store_true",
        help="grava em TODOS os repositorios da conta, e nao so nos que usam o secret.",
    )
    ap.add_argument(
        "--titulo",
        default="CI: escrita da tag de imagem (todos os apps)",
        help="titulo da deploy key no GitHub.",
    )
    args = ap.parse_args()

    if not NOME_DE_CONTA.match(args.dono):
        raise SystemExit(f"--dono nao parece uma conta do GitHub: {args.dono!r}")
    if not REPO_COMPLETO.match(args.gitops_repo):
        raise SystemExit(f"--gitops-repo precisa ser dono/repo; recebi {args.gitops_repo!r}")

    exige_ferramentas()

    if args.conferir:
        return confere(args.dono)

    if args.chave_nova:
        destino = Path.home() / ".ssh"
        destino.mkdir(parents=True, exist_ok=True)
        privada = chave_de_deploy(args.gitops_repo, destino, args.titulo)
        print(f"\nPronto. A privada esta em {privada}.")
        print("Para gravar nos repositorios:")
        print(f"  python bin/preparar_repositorios.py --secrets --chave {privada}")
        return 0

    # --tudo e --secrets daqui para baixo.
    if args.todos:
        quantos = "TODOS os repositorios da conta"
        print(f"--todos: o valor vai para {quantos}, inclusive nos que nao usam o secret.")
        print("Cada copia e mais um lugar de onde vazar e mais um para rotacionar.")
        if sys.stdin.isatty() and input("Confirma? [digite `sim`]: ").strip().lower() != "sim":
            print("Nada foi gravado.")
            return 1

    # Diretorio temporario, e nao um fixo no perfil: ele some no `finally`
    # mesmo que algo falhe no meio. Segredo esquecido em disco e segredo que
    # vaza depois, quando ninguem lembra que ele esta la.
    pasta = Path(tempfile.mkdtemp(prefix="segredos-"))
    try:
        secrets: list[str] = []

        if args.tudo and not args.chave:
            privada = chave_de_deploy(args.gitops_repo, pasta, args.titulo)
        else:
            privada = args.chave
            if privada is None:
                raise SystemExit("::error::--secrets exige --chave (ou use --tudo).")
            if not privada.is_file():
                raise SystemExit(f"::error::{privada} nao existe.")

        shutil.copyfile(privada, pasta / "GITOPS_SSH_KEY")
        secrets.append("GITOPS_SSH_KEY")

        if not args.sem_sonar:
            token = le_segredo("SONAR_TOKEN", args.sonar_token_arquivo)
            (pasta / "SONAR_TOKEN").write_text(token, encoding="utf-8")
            secrets.append("SONAR_TOKEN")

        falhou = semeia(pasta, secrets, args.dono, args.todos)
    finally:
        shutil.rmtree(pasta, ignore_errors=True)

    if falhou:
        print("::error::a gravacao falhou; a conferencia abaixo dira o que ficou de fora.")
    confere(args.dono)
    return falhou


if __name__ == "__main__":
    sys.exit(main())

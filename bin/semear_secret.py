#!/usr/bin/env python3
"""Grava um secret nos repositorios que usam esta esteira, de uma vez.

POR QUE ESTE ARQUIVO EXISTE
---------------------------
Nao existe secret de Actions global para conta pessoal. O proprio `gh` diz:

    organization: available to GitHub Actions runs ... within an organization
    user:         available to Codespaces for your user

O secret de usuario existe e so o Codespaces enxerga; Actions nao. E secret de
ORGANIZACAO, no plano Free, nao alcanca repositorio privado — entao a
organizacao nao elimina o problema, divide em dois: um mecanismo para os
publicos e outro para os privados.

Este script resolve os dois com o mesmo comando.

A LISTA NAO E MANTIDA A MAO
---------------------------
Ele DESCOBRE quais repositorios chamam `slipalison/github-workflows`, lendo os
workflows de cada um. Uma lista escrita a mao envelhece em silencio: alguem
adota a esteira num repositorio novo, esquece de acrescentar aqui, e na proxima
rotacao aquele repositorio fica com o token velho — que falha meses depois, sem
ninguem ligar uma coisa na outra.

O SEGREDO NAO PASSA PELA LINHA DE COMANDO
-----------------------------------------
`gh secret set NOME --body "$TOKEN"` poe o valor no argv, e argv e legivel por
qualquer processo da maquina (`ps aux`). Aqui o valor vai por STDIN, que nao
aparece em lugar nenhum. Pelo mesmo motivo o script nunca aceita o token como
argumento: ou vem do ambiente, ou e digitado sem eco.

USO
    python bin/semear_secret.py --listar
    SONAR_TOKEN=... python bin/semear_secret.py --aplicar
    python bin/semear_secret.py --aplicar --secret GITOPS_SSH_KEY

Rotacionar e rodar de novo: o `gh secret set` sobrescreve.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROPRIO = "github-workflows"

# Tudo o que vira argumento do `gh` passa por aqui antes. Nao ha shell no meio,
# mas um valor comecando com `-` viraria flag do `gh` — e o nome do repositorio
# vem da API, nao de quem digita. Reprovar cedo custa uma linha.
NOME_DE_SECRET = re.compile(r"^[A-Z_][A-Z0-9_]*$")
NOME_DE_CONTA = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
NOME_DE_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# O criterio nao pode ser "usa a esteira": e largo demais, e foi medido. Na
# primeira execucao ele trouxe `homelab-gitops`, que so chama o helm-lint.yml, e
# o proprio `github-workflows`, que se auto-referencia. Nenhum dos dois tem
# Sonar. Gravar um token onde ele nao serve nao quebra nada hoje — e cria um
# segredo a mais para vazar e para lembrar de rotacionar.
#
# Entao cada secret procura o que REALMENTE o consome.
# O padrao e o CAMINHO QUALIFICADO, e nao o nome do arquivo. Tambem foi medido:
# procurar por "pipeline.yml" trouxe o `TranslateReader`, que tem um pipeline.yml
# proprio, sem nenhuma relacao com esta esteira. Nome de arquivo colide; caminho
# com o dono e o repositorio dentro, nao.
_AQUI = "slipalison/github-workflows/.github/workflows"
PADROES = {
    "SONAR_TOKEN": (f"{_AQUI}/sonar.yml", "sonar_projeto"),
    "GITOPS_SSH_KEY": (f"{_AQUI}/deploy.yml", f"{_AQUI}/pipeline.yml"),
    "GITOPS_TOKEN": (f"{_AQUI}/deploy.yml", f"{_AQUI}/pipeline.yml"),
}


def gh(*args: str, entrada: str | None = None, checar: bool = True) -> str:
    r = subprocess.run(
        ["gh", *args], capture_output=True, text=True, input=entrada, encoding="utf-8"
    )
    if checar and r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} falhou: {r.stderr.strip()}")
    return r.stdout


def repositorios(dono: str) -> list[dict]:
    dados = gh("repo", "list", dono, "--limit", "200", "--json", "name,visibility,isArchived")
    # Repositorio arquivado e somente leitura: gravar secret nele falha, e a
    # falha nao significa nada — so polui a saida.
    lista = [r for r in json.loads(dados) if not r["isArchived"]]
    for r in lista:
        if not NOME_DE_REPO.match(r["name"]):
            raise SystemExit(f"nome de repositorio inesperado vindo da API: {r['name']!r}")
    return lista


def consome(dono: str, repo: str, padroes: tuple[str, ...]) -> str | None:
    """Nome do workflow que consome este secret, ou None.

    Le o conteudo de verdade em vez de confiar na busca de codigo do GitHub: a
    busca nao indexa repositorio privado sem escopo extra, e indexa com atraso —
    os dois falham para MENOS, que e o pior lado para errar aqui.
    """
    listagem = gh(
        "api",
        f"repos/{dono}/{repo}/contents/.github/workflows",
        checar=False,
    )
    if not listagem.strip():
        return None
    try:
        arquivos = json.loads(listagem)
    except json.JSONDecodeError:
        return None
    if not isinstance(arquivos, list):
        return None

    for arquivo in arquivos:
        if not arquivo["name"].endswith((".yml", ".yaml")):
            continue
        bruto = gh("api", arquivo["url"], checar=False)
        if not bruto.strip():
            continue
        try:
            conteudo = base64.b64decode(json.loads(bruto).get("content", "")).decode(
                "utf-8", "replace"
            )
        except (json.JSONDecodeError, ValueError):
            continue
        if any(padrao in conteudo for padrao in padroes):
            return arquivo["name"]
    return None


def impressao(valor: str) -> str:
    """Identifica o valor sem revelar o valor.

    Existe porque um segredo mal colado nao tem sintoma: grava sem reclamar,
    e o erro aparece tres execucoes depois num 403 que manda "conferir o token".
    Com o tamanho e o sha256 na tela da para comparar com a origem em dois
    segundos.
    """
    return f"{len(valor)} caracteres, sha256 {hashlib.sha256(valor.encode()).hexdigest()[:12]}"


def valida_sonar(valor: str) -> str | None:
    """None se o token presta; a razao, se nao presta.

    Um endpoint que NAO faz analise: so responde quem sou eu. Assim a checagem
    nao depende do projeto existir, nem de permissao de analise, nem de a
    Automatic Analysis estar ligada — isola a autenticacao e mais nada.
    """
    credencial = base64.b64encode(f"{valor}:".encode()).decode()
    req = urllib.request.Request(
        "https://sonarcloud.io/api/users/current",
        headers={"Authorization": f"Basic {credencial}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            quem = json.load(r)
        return None if quem.get("login") else "resposta sem login"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} — o SonarCloud recusou este token"
    except Exception as e:  # noqa: BLE001 - rede, DNS, TLS: tudo aqui e inconclusivo
        print(f"  aviso: nao deu para validar ({e}); seguindo sem conferir.")
        return None


VALIDADORES = {"SONAR_TOKEN": valida_sonar}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--listar", action="store_true", help="so mostra, nao grava")
    grupo.add_argument("--aplicar", action="store_true")
    ap.add_argument("--secret", default="SONAR_TOKEN")
    ap.add_argument("--dono", default="slipalison")
    ap.add_argument(
        "--padrao",
        action="append",
        help="texto a procurar nos workflows; repetivel. Sem isto usa PADROES.",
    )
    ap.add_argument(
        "--arquivo",
        type=Path,
        help=(
            "le o valor de um arquivo, em vez do terminal. Use isto no Git Bash "
            "e no MSYS: o getpass do Python nao le a colagem de forma confiavel "
            "nesses terminais, e um valor truncado grava sem reclamar."
        ),
    )
    args = ap.parse_args()

    if not NOME_DE_SECRET.match(args.secret):
        raise SystemExit(f"--secret precisa ser MAIUSCULAS_E_SUBLINHADO; recebi {args.secret!r}")
    if not NOME_DE_CONTA.match(args.dono):
        raise SystemExit(f"--dono nao parece uma conta do GitHub: {args.dono!r}")

    padroes = tuple(args.padrao) if args.padrao else PADROES.get(args.secret)
    if not padroes:
        raise SystemExit(
            f"`{args.secret}` nao esta em PADROES. Diga o que procurar com --padrao, "
            "para o script nao gravar o segredo em repositorio que nao o usa."
        )

    print(f"Procurando {' ou '.join(padroes)} em {args.dono}/* ...")
    alvos: list[tuple[str, str, str]] = []
    for repo in repositorios(args.dono):
        # O repositorio dos templates se auto-referencia; ele DEFINE a esteira,
        # nao a consome.
        if repo["name"] == PROPRIO:
            continue
        onde = consome(args.dono, repo["name"], padroes)
        if onde:
            alvos.append((repo["name"], repo["visibility"], onde))
            print(f"  {repo['visibility']:<8} {repo['name']}  ({onde})")

    if not alvos:
        print("\nNenhum repositorio usa a esteira. Nada a fazer.")
        return 0

    print(f"\n{len(alvos)} repositorio(s) receberiam `{args.secret}`.")
    if args.listar:
        print("Modo --listar: nada foi gravado.")
        return 0

    if args.arquivo:
        # strip no fim: um editor quase sempre deixa uma quebra de linha no
        # final do arquivo, e um token com quebra de linha colada atras e um
        # token diferente — que o SonarCloud recusa sem explicar por que.
        valor = args.arquivo.read_text(encoding="utf-8").strip()
        print(f"Valor lido de {args.arquivo}.")
    elif os.environ.get(args.secret):
        valor = os.environ[args.secret]
        print(f"Valor de `{args.secret}` lido do ambiente.")
    elif not sys.stdin.isatty():
        valor = sys.stdin.read().strip()
        print("Valor lido da entrada padrao.")
    else:
        # getpass, e nao input(): sem eco e sem historico de shell. E nunca como
        # argumento — argv e publico na maquina.
        valor = getpass.getpass(f"Valor de {args.secret} (nao aparece na tela): ")
    valor = valor.strip()
    if not valor:
        print("::error::valor vazio; nada foi gravado.")
        return 1

    # A impressao digital vem ANTES da gravacao, para dar para comparar com a
    # origem. Foi assim que um token mal lido custou tres execucoes da esteira e
    # dois diagnosticos errados: o erro so aparecia la na frente, num 403 do
    # SonarCloud que mandava "conferir o token".
    print(f"Impressao digital: {impressao(valor)}")

    validador = VALIDADORES.get(args.secret)
    if validador:
        print("Conferindo o valor na origem antes de gravar...")
        problema = validador(valor)
        if problema:
            print(f"::error::{problema}. NADA foi gravado.")
            print(
                "::error::Confira se colou o valor inteiro. Em Git Bash ou MSYS, "
                "prefira --arquivo ou a variavel de ambiente: o getpass nao le "
                "colagem de forma confiavel nesses terminais."
            )
            return 1
        print("  ok: a origem aceitou o valor.")

    falhas = 0
    for nome, _, _ in alvos:
        r = subprocess.run(
            ["gh", "secret", "set", args.secret, "--repo", f"{args.dono}/{nome}"],
            input=valor,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if r.returncode == 0:
            print(f"  ok    {nome}")
        else:
            falhas += 1
            print(f"  FALHA {nome}: {r.stderr.strip()}")

    print(f"\n{len(alvos) - falhas} gravado(s), {falhas} falha(s).")
    if falhas:
        return 1
    print(
        "\nLembrete: com o secret no lugar, tire `sonar_exigir_token: false` do\n"
        "ci.yml de cada repositorio — enquanto ele estiver la, o guard apenas\n"
        "avisa em vez de reprovar quando a analise nao roda."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

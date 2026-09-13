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
import json
import os
import subprocess
import sys

PROPRIO = "github-workflows"

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
    return [r for r in json.loads(dados) if not r["isArchived"]]


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
    args = ap.parse_args()

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

    valor = os.environ.get(args.secret)
    if valor:
        print(f"Valor de `{args.secret}` lido do ambiente.")
    else:
        # getpass, e nao input(): sem eco na tela e sem ir para o historico do
        # shell. E nunca como argumento — argv e publico na maquina.
        valor = getpass.getpass(f"Valor de {args.secret} (nao aparece na tela): ")
    if not valor.strip():
        print("::error::valor vazio; nada foi gravado.")
        return 1

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

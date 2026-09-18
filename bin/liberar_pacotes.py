#!/usr/bin/env python3
"""Libera a leitura de pacote privado do npm do GitHub — na maquina e no CI.

POR QUE ESTE ARQUIVO EXISTE
---------------------------
O npm do GitHub exige token ATE PARA PACOTE PUBLICO. Isso e diferente do
`ghcr.io`, onde imagem publica se puxa sem credencial nenhuma, e a diferenca
pega todo mundo de surpresa uma vez: `npm install @dono/pacote` responde 401
num repositorio publico, e a mensagem do npm nao diz que o problema e escopo
de token.

Pior: o token faz falta em QUATRO lugares, nao em um.

    maquina de quem escreve   npm ci / npm install
    job de qualidade          npm ci do componente node
    job do Sonar              o comando_testes instala de novo
    docker build              o estagio que constroi o frontend

Os tres ultimos se resolvem com o secret `GH_PACKAGES_TOKEN`, que o
`pipeline.yml` distribui (ver README). O primeiro se resolve com escopo no
`gh`. Este script faz os dois, confere o resultado de verdade, e e seguro
rodar de novo: tudo que ele faz e idempotente.

O QUE ELE CONFERE, E POR QUE NAO ACREDITA NO ESCOPO
---------------------------------------------------
Ter `read:packages` escrito na lista de escopos NAO e a mesma coisa que o
registro aceitar o token. Escopo revogado do lado do dono do pacote, token
de organizacao sem acesso concedido ao repositorio, pacote que nao existe
com aquele nome — nos tres casos o escopo aparece e o `npm ci` reprova.

Por isso o `--pacote` existe: a prova e um GET no registro, pedindo o pacote
que o projeto vai instalar de verdade. E a unica que vale.

O SEGREDO NAO PASSA PELA LINHA DE COMANDO
-----------------------------------------
`gh secret set NOME --body "$TOKEN"` poe o valor no argv, e argv e legivel
por qualquer processo da maquina (`ps aux`, e no Windows o Process Explorer).
Aqui o valor vai por STDIN, em bytes, e nunca e impresso — nem em erro, nem
em modo verboso, porque nao ha modo verboso.

USO
---
    # o que falta, sem mudar nada. Sai 1 se faltar algo.
    python bin/liberar_pacotes.py --conferir --pacote @slipalison/coluna \\
        --repo slipalison/basalto

    # conserta: pede o escopo que falta e grava o secret nos repositorios
    python bin/liberar_pacotes.py --aplicar --pacote @slipalison/coluna \\
        --repo slipalison/basalto

QUAL TOKEN VAI PARAR NO CI
--------------------------
Por padrao, o do proprio `gh` (`gh auth token`). Funciona e nao exige criar
nada — mas ele morre junto com a autorizacao do gh: se voce rodar
`gh auth logout`, revogar o app na conta, ou trocar de maquina e revogar a
antiga, o CI comeca a reprovar sem ninguem ter mexido nele.

Para um token que nao depende disso, crie um PAT com `read:packages` e SO
isso, ponha num arquivo, e passe `--de-arquivo`. O arquivo e lido e nao e
tocado — apague voce, depois. E o mesmo formato que o `semear_secret.py`
espera em `--de-diretorio`, entao o arquivo serve para os dois.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

SECRET = "GH_PACKAGES_TOKEN"
ESCOPO = "read:packages"
REGISTRO = "https://npm.pkg.github.com"
SERVIDOR = "github.com"

# Codigo de saida: 0 tudo certo, 1 falta alguma coisa. Um so, de proposito —
# quem chama isto num script quer saber "posso seguir?", e nao catalogar
# variedades de falha.
FALTA = 1


def erro(msg: str) -> None:
    print(f"  ERRO   {msg}")


def ok(msg: str) -> None:
    print(f"  ok     {msg}")


def aviso(msg: str) -> None:
    print(f"  aviso  {msg}")


def gh(*args: str, checar: bool = True) -> str:
    """Roda o `gh` capturando a saida. Valor de secret NAO passa por aqui."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if checar and r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} falhou: {r.stderr.strip()}")
    return r.stdout


def gh_interativo(*args: str) -> int:
    """Roda o `gh` HERDANDO o terminal.

    `gh auth refresh` e um fluxo de duas pernas: ele mostra um codigo de uso
    unico, abre o navegador, e so troca o token depois que a pessoa cola o
    codigo e autoriza. Capturar a saida esconderia justamente o codigo, e o
    comando ficaria esperando para sempre por uma coisa que ninguem viu.
    """
    return subprocess.run(["gh", *args]).returncode


def token_do_gh() -> str:
    valor = gh("auth", "token").strip()
    if not valor:
        raise SystemExit("`gh auth token` devolveu vazio — rode `gh auth login` primeiro.")
    return valor


def escopos_do_token(valor: str) -> set[str] | None:
    """Escopos que o GitHub associa a este token, ou None se ele nao informa.

    Token classico devolve o cabecalho `x-oauth-scopes`. Token fine-grained
    nao devolve cabecalho nenhum — ali a resposta honesta e "nao sei", e quem
    decide e a prova no registro, logo adiante.
    """
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {valor}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        bruto = r.headers.get("x-oauth-scopes")
    if bruto is None:
        return None
    return {e.strip() for e in bruto.split(",") if e.strip()}


def le_pacote(valor: str, pacote: str) -> tuple[bool, str]:
    """(deu certo, o que dizer). A prova de verdade: o registro aceitou?"""
    caminho = urllib.parse.quote(pacote, safe="")
    req = urllib.request.Request(
        f"{REGISTRO}/{caminho}",
        headers={"Authorization": f"Bearer {valor}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            dados = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"{pacote}: HTTP {e.code} — o registro recusou este token"
        if e.code == 404:
            return False, (
                f"{pacote}: HTTP 404 — ou o nome esta errado, ou este token nao "
                f"enxerga o pacote (os dois casos respondem 404 aqui)"
            )
        return False, f"{pacote}: HTTP {e.code} — o registro nao respondeu o que se esperava"
    except Exception as e:  # noqa: BLE001 - rede, DNS, TLS: tudo aqui e inconclusivo
        return False, f"{pacote}: nao deu para conferir ({e})"
    versao = (dados.get("dist-tags") or {}).get("latest", "?")
    return True, f"{pacote}: o registro entregou (ultima versao {versao})"


def secret_ja_existe(repo: str) -> bool | None:
    """True/False, ou None se nao deu para olhar (sem permissao, repo errado)."""
    saida = gh("secret", "list", "--repo", repo, "--json", "name", checar=False)
    if not saida.strip():
        return None
    try:
        nomes = {s["name"] for s in json.loads(saida)}
    except (ValueError, KeyError, TypeError):
        return None
    return SECRET in nomes


def grava_secret(repo: str, valor: str) -> bool:
    """Grava por STDIN. O valor nunca aparece em argv nem na saida."""
    r = subprocess.run(
        ["gh", "secret", "set", SECRET, "--repo", repo],
        input=valor.encode("utf-8"),
        capture_output=True,
    )
    if r.returncode != 0:
        detalhe = r.stderr.decode("utf-8", "replace").strip()
        erro(f"{repo}: nao gravou — {detalhe}")
        return False
    return True


def linha_de_export() -> None:
    print()
    print("Na sua maquina, antes de `npm ci` ou `npm run e2e`:")
    print()
    if sys.platform == "win32":
        print("  PowerShell:  $env:NODE_AUTH_TOKEN = (gh auth token)")
        print('  Git Bash:    export NODE_AUTH_TOKEN="$(gh auth token)"')
    else:
        print('  export NODE_AUTH_TOKEN="$(gh auth token)"')
    print()
    print("O `.npmrc` do projeto referencia essa variavel em vez de trazer o")
    print("token escrito — e por isso ele pode ser versionado. Sem a variavel,")
    print("`npm run build` e `npm test` seguem funcionando: so o `npm ci` de um")
    print("pacote privado e que precisa dela.")


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Libera a leitura de pacote privado do npm do GitHub, na maquina e no CI. "
            "Idempotente: rodar de novo nao estraga nada."
        ),
        epilog=(
            "Exemplo: python bin/liberar_pacotes.py --aplicar "
            "--pacote @slipalison/coluna --repo slipalison/basalto"
        ),
    )
    modo = p.add_mutually_exclusive_group(required=True)
    modo.add_argument("--conferir", action="store_true", help="so diz o que falta; nao muda nada")
    modo.add_argument("--aplicar", action="store_true", help="pede o escopo e grava o secret")
    p.add_argument(
        "--pacote",
        action="append",
        default=[],
        metavar="@DONO/NOME",
        help="pacote a provar no registro; repetivel. Sem isto, a conferencia e so do escopo.",
    )
    p.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="DONO/REPO",
        help=f"onde gravar o {SECRET}; repetivel. Sem isto, so a parte local.",
    )
    p.add_argument(
        "--de-arquivo",
        metavar="CAMINHO",
        help=(
            "le o token deste arquivo em vez de usar o do `gh`. Para um PAT proprio, "
            "que nao morre junto com a autorizacao do gh."
        ),
    )
    args = p.parse_args()

    if shutil.which("gh") is None:
        erro("o `gh` nao esta no PATH. https://cli.github.com")
        return FALTA

    faltou = False

    # ---------------------------------------------------------------- 1. token
    print("1. Token")
    if args.de_arquivo:
        try:
            with open(args.de_arquivo, encoding="utf-8") as f:
                valor = f.read().strip()
        except OSError as e:
            erro(f"nao deu para ler {args.de_arquivo}: {e}")
            return FALTA
        if not valor:
            erro(f"{args.de_arquivo} esta vazio")
            return FALTA
        ok(f"lido de {args.de_arquivo} (o arquivo nao foi tocado; apague voce, depois)")
    else:
        if gh("auth", "status", checar=False) == "" and gh("auth", "token", checar=False) == "":
            erro("o `gh` nao esta autenticado. Rode `gh auth login`.")
            return FALTA
        valor = token_do_gh()
        ok("usando o token do proprio `gh`")

    # --------------------------------------------------------------- 2. escopo
    print("2. Escopo")
    try:
        escopos = escopos_do_token(valor)
    except urllib.error.HTTPError as e:
        erro(f"o GitHub recusou o token (HTTP {e.code})")
        return FALTA
    except Exception as e:  # noqa: BLE001 - rede: inconclusivo, nao reprovado
        aviso(f"nao deu para conferir o escopo ({e}); seguindo para a prova no registro")
        escopos = None

    if escopos is None:
        aviso("token fine-grained nao informa escopo — quem decide e a prova abaixo")
    elif ESCOPO in escopos:
        ok(f"{ESCOPO} presente")
    elif args.de_arquivo:
        erro(f"este token nao tem {ESCOPO}. Crie outro PAT, com esse escopo.")
        faltou = True
    elif args.conferir:
        erro(f"falta {ESCOPO}. Com --aplicar eu peco (abre o navegador).")
        faltou = True
    else:
        print(f"  ...    pedindo {ESCOPO} ao GitHub. COLE O CODIGO no navegador que vai abrir.")
        print()
        rc = gh_interativo("auth", "refresh", "-h", SERVIDOR, "-s", ESCOPO)
        print()
        if rc != 0:
            erro("o `gh auth refresh` nao concluiu — o token antigo continua valendo")
            return FALTA
        valor = token_do_gh()
        depois = escopos_do_token(valor)
        if depois is not None and ESCOPO not in depois:
            erro(f"mesmo depois do refresh, {ESCOPO} nao aparece. O fluxo foi concluido mesmo?")
            return FALTA
        ok(f"{ESCOPO} concedido")

    # ---------------------------------------------------------------- 3. prova
    print("3. Prova no registro")
    if not args.pacote:
        aviso("nenhum --pacote: nao da para provar que o registro aceita este token")
    for pacote in args.pacote:
        deu, recado = le_pacote(valor, pacote)
        (ok if deu else erro)(recado)
        faltou = faltou or not deu

    # -------------------------------------------------------------- 4. secrets
    print(f"4. {SECRET} nos repositorios")
    if not args.repo:
        aviso("nenhum --repo: o CI nao foi tocado")
    for repo in args.repo:
        existe = secret_ja_existe(repo)
        if existe is None:
            erro(f"{repo}: nao deu para listar os secrets (repo errado, ou sem permissao)")
            faltou = True
            continue
        if args.conferir:
            (ok if existe else erro)(f"{repo}: {'ja esta la' if existe else 'FALTA'}")
            faltou = faltou or not existe
            continue
        if grava_secret(repo, valor):
            ok(f"{repo}: gravado{' (sobrescrito)' if existe else ''}")
        else:
            faltou = True

    # ----------------------------------------------------------------- arremate
    linha_de_export()
    print()
    if faltou:
        print("Falta coisa acima. Rode com --aplicar, ou leia o ERRO.")
        return FALTA
    print("Tudo liberado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

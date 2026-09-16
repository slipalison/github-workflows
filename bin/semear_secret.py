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
    # o que falta, em quem, sem gravar nada. Sai 1 se faltar algum.
    python bin/semear_secret.py --conferir

    # quem receberia, sem gravar
    python bin/semear_secret.py --listar --secret SONAR_TOKEN

    # gravar um
    SONAR_TOKEN=... python bin/semear_secret.py --aplicar --secret SONAR_TOKEN

    # gravar varios numa passada: um arquivo por secret, com o NOME do secret
    # como nome do arquivo, num diretorio FORA de qualquer repositorio
    python bin/semear_secret.py --aplicar \
        --secret SONAR_TOKEN --secret GITOPS_SSH_KEY \
        --de-diretorio ~/.config/segredos-github

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


def so_o_que_casa(padrao: re.Pattern[str], valor: str, queixa: str) -> str:
    """Devolve o valor SO se ele casar inteiro com o padrao; senao, para aqui.

    O retorno sai de `m.group(0)`, e nao do argumento. Sao a mesma string, mas
    a diferenca importa duas vezes: quem le sabe que dali para baixo o valor ja
    passou pelo crivo, e a analise estatica consegue seguir isso — antes ela
    apontava injecao de argumento em `gh secret set`, porque a checagem ficava
    num `if` que ela nao relacionava com o uso la embaixo.

    Estes valores viram argumento de `gh`. Um `--secret` que comecasse com
    tracos viraria uma OPCAO do `gh`, e nao o nome de um secret.
    """
    m = padrao.match(valor)
    if m is None:
        raise SystemExit(f"{queixa}; recebi {valor!r}")
    return m.group(0)


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
    # NPM_TOKEN nao passa por esta esteira: quem publica no npm tem workflow
    # proprio (o `jdi-cli` tem `npm-publish.yml`). Entao o padrao aqui nao pode
    # ser o caminho da esteira — e a REFERENCIA AO PROPRIO SECRET, que e o
    # criterio mais honesto que existe: se um workflow le `secrets.NPM_TOKEN`,
    # aquele repositorio precisa dele. Sem falso positivo possivel.
    "NPM_TOKEN": ("secrets.NPM_TOKEN", "NODE_AUTH_TOKEN"),
}

# Secrets que se SUBSTITUEM. O `deploy.yml` aceita chave SSH ou token
# fine-grained e usa o que encontrar; ter os dois nao ajuda em nada, e exigir
# os dois e pedir um segredo a mais para vazar e para rotacionar.
#
# Sem esta tabela o `--conferir` acusava `demo-python` de estar sem
# GITOPS_TOKEN — ele tem a chave SSH, que basta. Um relatorio que aponta
# problema onde nao ha e um relatorio que se aprende a ignorar, e ai ele deixa
# de servir para o caso em que o problema e real.
ALTERNATIVAS = {
    "GITOPS_SSH_KEY": ("GITOPS_TOKEN",),
    "GITOPS_TOKEN": ("GITOPS_SSH_KEY",),
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


def valida_npm(valor: str) -> str | None:
    """None se o token presta; a razao, se nao presta.

    `/-/whoami` so responde quem sou eu — nao publica, nao lista pacote, nao
    depende de o pacote existir. Mesmo criterio do validador do SonarCloud:
    isolar a autenticacao e mais nada.

    Vale especialmente aqui porque token do npm expira. Um token vencido grava
    sem reclamar e so aparece no dia da publicacao, com um 401 no meio do
    release — que e o pior momento para descobrir.
    """
    req = urllib.request.Request(
        "https://registry.npmjs.org/-/whoami",
        headers={"Authorization": f"Bearer {valor}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            quem = json.load(r)
        return None if quem.get("username") else "resposta sem username"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return f"HTTP {e.code} — o npm recusou este token (vencido, revogado ou incompleto)"
        return f"HTTP {e.code} — o npm nao aceitou a conferencia"
    except Exception as e:  # noqa: BLE001 - rede, DNS, TLS: tudo aqui e inconclusivo
        print(f"  aviso: nao deu para validar ({e}); seguindo sem conferir.")
        return None


VALIDADORES = {"SONAR_TOKEN": valida_sonar, "NPM_TOKEN": valida_npm}


def secrets_do_repo(dono: str, repo: str) -> set[str] | None:
    """Nomes dos secrets ja gravados, ou None se nao deu para olhar.

    `gh secret list` devolve so os NOMES — valor de secret nao volta nem para
    quem o gravou, e e assim que tem de ser. Para conferencia, nome basta: o
    que se quer saber e se ele existe.

    None e diferente de conjunto vazio: sem permissao de admin no repositorio a
    listagem falha, e tratar isso como "nao tem nenhum" faria o relatorio
    mandar gravar secret onde talvez ja exista.
    """
    r = subprocess.run(
        ["gh", "secret", "list", "--repo", f"{dono}/{repo}", "--json", "name"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if r.returncode != 0:
        return None
    try:
        return {s["name"] for s in json.loads(r.stdout or "[]")}
    except json.JSONDecodeError:
        return None


def confere(dono: str, secrets: list[str], todos: bool = False) -> int:
    """Diz quem consome cada secret e se ele ja esta la. Nao grava nada.

    POR QUE EXISTE. Ate 2026-09-16 a unica forma de descobrir que um secret
    faltava era a esteira reprovar. Aconteceu com o `basalto`: ele nasceu sem
    SONAR_TOKEN e sem GITOPS_SSH_KEY, e a falta so apareceu num job vermelho
    depois de a esteira inteira rodar. A pergunta "qual repositorio esta sem
    qual secret?" nao tinha resposta a nao ser abrindo a tela de cada um.

    Sai com 1 quando falta alguma coisa, para servir de passo de CI ou de
    tarefa agendada.
    """
    repos = repositorios(dono)
    faltando: list[tuple[str, str]] = []
    sem_permissao: set[str] = set()
    # Uma listagem de secrets por repositorio, e nao uma por secret: a chamada
    # e a mesma e o repositorio nao muda no meio da execucao.
    cache: dict[str, set[str] | None] = {}

    for secret in secrets:
        padroes = PADROES.get(secret)
        if not padroes:
            print(f"\n{secret}: fora de PADROES, nao da para saber quem consome. Pulando.")
            continue

        print(f"\n{secret}")
        algum = False
        for repo in repos:
            nome = repo["name"]
            if nome == PROPRIO:
                continue
            if not consome(dono, nome, padroes):
                continue
            algum = True
            if nome not in cache:
                cache[nome] = secrets_do_repo(dono, nome)
            existentes = cache[nome]
            if existentes is None:
                sem_permissao.add(nome)
                print(f"  ?       {nome}  (sem permissao para listar os secrets)")
            elif secret in existentes:
                print(f"  ok      {nome}")
            elif substituto := next(
                (a for a in ALTERNATIVAS.get(secret, ()) if a in existentes), None
            ):
                print(f"  ok      {nome}  (por {substituto})")
            else:
                faltando.append((nome, secret))
                print(f"  FALTA   {nome}")
        if not algum:
            print("  (nenhum repositorio consome este secret)")

    print()
    if sem_permissao:
        quais = ", ".join(sorted(sem_permissao))
        print(f"{len(sem_permissao)} repositorio(s) nao deram para conferir: {quais}")
    if not faltando:
        print("Nada faltando.")
        return 0

    # `GITOPS_SSH_KEY` e `GITOPS_TOKEN` faltando no mesmo repositorio sao UMA
    # exigencia, nao duas: basta gravar um deles. Listar os dois faria a conta
    # parecer maior do que o trabalho e.
    vistos: set[tuple[str, str]] = set()
    linhas: list[str] = []
    for nome, secret in faltando:
        alternativas = ALTERNATIVAS.get(secret, ())
        chave = (nome, min([secret, *alternativas]))
        if chave in vistos:
            continue
        vistos.add(chave)
        sufixo = f" (ou {' ou '.join(alternativas)})" if alternativas else ""
        linhas.append(f"  {nome}: {secret}{sufixo}")

    print(f"{len(linhas)} exigencia(s) nao atendida(s):")
    for linha in linhas:
        print(linha)
    print("\nPara gravar:  python bin/semear_secret.py --aplicar --secret <NOME>")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--listar", action="store_true", help="so mostra, nao grava")
    grupo.add_argument("--aplicar", action="store_true")
    grupo.add_argument(
        "--conferir",
        action="store_true",
        help="diz quem consome cada secret e se ele ja esta la. Nao grava. Sai 1 se faltar algum.",
    )
    ap.add_argument(
        "--secret",
        action="append",
        help=(
            "nome do secret; repetivel. Sem isto: SONAR_TOKEN em --listar e "
            "--aplicar, e TODOS os de PADROES em --conferir."
        ),
    )
    ap.add_argument(
        "--de-diretorio",
        type=Path,
        help=(
            "le o valor de cada secret de <DIR>/<NOME_DO_SECRET>, o que permite "
            "gravar varios de uma vez. Use um diretorio FORA de qualquer "
            "repositorio — um arquivo de segredo dentro da arvore de trabalho "
            "acaba commitado."
        ),
    )
    ap.add_argument(
        "--todos",
        action="store_true",
        help=(
            "ignora a descoberta e alcanca TODOS os repositorios nao arquivados "
            "da conta. Grava tambem onde o secret nao e usado — mais copias "
            "para vazar e para rotacionar. Pede confirmacao."
        ),
    )
    ap.add_argument(
        "--sim",
        action="store_true",
        help="responde `sim` a confirmacao do --todos. Para uso em script.",
    )
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

    dono = so_o_que_casa(NOME_DE_CONTA, args.dono, "--dono nao parece uma conta do GitHub")
    args.dono = dono

    # Sem `--secret`: conferir olha TUDO o que a esteira conhece, porque a
    # pergunta que ele responde e "o que esta faltando em algum lugar?"; listar
    # e aplicar continuam com o padrao de um so, para nao gravar em lote por
    # engano quem so queria um.
    brutos = args.secret or (list(PADROES) if args.conferir else ["SONAR_TOKEN"])
    secrets = [
        so_o_que_casa(NOME_DE_SECRET, s, "--secret precisa ser MAIUSCULAS_E_SUBLINHADO")
        for s in brutos
    ]
    args.secret = secrets

    if args.conferir:
        return confere(args.dono, secrets, todos=args.todos)

    if len(secrets) > 1 and not args.de_diretorio:
        raise SystemExit(
            "mais de um --secret exige --de-diretorio: o valor de cada um vem de "
            "<DIR>/<NOME_DO_SECRET>. Digitar varios seguidos no terminal e onde "
            "se cola o token errado no campo certo."
        )
    if args.padrao and len(secrets) > 1:
        raise SystemExit(
            "--padrao vale para um --secret so; com varios, cada um usa o seu de PADROES."
        )

    # So `--aplicar` pede confirmacao. `--listar --todos` existe justamente
    # para ver os 82 nomes ANTES de decidir, e perguntar ali seria pedir
    # autorizacao para nao fazer nada.
    if args.aplicar and args.todos and not args.sim:
        quantos = len(repositorios(args.dono))
        print(
            f"--todos vai gravar {', '.join(secrets)} em {quantos} repositorio(s) de "
            f"{args.dono}, inclusive nos que nao usam o secret."
        )
        print(
            "Cada copia e mais um lugar de onde o segredo pode vazar e mais um "
            "para lembrar de trocar na proxima rotacao."
        )
        if not confirma("Confirma?"):
            print("Nada foi gravado.")
            return 1

    total_falhas = 0
    for secret in secrets:
        if len(secrets) > 1:
            print(f"\n{'=' * 60}\n{secret}\n{'=' * 60}")
        total_falhas += aplica_um(args, secret)
    return 1 if total_falhas else 0


def todos_os_alvos(dono: str) -> list[tuple[str, str, str]]:
    """Todo repositorio nao arquivado da conta, sem olhar workflow nenhum.

    E o modo `--todos`. O CUSTO esta assumido e vale escrever: um segredo
    gravado onde ninguem o usa nao quebra nada hoje, e cria mais uma copia para
    vazar e mais um lugar para lembrar de rotacionar quando o token trocar. Com
    82 repositorios, e 82 copias.

    A troca e deliberada: quem usa este modo prefere gravar uma vez em tudo a
    voltar aqui a cada repositorio novo que adota a esteira.
    """
    alvos = [(r["name"], r["visibility"], "--todos") for r in repositorios(dono)]
    print(f"Modo --todos: {len(alvos)} repositorio(s) nao arquivado(s) de {dono}.")
    return alvos


def confirma(pergunta: str) -> bool:
    """True quando a pessoa digitou `sim`. Fora de terminal, recusa.

    Sem terminal nao ha como perguntar, e seguir em frente seria gravar em
    dezenas de repositorios porque alguem canalizou a saida por engano.
    `--sim` e a forma de dizer que sabe o que esta fazendo.
    """
    if not sys.stdin.isatty():
        print("::error::sem terminal para confirmar. Use --sim se for intencional.")
        return False
    return input(f"{pergunta} [digite `sim`]: ").strip().lower() == "sim"


def aplica_um(args, secret: str) -> int:
    """Escolhe os repositorios, le o valor e grava. 0 se deu tudo certo."""
    if args.todos:
        alvos = todos_os_alvos(args.dono)
    else:
        padroes = tuple(args.padrao) if args.padrao else PADROES.get(secret)
        if not padroes:
            raise SystemExit(
                f"`{secret}` nao esta em PADROES. Diga o que procurar com --padrao, "
                "use --todos, para o script nao gravar o segredo em repositorio "
                "que nao o usa."
            )

        print(f"Procurando {' ou '.join(padroes)} em {args.dono}/* ...")
        alvos = []
        for repo in repositorios(args.dono):
            # O repositorio dos templates se auto-referencia; ele DEFINE a
            # esteira, nao a consome.
            if repo["name"] == PROPRIO:
                continue
            onde = consome(args.dono, repo["name"], padroes)
            if onde:
                alvos.append((repo["name"], repo["visibility"], onde))
                print(f"  {repo['visibility']:<8} {repo['name']}  ({onde})")

    if not alvos:
        print("\nNenhum repositorio usa a esteira. Nada a fazer.")
        return 0

    print(f"\n{len(alvos)} repositorio(s) receberiam `{secret}`.")
    if args.listar:
        print("Modo --listar: nada foi gravado.")
        return 0

    if args.de_diretorio:
        # Um arquivo por secret, com o NOME do secret como nome do arquivo. E o
        # que permite gravar varios numa passada sem ninguem digitar nada — e
        # sem valor nenhum em argv.
        caminho = args.de_diretorio / secret
        if not caminho.is_file():
            print(f"::error::{caminho} nao existe; `{secret}` nao foi gravado.")
            return 1
        valor = caminho.read_text(encoding="utf-8").strip()
        print(f"Valor lido de {caminho}.")
    elif args.arquivo:
        # strip no fim: um editor quase sempre deixa uma quebra de linha no
        # final do arquivo, e um token com quebra de linha colada atras e um
        # token diferente — que o SonarCloud recusa sem explicar por que.
        valor = args.arquivo.read_text(encoding="utf-8").strip()
        print(f"Valor lido de {args.arquivo}.")
    elif os.environ.get(secret):
        valor = os.environ[secret]
        print(f"Valor de `{secret}` lido do ambiente.")
    elif not sys.stdin.isatty():
        valor = sys.stdin.read().strip()
        print("Valor lido da entrada padrao.")
    else:
        # getpass, e nao input(): sem eco e sem historico de shell. E nunca como
        # argumento — argv e publico na maquina.
        valor = getpass.getpass(f"Valor de {secret} (nao aparece na tela): ")
    valor = valor.strip()
    if not valor:
        print("::error::valor vazio; nada foi gravado.")
        return 1

    # A impressao digital vem ANTES da gravacao, para dar para comparar com a
    # origem. Foi assim que um token mal lido custou tres execucoes da esteira e
    # dois diagnosticos errados: o erro so aparecia la na frente, num 403 do
    # SonarCloud que mandava "conferir o token".
    print(f"Impressao digital: {impressao(valor)}")

    validador = VALIDADORES.get(secret)
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
            ["gh", "secret", "set", secret, "--repo", f"{args.dono}/{nome}"],
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

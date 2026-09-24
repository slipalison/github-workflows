#!/usr/bin/env python3
"""Roda nesta maquina os workflows que o pull request vai rodar no Actions.

POR QUE EXISTE
--------------
Nos repositorios privados, todo minuto de runner sai da cota da conta, e cada
job e cobrado em minuto cheio. Em setembro de 2026 a cota Free (2.000 min/mes)
acabou no dia 23, e 21% do que se gastou foi em runs que FALHARAM — erro que
dava para ver antes do push. O CI confirma; quem descobre e este comando.

COMO: O MESMO YAML, E NAO UMA COPIA DELE
---------------------------------------
Um script que reescrevesse os passos do CI divergiria dele no primeiro ajuste.
Este roda os PROPRIOS workflows com o `act` (nektos/act) em modo host: cada
`run:` executa aqui, com as expressoes, os `if:`, as matrizes, os workflows
reutilizaveis desta esteira e as actions de terceiro resolvidos pelo act. Fica
de fora so o que nao faz sentido fora do GitHub, e cada um desses e trocado por
uma action vazia, dita no resumo:

    step-security/harden-runner   em modo host ele instalaria um agente de rede
                                  NESTA maquina, com sudo;
    github/codeql-action          envia SARIF para a aba Security do GitHub;
    docker/login-action           login no GHCR com o GITHUB_TOKEN, que aqui
                                  nao existe (e o PR nao publica imagem);
    o Sonar                       `sonar_projeto` vazio: a analise e do
                                  SonarCloud, e so o CI a mede;
    a publicacao da imagem        o job `imagem` publica `sha-<commit>` no
                                  GHCR ate no PR; na copia da esteira, todo
                                  `push: true` vira `push: false`.

E uma e trocada por outra versao dela mesma: `actions/upload-artifact` depois
da v4 fala uma API que o servidor de artefatos do act ainda nao tem ("Failed
to CreateArtifact", medido com a v7); vira a v4.6.2, que ele fala e que faz o
mesmo upload.
A do TruffleHog e a mesma action, no mesmo SHA, com `--memory` no
`docker run`: ela varre o historico inteiro, e sem teto derrubava a maquina.

O QUE ELE PROTEGE NESTA MAQUINA
-------------------------------
Modo host roda os passos no sistema daqui, e os workflows foram escritos para
uma VM descartavel. Por isso:

    - HOME isolado em ~/.cache/validar-local/home: um passo que escreva em
      ~/.ssh, ~/.cargo ou ~/.npm escreve la, e nao no HOME de verdade;
    - `git@github.com:` vira HTTPS com a credencial do `gh`, entao um job que
      clona por chave de deploy clona sem ela (a chave recebe um valor de
      mentira, e nunca e usada);
    - nenhum token com escrita chega aos passos: o GITHUB_TOKEN nao e passado,
      e o do npm do GitHub e o do `gh` (leitura de pacote);
    - um validar-local por vez (trava), porque os jobs dividem Docker e portas;
    - os jobs rodam um de cada vez, pelo mesmo motivo.

A esteira usada e a `main` de `origin` deste repositorio (`@main` e o que o CI
de quem chama usa); `--esteira-local` usa a copia daqui como esta, para validar
uma mudanca na propria esteira antes do PR dela.

SEGREDOS
--------
Nunca por argumento: o act le cada um do ambiente (`-s NOME`). Vem de tres
lugares, nesta ordem: o ambiente de quem chama; os arquivos
`~/.config/<repositorio>/*.env` (o basalto guarda la as credenciais do E2E);
e dois casos conhecidos — GH_PACKAGES_TOKEN e o token do `gh`, e
GITOPS_SSH_KEY recebe o valor de mentira acima. O que faltar e dito antes de
rodar, com o nome do workflow que o pede.

O REGISTRO
----------
Com tudo verde e a arvore sem mudanca por commitar, grava
`.git/validar-local/<arvore do HEAD>`. `--status` diz se o HEAD ja passou.

USO (na raiz do repositorio que vai abrir o PR)
    validar-local                    todo workflow que roda em pull_request
    validar-local -W e2e.yml         so esse workflow
    validar-local -W ci.yml -j rsa   so um job (para iterar; antes do PR, tudo)
    validar-local --listar           o que rodaria, sem rodar
    validar-local --status           o HEAD ja foi validado?
    validar-local --esteira-local    com a esteira desta maquina, como esta
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ESTEIRA = Path(__file__).resolve().parent.parent
# Fixo, e nao lido do ambiente: tudo o que este script apaga e reescreve mora
# aqui, e um caminho vindo de fora poria essa limpeza em qualquer lugar.
CACHE = Path.home() / ".cache" / "validar-local"
REPO_ESTEIRA = "slipalison/github-workflows"
DIR_GITHUB = ".github"
DIR_CARGO = ".cargo"
ACTION_YML = "action.yml"
GLOB_YAML = "*.y*ml"
REF_MAIN = "origin/main"

# O que -W e -j aceitam: um nome de arquivo de workflow e um id de job. Nada
# que nao case chega ao act.
NOME_WORKFLOW = re.compile(r"[\w.-]+\.ya?ml")
ID_JOB = re.compile(r"[\w.-]+")

# Versao E sha256 fixos aqui, conferidos contra o checksums.txt de cada release
# em 2026-09-24. Subir de versao e mudar as duas colunas.
FERRAMENTAS = {
    "act": (
        "https://github.com/nektos/act/releases/download/v0.2.89/act_Linux_x86_64.tar.gz",
        "0191d6f1f3b716b5c55820032605d05fc3c1cdbf581ebeff655019e5dd1524c0",
        "act",
    ),
    "actionlint": (
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/"
        "actionlint_1.7.12_linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
        "actionlint",
    ),
    # O runner do GitHub traz o yq; esta maquina nao.
    "yq": (
        "https://github.com/mikefarah/yq/releases/download/v4.53.6/yq_linux_amd64",
        "c5f056448f973ae7d39b5401949648a78f2dc1947d6a8eb65be60d5c504b9385",
        None,
    ),
}

# As actions que so fazem sentido no GitHub, ou que mexeriam nesta maquina.
TROCADAS = ("step-security/harden-runner", "github/codeql-action", "docker/login-action")

# A versao do upload-artifact que o servidor de artefatos do act fala.
UPLOAD_V4 = ("v4.6.2", "ea165f8d65b6e75b540449e92b4886f43607fa02")

ROTULOS = ("ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04")

# O que o runner ubuntu do GitHub ja traz e os workflows desta casa usam sem
# instalar. Faltando aqui, o passo falha por ambiente, e nao por defeito — entao
# se confere ANTES, com o comando que resolve.
DO_RUNNER = {
    "docker": "o Docker",
    "git": "git",
    "gh": "gh (com login)",
    "node": "Node.js",
    "npm": "npm",
    "jq": "jq",
    "curl": "curl",
    "unzip": "unzip",
    "zstd": "zstd",
}

VERDE, VERMELHO, AMARELO, FRACO, FIM = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    VERDE = VERMELHO = AMARELO = FRACO = FIM = ""


def diz(texto: str = "") -> None:
    print(texto, flush=True)


def falha(texto: str) -> int:
    diz(f"{VERMELHO}validar-local:{FIM} {texto}")
    return 1


def git(*args: str, cwd: Path | None = None, verificar: bool = True) -> str:
    saida = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if verificar and saida.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {saida.stderr.strip()}")
    return saida.stdout.strip()


# --------------------------------------------------------------- ferramentas


def memoria_livre_mb() -> int | None:
    try:
        for linha in Path("/proc/meminfo").read_text().splitlines():
            if linha.startswith("MemAvailable:"):
                return int(linha.split()[1]) // 1024
    except OSError:
        return None
    return None


def falta_do_runner() -> list[str]:
    faltam = [f"{desc} ({cmd})" for cmd, desc in DO_RUNNER.items() if not shutil.which(cmd)]
    python = subprocess.run(["python3", "-c", "import yaml"], capture_output=True)
    if python.returncode != 0:
        faltam.append("PyYAML no python3 do sistema (python3-yaml)")
    docker = (
        subprocess.run(["docker", "info"], capture_output=True) if shutil.which("docker") else None
    )
    if docker is not None and docker.returncode != 0:
        faltam.append("acesso ao Docker (o usuario no grupo docker, numa sessao nova)")
    return faltam


def ferramenta(nome: str) -> Path:
    url, esperado, dentro = FERRAMENTAS[nome]
    destino = CACHE / "bin" / nome
    marca = destino.with_suffix(".sha256")
    if destino.exists() and marca.exists() and marca.read_text().strip() == esperado:
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resposta:  # noqa: S310 - URL fixa acima
        dados = resposta.read()
    medido = hashlib.sha256(dados).hexdigest()
    if medido != esperado:
        raise RuntimeError(f"{nome}: sha256 {medido}, esperado {esperado} — o release mudou")
    if dentro is None:
        destino.write_bytes(dados)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            arquivo = Path(tmp) / "pacote.tgz"
            arquivo.write_bytes(dados)
            with tarfile.open(arquivo) as tar:
                tar.extract(dentro, tmp, filter="data")
            shutil.move(str(Path(tmp) / dentro), destino)
    destino.chmod(0o755)
    marca.write_text(esperado + "\n")
    return destino


# ------------------------------------------------------------------- sandbox


def sandbox() -> Path:
    home = CACHE / "home"
    home.mkdir(parents=True, exist_ok=True)
    # `git@github.com:` por HTTPS com a credencial do gh: o job que clona por
    # chave de deploy clona sem ela.
    (home / ".gitconfig").write_text(
        '[url "https://github.com/"]\n'
        "\tinsteadOf = git@github.com:\n"
        '[credential "https://github.com"]\n'
        "\thelper = !gh auth git-credential\n"
    )
    # O runner do GitHub traz o rustup; esta maquina, dentro do sandbox, tambem.
    if not (home / DIR_CARGO / "bin" / "rustup").exists():
        diz(f"{FRACO}instalando o rustup no sandbox (uma vez){FIM}")
        with urllib.request.urlopen("https://sh.rustup.rs", timeout=60) as resposta:
            script = resposta.read()
        ambiente = {
            **os.environ,
            "CARGO_HOME": str(home / DIR_CARGO),
            "RUSTUP_HOME": str(home / ".rustup"),
        }
        subprocess.run(
            [
                "sh",
                "-s",
                "--",
                "-y",
                "--profile",
                "minimal",
                "--default-toolchain",
                "none",
                "--no-modify-path",
            ],
            input=script,
            env=ambiente,
            check=True,
            capture_output=True,
        )
    return home


def python_no_cache_de_ferramentas(home: Path) -> None:
    """O `setup-python` procura o Python pre-compilado PARA UBUNTU e nao acha
    versao para o Debian (medido: "3.13 ... was not found for Debian 13"). O
    runner do GitHub traz as suas no tool cache; aqui, o Python do sistema vai
    para o mesmo lugar, como um venv que enxerga os pacotes do sistema. Uma
    versao que esta maquina nao tem continua falhando, e com razao."""
    versao = subprocess.run(
        ["python3", "-c", "import platform; print(platform.python_version())"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    base = home / ".cache" / "act" / "tool_cache" / "Python" / versao
    if (base / "x64.complete").exists():
        return
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    subprocess.run(
        ["python3", "-m", "venv", "--system-site-packages", str(base / "x64")], check=True
    )
    (base / "x64.complete").touch()


def trocas() -> Path:
    pasta = CACHE / "trocadas"
    vazia = (
        "name: trocada pelo validar-local\n"
        "description: so faz sentido no GitHub, ou mexeria nesta maquina\n"
        "runs:\n  using: composite\n  steps:\n"
        "    - shell: bash\n      run: |\n"
        "        echo 'validar-local: action trocada por uma vazia'\n"
    )
    for sub in ("vazia", "codeql/upload-sarif", "codeql/init", "codeql/analyze"):
        (pasta / sub).mkdir(parents=True, exist_ok=True)
        (pasta / sub / ACTION_YML).write_text(vazia)
    return pasta


def trufflehog_limitado(sha: str) -> Path:
    """A MESMA action do TruffleHog, no mesmo SHA, com um limite de memoria no
    conteiner. Ela varre o historico inteiro num conteiner filho do dockerd — o
    `oom_score_adj` desta validacao nao chega la — e numa maquina de 4 GB o
    OOM killer escolheu a sessao de quem chamou, quatro vezes (2026-09-24).
    Com o limite, estourar mata so o conteiner, e o passo falha dizendo."""
    destino = CACHE / "acoes" / f"trufflehog-{sha[:12]}"
    alvo = destino / ACTION_YML
    if not alvo.exists():
        url = f"https://raw.githubusercontent.com/trufflesecurity/trufflehog/{sha}/action.yml"
        with urllib.request.urlopen(url, timeout=60) as resposta:  # noqa: S310 - SHA fixo
            texto = resposta.read().decode()
        if texto.count("docker run --rm ") != 1:
            raise RuntimeError(f"trufflehog {sha[:12]}: a action mudou de forma; revise a troca")
        memoria = os.environ.get("VALIDAR_LOCAL_MEMORIA_SCANNER", "2g")
        texto = texto.replace(
            "docker run --rm ", f"docker run --rm --memory={memoria} --memory-swap={memoria} "
        )
        destino.mkdir(parents=True, exist_ok=True)
        alvo.write_text(texto)
    return destino


def upload_v4() -> Path:
    tag, sha = UPLOAD_V4
    destino = CACHE / "acoes" / f"upload-artifact-{tag}"
    if not (destino / ACTION_YML).exists():
        shutil.rmtree(destino, ignore_errors=True)
        git(
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            tag,
            "https://github.com/actions/upload-artifact",
            str(destino),
        )
    medido = git("rev-parse", "HEAD", cwd=destino)
    if medido != sha:
        raise RuntimeError(f"upload-artifact {tag} em {medido}, esperado {sha} — a tag mudou")
    return destino


def um_por_vez(texto: str) -> str:
    """`max-parallel: 1` em toda `strategy:`. O act roda as entradas de uma
    matriz AO MESMO TEMPO, apesar do `--concurrent-jobs 1` (medido: os dois
    jobs de cenarios do E2E comecaram na mesma linha do log, e o segundo
    `apt-get update` achou a trava do primeiro). Nesta maquina elas dividem
    Docker, portas e memoria; no GitHub cada uma tinha a sua VM."""
    linhas = texto.split("\n")
    saida: list[str] = []
    i = 0
    while i < len(linhas):
        linha = linhas[i]
        saida.append(linha)
        achado = re.match(r"^(\s*)strategy:\s*(#.*)?$", linha)
        if achado:
            recuo = len(achado.group(1))
            j = i + 1
            bloco: list[str] = []
            while j < len(linhas) and (
                not linhas[j].strip() or len(linhas[j]) - len(linhas[j].lstrip()) > recuo
            ):
                bloco.append(linhas[j])
                j += 1
            filhos = [b for b in bloco if b.strip()]
            recuo_filho = (len(filhos[0]) - len(filhos[0].lstrip())) if filhos else recuo + 2
            if any(re.match(r"^\s*max-parallel:", b) for b in bloco):
                bloco = [re.sub(r"^(\s*max-parallel:).*$", r"\1 1", b) for b in bloco]
            else:
                bloco.insert(0, " " * recuo_filho + "max-parallel: 1  # validar-local")
            saida.extend(bloco)
            i = j
            continue
        i += 1
    return "\n".join(saida)


def sem_publicar(raiz: Path) -> int:
    """Nada e publicado daqui. O job `imagem` da esteira publica a tag
    `sha-<commit>` no GHCR TAMBEM no PR, de proposito (ver o README); numa
    validacao local isso seria efeito colateral. Todo `push: true` dos
    workflows vira `push: false` na COPIA da esteira: a imagem e construida e
    varrida do mesmo jeito, e nao sai da maquina."""
    trocas_feitas = 0
    base = raiz.resolve()
    for arquivo in (raiz / DIR_GITHUB).rglob(GLOB_YAML):
        # So o que resolve para DENTRO da copia: um link simbolico na esteira
        # que apontasse para fora faria esta reescrita mexer onde nao deve.
        if arquivo.is_symlink() or not arquivo.resolve().is_relative_to(base):
            continue
        original = arquivo.read_text()
        texto = um_por_vez(original) if "strategy:" in original else original
        texto, n = re.subn(
            r"(?m)^(\s*)push: true\s*$", r"\1push: false  # validar-local: nada e publicado", texto
        )
        trocas_feitas += n
        if texto != original:
            # O caminho sai do rglob de uma pasta FIXA do cache e passou pela
            # contencao acima; o conteudo e o do proprio arquivo, reescrito no
            # mesmo lugar. Nao ha entrada de fora aqui (falso positivo do S2083).
            arquivo.write_text(texto)  # NOSONAR
    return trocas_feitas


def esteira(local: bool) -> Path:
    """Uma COPIA da esteira, nunca a arvore de ninguem: e nela que o
    `sem_publicar` mexe."""
    destino = CACHE / "esteira"
    shutil.rmtree(destino, ignore_errors=True)
    destino.mkdir(parents=True)
    if local:
        # A arvore como esta, com o que ainda nao foi commitado.
        arquivos = git("ls-files", "--cached", "--others", "--exclude-standard", cwd=ESTEIRA)
        for nome in arquivos.splitlines():
            origem = ESTEIRA / nome
            if origem.is_file():
                (destino / nome).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origem, destino / nome)
    else:
        git("fetch", "--quiet", "origin", "main", cwd=ESTEIRA)
        arquivo = subprocess.run(
            ["git", "archive", REF_MAIN], cwd=ESTEIRA, capture_output=True, check=True
        ).stdout
        with tempfile.TemporaryDirectory() as tmp:
            pacote = Path(tmp) / "esteira.tar"
            pacote.write_bytes(arquivo)
            with tarfile.open(pacote) as tar:
                tar.extractall(destino, filter="data")
    sem_publicar(destino)
    return destino


# ------------------------------------------------------------------ workflows


def ler(yq: Path, arquivo: Path) -> dict:
    saida = subprocess.run(
        [str(yq), "-o=json", ".", str(arquivo)], capture_output=True, text=True, check=True
    )
    return json.loads(saida.stdout or "{}") or {}


def gatilhos(workflow: dict) -> set[str]:
    # O YAML 1.1 le a chave `on` como verdadeiro: o yq a entrega como "on" ou "true".
    on = workflow.get("on", workflow.get("true"))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return set(on)
    if isinstance(on, dict):
        return set(on)
    return set()


def derivar(arquivo: Path, destino: Path) -> tuple[Path, bool]:
    """Copia o workflow com o Sonar desligado, quando ele chama a esteira."""
    texto = arquivo.read_text()
    novo, trocas_sonar = re.subn(r"(?m)^(\s*sonar_projeto:).*$", r'\1 ""', texto)
    novo = um_por_vez(novo)
    saida = destino / arquivo.name
    saida.write_text(novo)
    return saida, trocas_sonar > 0


def shas(pastas: list[Path]) -> dict[str, set[str]]:
    achados: dict[str, set[str]] = {nome: set() for nome in TROCADAS}
    padrao = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)(?:/[\w./-]+)?@([0-9a-f]{40})")
    for pasta in pastas:
        for arquivo in pasta.rglob(GLOB_YAML):
            if ".git" in arquivo.parts:
                continue
            for repo, sha in padrao.findall(arquivo.read_text(errors="replace")):
                if repo in achados:
                    achados[repo].add(sha)
    return achados


def refs_da_esteira(arquivos: list[Path]) -> set[str]:
    padrao = re.compile(rf"{REPO_ESTEIRA}/[^@\s]+@([\w.-]+)")
    return {ref for a in arquivos for ref in padrao.findall(a.read_text())}


# ------------------------------------------------------------------- segredos


def pedidos_de_segredo(arquivos: list[Path]) -> dict[str, list[str]]:
    """Cada `secrets.NOME` dos workflows, com os arquivos que o pedem."""
    pedidos: dict[str, list[str]] = {}
    for arquivo in arquivos:
        for nome in re.findall(r"secrets\.([A-Za-z_]\w*)", arquivo.read_text()):
            onde = pedidos.setdefault(nome, [])
            if nome != "GITHUB_TOKEN" and arquivo.name not in onde:
                onde.append(arquivo.name)
    pedidos.pop("GITHUB_TOKEN", None)
    return pedidos


def valores_dos_envs(repo: Path, pedidos: dict[str, list[str]]) -> dict[str, str]:
    """Os arquivos `~/.config/<repositorio>/*.env`, lidos pelo bash, porque e
    bash que os escreve (o basalto grava com `printf %q`). O valor vem pelo
    stdout de um processo filho, e nunca aparece na tela nem num argumento."""
    pasta = Path.home() / ".config" / repo.name
    envs = sorted(pasta.glob("*.env")) if pasta.is_dir() else []
    if not envs:
        return {}
    leitura = subprocess.run(
        [
            "bash",
            "-c",
            'set -a; for f in "$@"; do . "$f"; done; '
            'python3 -c "import json,os; print(json.dumps(dict(os.environ)))"',
            "ler",
            *map(str, envs),
        ],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(Path.home())},
        check=True,
    )
    return {k: v for k, v in json.loads(leitura.stdout).items() if k in pedidos}


def token_do_gh() -> str:
    return (
        os.environ.get("NODE_AUTH_TOKEN")
        or subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    )


def segredos(repo: Path, arquivos: list[Path]) -> tuple[dict[str, str], dict[str, list[str]]]:
    pedidos = pedidos_de_segredo(arquivos)
    valores = valores_dos_envs(repo, pedidos)
    valores.update({k: os.environ[k] for k in pedidos if os.environ.get(k)})
    if "GH_PACKAGES_TOKEN" in pedidos and "GH_PACKAGES_TOKEN" not in valores:
        token = token_do_gh()
        if token:
            valores["GH_PACKAGES_TOKEN"] = token
    if "GITOPS_SSH_KEY" in pedidos:
        valores["GITOPS_SSH_KEY"] = "validar-local-nao-usa-chave"
    faltam = {k: v for k, v in pedidos.items() if k not in valores and k != "SONAR_TOKEN"}
    return valores, faltam


# --------------------------------------------------------------------- lint


def achados(comando: list[str], cwd: Path, arquivos: list[str]) -> list[dict]:
    saida = subprocess.run(
        [*comando, "-format", "{{json .}}", *arquivos], cwd=cwd, capture_output=True, text=True
    )
    return json.loads(saida.stdout or "[]") or []


def lint(actionlint: Path, repo: Path) -> bool:
    """So o achado NOVO reprova: um aviso antigo da main nao trava ninguem.

    Olhar so os arquivos que mudaram nao basta — o arquivo que mudou traz os
    avisos antigos dele junto. Entao o actionlint roda na versao da base e na
    de agora, e a comparacao e pelo texto do achado, sem o numero da linha
    (qualquer linha acrescentada acima desloca todos os de baixo).
    """
    base = git("merge-base", REF_MAIN, "HEAD", cwd=repo, verificar=False) or REF_MAIN
    nomes = set(git("diff", "--name-only", base, "--", ".github/workflows", cwd=repo).split())
    nomes |= set(
        git("ls-files", "--others", "--exclude-standard", ".github/workflows", cwd=repo).split()
    )
    mudados = [n for n in sorted(nomes) if n.endswith((".yml", ".yaml")) and (repo / n).exists()]
    if not mudados:
        diz(f"{FRACO}actionlint: nenhum workflow mudou em relacao a main{FIM}")
        return True
    comando = [str(actionlint)]
    if shutil.which("shellcheck"):
        comando += ["-shellcheck", shutil.which("shellcheck")]

    def chave(a: dict) -> tuple[str, str, str]:
        return (a.get("filepath", ""), a.get("kind", ""), a.get("message", ""))

    antes: dict[tuple[str, str, str], int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for nome in mudados:
            conteudo = subprocess.run(
                ["git", "show", f"{base}:{nome}"], cwd=repo, capture_output=True
            )
            if conteudo.returncode == 0:
                (Path(tmp) / nome).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / nome).write_bytes(conteudo.stdout)
        na_base = [n for n in mudados if (Path(tmp) / n).exists()]
        for a in achados(comando, Path(tmp), na_base) if na_base else []:
            antes[chave(a)] = antes.get(chave(a), 0) + 1
    novos = []
    for a in achados(comando, repo, mudados):
        if antes.get(chave(a), 0) > 0:
            antes[chave(a)] -= 1
        else:
            novos.append(a)
    if not novos:
        diz(f"{VERDE}actionlint{FIM}: nenhum achado novo em {', '.join(mudados)}")
        return True
    diz(f"{VERMELHO}actionlint{FIM}: {len(novos)} achado(s) novo(s):")
    for a in novos:
        diz(f"  {a.get('filepath')}:{a.get('line')}:{a.get('column')}: {a.get('message')}")
    return False


# ---------------------------------------------------------------------- act


def copia_do_repo(repo: Path) -> Path:
    """O act roda a partir de uma COPIA, e nunca da arvore de ninguem: o que o
    git versiona e o que ele nao ignora (como um checkout do CI, sem
    `node_modules` nem `target`), mais o `.git` com escrita liberada. Sem a
    escrita, um job com dois checkouts caia no segundo: o act copia o
    repositorio de novo, e os objetos do git nascem somente-leitura (medido no
    CI do proprio github-workflows)."""
    destino = CACHE / "copias" / repo.name
    shutil.rmtree(destino, ignore_errors=True)
    destino.mkdir(parents=True)
    nomes = git("ls-files", "--cached", "--others", "--exclude-standard", "-z", cwd=repo)
    for nome in filter(None, nomes.split("\0")):
        origem = repo / nome
        if origem.is_file() or origem.is_symlink():
            (destino / nome).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origem, destino / nome, follow_symlinks=False)
    shutil.copytree(
        Path(git("rev-parse", "--absolute-git-dir", cwd=repo)), destino / ".git", symlinks=True
    )
    for caminho in (destino / ".git").rglob("*"):
        if caminho.is_file() and not caminho.is_symlink():
            caminho.chmod(caminho.stat().st_mode | 0o200)
    return destino


def evento(repo: Path, destino: Path) -> Path:
    cabeca = git("rev-parse", "HEAD", cwd=repo)
    base = git("rev-parse", REF_MAIN, cwd=repo)
    nome = git("config", "--get", "remote.origin.url", cwd=repo)
    dono_repo = re.sub(r"(\.git)?$", "", nome.split("github.com")[-1].lstrip(":/"))
    dono, _, curto = dono_repo.partition("/")
    dados = {
        "pull_request": {
            "number": 0,
            "draft": False,
            "head": {"ref": git("branch", "--show-current", cwd=repo) or "HEAD", "sha": cabeca},
            "base": {"ref": "main", "sha": base},
        },
        "repository": {"full_name": dono_repo, "name": curto, "owner": {"login": dono}},
    }
    arquivo = destino / "evento.json"
    arquivo.write_text(json.dumps(dados))
    return arquivo


def endereco_docker0() -> str:
    """O servidor de cache do act precisa ser alcancavel de DENTRO de conteiner:
    o `cache-to: type=gha` do build roda no BuildKit, que e um conteiner, e la
    o 127.0.0.1 e ele mesmo (medido: o build compilou e caiu exportando o
    cache). A docker0 e alcancavel pelos conteineres e nao sai da maquina."""
    saida = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "docker0"], capture_output=True, text=True
    ).stdout
    achado = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", saida)
    return achado.group(1) if achado else "127.0.0.1"


def esperar_apt(limite_s: int = 900) -> None:
    """Espera o apt desta maquina ficar ocioso. O `--with-deps` do Playwright
    roda `apt-get update`, e a atualizacao automatica da maquina (apt-daily,
    unattended-upgrades) segura a trava de `lists` — que o apt NAO espera, nem
    com `DPkg::Lock::Timeout` (medido). No runner do GitHub ninguem disputa o apt.
    O `unattended-upgrade-shutdown --wait-for-signal` vive sempre no ar e nao conta."""
    inicio = time.monotonic()
    avisou = False
    while time.monotonic() - inicio < limite_s:
        saida = subprocess.run(
            ["pgrep", "-a", "-x", "apt|apt-get|dpkg|unattended-upgr"],
            capture_output=True,
            text=True,
        ).stdout
        ocupados = [ln for ln in saida.splitlines() if "--wait-for-signal" not in ln]
        if not ocupados:
            return
        if not avisou:
            diz(
                f"{FRACO}o apt desta maquina esta ocupado (atualizacao automatica?); esperando{FIM}"
            )
            avisou = True
        time.sleep(10)


def linha_de_job(linha: str) -> tuple[str, str] | None:
    """`[Workflow/job   ] resto` -> ("Workflow/job", "resto"). Sem regex: a que
    havia aqui tinha backtracking super-linear (Sonar S8786)."""
    if not linha.startswith("["):
        return None
    fim = linha.find("]")
    if fim <= 1:
        return None
    return linha[1:fim].strip(), linha[fim + 1 :].strip()


def rodar_act(
    act: Path,
    repo: Path,
    workflow: Path,
    job: str | None,
    home: Path,
    mapa: list[str],
    evento_json: Path,
    segredos_val: dict[str, str],
    log: Path,
) -> dict[str, str]:
    ambiente = {
        **{k: v for k, v in os.environ.items() if k not in ("HOME",)},
        **segredos_val,
        "HOME": str(home),
        "GH_CONFIG_DIR": str(Path.home() / ".config" / "gh"),
        "CARGO_HOME": str(home / DIR_CARGO),
        "RUSTUP_HOME": str(home / ".rustup"),
        # O runner do GitHub deixa o pip instalar; o Debian recusa sem isto.
        "PIP_BREAK_SYSTEM_PACKAGES": "1",
        # O runner tem 16 GB para 4 nucleos; uma maquina de 4 GB com o cargo em
        # 4 processos estoura a memoria (medido em 2026-09-24: o OOM killer
        # matou a sessao duas vezes no meio do audit). Dois processos cabem.
        "CARGO_BUILD_JOBS": os.environ.get("CARGO_BUILD_JOBS", "2"),
        "PATH": os.pathsep.join(
            [
                str(home / DIR_CARGO / "bin"),
                str(home / ".local" / "bin"),
                str(CACHE / "bin"),
                os.environ["PATH"],
            ]
        ),
    }
    comando = [
        str(act),
        "pull_request",
        "-W",
        str(workflow),
        "-e",
        str(evento_json),
        *[f"-P={rotulo}=-self-hosted" for rotulo in ROTULOS],
        "--concurrent-jobs",
        "1",
        "--secret-file",
        "/dev/null",
        "--artifact-server-path",
        str(CACHE / "artefatos"),
        "--artifact-server-addr",
        "127.0.0.1",
        "--cache-server-path",
        str(CACHE / "actcache"),
        "--cache-server-addr",
        endereco_docker0(),
        *[arg for m in mapa for arg in ("--local-repository", m)],
        *[arg for nome in segredos_val for arg in ("-s", nome)],
    ]
    if job:
        comando += ["-j", job]
    esperar_apt()
    jobs: dict[str, str] = {}
    ansi = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
    with log.open("w", buffering=1) as saida:
        processo = subprocess.Popen(
            comando,
            cwd=repo,
            env=ambiente,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert processo.stdout is not None
        for linha in processo.stdout:
            saida.write(linha)
            limpa = ansi.sub("", linha).rstrip()
            achado = linha_de_job(limpa)
            if "Could not get lock /var/lib/apt" in limpa:
                jobs["(apt ocupado)"] = "ambiente"
            if not achado:
                continue
            nome, resto = achado
            if "🚀  Start image" in resto or ("⭐ Run Set up job" in resto and nome not in jobs):
                jobs.setdefault(nome, "rodando")
                diz(f"  {FRACO}…{FIM} {nome}")
            elif "❌  Failure - Main" in resto:
                diz(f"    {VERMELHO}falhou:{FIM} {resto.split('Main', 1)[1].strip()}")
            elif "🏁  Job succeeded" in resto:
                jobs[nome] = "passou"
                diz(f"  {VERDE}passou{FIM} {nome}")
            elif "🏁  Job failed" in resto:
                jobs[nome] = "reprovou"
                diz(f"  {VERMELHO}reprovou{FIM} {nome}")
        codigo = processo.wait()
    if codigo != 0 and all(v != "reprovou" for v in jobs.values()):
        jobs["(act)"] = "reprovou"
    return jobs


# --------------------------------------------------------------------- main


def registro(repo: Path) -> Path:
    return Path(git("rev-parse", "--absolute-git-dir", cwd=repo)) / "validar-local"


def argumentos() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Detalhes no cabecalho de bin/validar_local.py.",
    )
    ap.add_argument("-W", "--workflow", action="append", help="so este workflow (repetivel)")
    ap.add_argument("-j", "--job", help="so este job (repassado ao act)")
    ap.add_argument("--esteira-local", action="store_true", help="usa a esteira desta maquina")
    ap.add_argument("--listar", action="store_true", help="mostra o que rodaria e sai")
    ap.add_argument("--status", action="store_true", help="o HEAD ja passou?")
    return ap.parse_args()


def argumento_invalido(args: argparse.Namespace) -> str | None:
    """-W e um nome de arquivo em .github/workflows, e -j um id de job: o que
    nao casar nao chega ao act (nem vira caminho fora da pasta)."""
    for nome in args.workflow or []:
        if not NOME_WORKFLOW.fullmatch(nome):
            return f"-W espera o nome de um arquivo de .github/workflows, e nao {nome!r}"
    if args.job is not None and not ID_JOB.fullmatch(args.job):
        return f"-j espera o id de um job, e nao {args.job!r}"
    return None


def mostrar_status(repo: Path, arvore: str) -> int:
    marca = registro(repo) / arvore
    if marca.exists():
        diz(f"{VERDE}validado{FIM}: {marca.read_text().strip()}")
        return 0
    diz(f"{AMARELO}nao validado{FIM}: a arvore do HEAD ({arvore[:12]}) nunca passou aqui")
    return 1


def maquina_pronta() -> str | None:
    """None se a maquina aguenta; senao, o que falta."""
    # Se a memoria acabar, que o kernel mate a VALIDACAO, e nao a sessao de
    # quem a chamou: o score passa para os filhos (act, cargo, semgrep). Medido
    # em 2026-09-24, numa maquina de 4 GB, o OOM killer escolheu a sessao do
    # Claude Code tres vezes antes disto. Subir o proprio score nao pede
    # privilegio.
    with contextlib.suppress(OSError):
        Path("/proc/self/oom_score_adj").write_text("1000")
    livre = memoria_livre_mb()
    if livre is not None and livre < 2048:
        diz(
            f"{AMARELO}so {livre} MB de memoria livre{FIM}: a compilacao do Rust pode "
            "acionar o OOM killer. Feche o que puder (um tmpfs cheio conta como memoria)."
        )
    faltam_runner = falta_do_runner()
    if faltam_runner:
        return (
            "falta nesta maquina o que o runner do GitHub traz: "
            + "; ".join(faltam_runner)
            + ". No Debian: sudo apt install python3-yaml jq zstd unzip"
        )
    return None


def escolher(args: argparse.Namespace, pasta_wf: Path, todos: list[Path]) -> list[Path] | str:
    if args.workflow:
        escolhidos = [pasta_wf / w for w in args.workflow]
        faltando = [str(w) for w in escolhidos if not w.exists()]
        if faltando:
            return f"workflow nao existe: {', '.join(faltando)}"
        return escolhidos
    yq = ferramenta("yq")
    escolhidos = [w for w in todos if "pull_request" in gatilhos(ler(yq, w))]
    return escolhidos or "nenhum workflow roda em pull_request"


def shas_de(pastas: list[Path], acao: str) -> set[str]:
    padrao = re.compile(rf"uses:\s*{re.escape(acao)}@([0-9a-f]{{40}})")
    return {
        sha
        for pasta in pastas
        for arq in pasta.rglob(GLOB_YAML)
        for sha in padrao.findall(arq.read_text(errors="replace"))
    }


def mapa_de_acoes(repo: Path, raiz_esteira: Path) -> list[str]:
    """O `--local-repository` de cada action trocada, e da propria esteira."""
    pasta_trocas = trocas()
    mapa = [f"{REPO_ESTEIRA}@main={raiz_esteira}"]
    pastas = [repo / DIR_GITHUB, raiz_esteira / DIR_GITHUB]
    novos_upload = shas_de(pastas, "actions/upload-artifact") - {UPLOAD_V4[1]}
    if novos_upload:
        v4 = upload_v4()
        mapa += [f"actions/upload-artifact@{sha}={v4}" for sha in sorted(novos_upload)]
    for sha in sorted(shas_de(pastas, "trufflesecurity/trufflehog")):
        mapa.append(f"trufflesecurity/trufflehog@{sha}={trufflehog_limitado(sha)}")
    for repo_acao, conjunto in shas(pastas).items():
        alvo = pasta_trocas / ("codeql" if repo_acao == "github/codeql-action" else "vazia")
        mapa += [f"{repo_acao}@{sha}={alvo}" for sha in sorted(conjunto)]
    return mapa


def resumir(resultado: dict[str, dict[str, str]], minutos: float) -> bool:
    ok = True
    diz("\nResumo")
    for nome, jobs in resultado.items():
        if jobs.pop("(apt ocupado)", None):
            diz(
                f"  {AMARELO}{nome}: o apt desta maquina estava ocupado{FIM} (outro job ou a "
                "atualizacao automatica) quando um passo o chamou — falha de ambiente, "
                "e nao do codigo. Rode de novo."
            )
        ruins = [j for j, r in jobs.items() if r != "passou"]
        diz(
            f"  {nome}: {len(jobs) - len(ruins)}/{len(jobs)} jobs verdes"
            + (f" — {VERMELHO}{', '.join(ruins)}{FIM}" if ruins else "")
        )
        ok = ok and not ruins and bool(jobs)
    diz(f"  {minutos:.1f} min nesta maquina; o Sonar so o CI mede.")
    return ok


def concluir(
    repo: Path, arvore: str, args: argparse.Namespace, suja: bool, feito: str, minutos: float
) -> int:
    if args.job or args.workflow:
        diz(f"\n{VERDE}verde{FIM} no que foi pedido — sem registro: antes do PR, rode tudo.")
    elif suja:
        diz(f"\n{VERDE}verde{FIM}, mas com mudanca por commitar — sem registro. Commite e rode.")
    else:
        pasta = registro(repo)
        pasta.mkdir(exist_ok=True)
        (pasta / arvore).write_text(
            f"{dt.datetime.now().isoformat(timespec='seconds')} {feito} em {minutos:.1f} min\n"
        )
        diz(f"\n{VERDE}VERDE{FIM}: a arvore {arvore[:12]} passou; registrado.")
    return 0


def cabecalho(repo: Path, escolhidos: list[Path], args: argparse.Namespace) -> None:
    diz(f"validar-local em {repo.name} ({git('branch', '--show-current', cwd=repo) or 'HEAD'})")
    diz(
        f"  workflows: {', '.join(w.name for w in escolhidos)}"
        + (f"  job: {args.job}" if args.job else "")
    )
    diz(f"  esteira:   {'esta maquina, como esta' if args.esteira_local else REF_MAIN}")
    diz(
        "  trocadas:  harden-runner, codeql-action, docker/login-action; Sonar desligado;"
        " nada e publicado (push: false)"
    )


def rodar_workflows(
    repo: Path,
    escolhidos: list[Path],
    args: argparse.Namespace,
    raiz_esteira: Path,
    valores: dict[str, str],
) -> dict[str, dict[str, str]]:
    act = ferramenta("act")
    home = sandbox()
    python_no_cache_de_ferramentas(home)
    mapa = mapa_de_acoes(repo, raiz_esteira)
    resultado: dict[str, dict[str, str]] = {}
    logs = CACHE / "logs"
    logs.mkdir(exist_ok=True)
    carimbo = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory(prefix="validar-local-") as tmp:
        tmp_path = Path(tmp)
        ev = evento(repo, tmp_path)
        copia = copia_do_repo(repo)
        for workflow in escolhidos:
            derivado, sem_sonar = derivar(workflow, tmp_path)
            log = logs / f"{repo.name}-{carimbo}-{workflow.stem}.log"
            diz(f"\n{workflow.name}{' (Sonar desligado)' if sem_sonar else ''} — log em {log}")
            resultado[workflow.name] = rodar_act(
                act, copia, derivado, args.job, home, mapa, ev, valores, log
            )
    return resultado


def main() -> int:
    args = argumentos()
    invalido = argumento_invalido(args)
    if invalido:
        return falha(invalido)
    try:
        repo = Path(git("rev-parse", "--show-toplevel"))
    except RuntimeError:
        return falha("rode dentro de um repositorio git")
    arvore = git("rev-parse", "HEAD^{tree}", cwd=repo)
    antes = git("status", "--porcelain", cwd=repo)
    if args.status:
        return mostrar_status(repo, arvore)

    pasta_wf = repo / DIR_GITHUB / "workflows"
    todos = sorted([*pasta_wf.glob("*.yml"), *pasta_wf.glob("*.yaml")])
    if not todos:
        return falha(f"{repo.name} nao tem .github/workflows")
    problema = maquina_pronta()
    if problema:
        return falha(problema)

    CACHE.mkdir(parents=True, exist_ok=True)
    trava = (CACHE / "trava").open("w")
    try:
        fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return falha("outro validar-local esta rodando (os dois disputariam Docker e portas)")

    escolhidos = escolher(args, pasta_wf, todos)
    if isinstance(escolhidos, str):
        return falha(escolhidos)
    refs = refs_da_esteira(escolhidos) - {"main"}
    if refs:
        return falha(
            f"a esteira e chamada em {', '.join(sorted(refs))}, e nao em @main: as actions que "
            "ela traz nesse ref nao estao mapeadas, e o harden-runner rodaria nesta maquina"
        )

    git("fetch", "--quiet", "origin", "main", cwd=repo, verificar=False)
    raiz_esteira = esteira(args.esteira_local)
    # Quem chama a esteira repassa cada segredo pelo nome (`secrets:`), entao os
    # pedidos sao os dos workflows escolhidos.
    valores, faltam = segredos(repo, escolhidos)
    cabecalho(repo, escolhidos, args)
    for nome, onde in sorted(faltam.items()):
        diz(f"  {AMARELO}sem valor local{FIM}: {nome} (pedido em {', '.join(onde)})")
    if args.listar:
        return 0

    inicio = time.monotonic()
    ok = lint(ferramenta("actionlint"), repo)
    resultado = rodar_workflows(repo, escolhidos, args, raiz_esteira, valores)
    minutos = (time.monotonic() - inicio) / 60
    # O act roda numa copia do repositorio, e o que ele fizer nao pode aparecer
    # aqui. Se aparecer, e defeito deste script ou de um passo, e a arvore que
    # vai para o PR nao e mais a que foi validada.
    depois = git("status", "--porcelain", cwd=repo)
    if depois != antes:
        novos = sorted(set(depois.splitlines()) - set(antes.splitlines()))
        diz(f"{VERMELHO}a rodada mexeu na arvore{FIM}: {', '.join(novos) or depois}")
        ok = False
    ok = resumir(resultado, minutos) and ok
    if not ok:
        diz(f"\n{VERMELHO}REPROVADO{FIM}: conserte aqui antes de empurrar.")
        return 1
    return concluir(repo, arvore, args, bool(antes), ", ".join(resultado), minutos)


if __name__ == "__main__":
    sys.exit(main())

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
    o Sonar                       `sonar_projeto` vazio, e `sonar_dispensa`
                                  com o motivo (sem ele o portao da esteira
                                  reprova "Sonar desligado sem motivo"): a
                                  analise e do SonarQube, e so o CI a mede;
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
    - job pesado roda sozinho (ver JOBS EM PARALELO).

A esteira usada e a `main` de `origin` deste repositorio (`@main` e o que o CI
de quem chama usa); `--esteira-local` usa a copia daqui como esta, para validar
uma mudanca na propria esteira antes do PR dela.

JOBS EM PARALELO
----------------
Ate 2026-10 os jobs rodavam um de cada vez, porque a caixa tinha 4 GB e o
`cargo`, o Semgrep e o TruffleHog juntos acionavam o OOM killer. Com 8 GB e 6
vCPU, jobs LEVES rodam juntos — ate VALIDAR_LOCAL_JOBS (padrao 2; `1` e o
comportamento antigo, sem nada do que segue) — e o PESADO continua sozinho.

Pesado e o passo que compila (`cargo build/test/check/clippy/install/llvm-cov`,
`cargo sqlx prepare`, `dotnet build/test/publish`, `go build/test`), sobe o E2E
(Playwright, `run-families`, `docker compose`), constroi imagem (`docker
build`, buildx, `docker/build-push-action`), roda o Sonar, ou mexe no apt (a
trava do apt e uma so). A classificacao le o PROPRIO workflow, sem lista de
job: o nome e o `run:` de cada passo, os scripts do repositorio que o `run:`
cita (ate tres niveis), e o `uses:`. Um `run:` que e so uma expressao
(`${{ inputs.comando_testes }}`) conta como pesado. `cargo tree`, `cargo audit`
e `cargo --version` nao compilam, e nao contam. Os passos de dentro de uma
composite action nao sao lidos.

Como: na COPIA de cada workflow (o derivado e os da esteira), todo job com
`steps:` ganha um passo "validar-local: espera a vez" antes do primeiro, que
pega um lugar de leve, e outro antes de cada passo pesado, com o MESMO `if:`
dele, que espera ficar sozinho — e so espera se o passo for rodar (`matrix.c.
linguagem == 'rust'` e avaliado pelo act, na hora). A vez e uma fila na ordem
de chegada, em ~/.cache/validar-local/vez: leve ocupa um lugar, pesado ocupa
todos; quem espera nao segura lugar (o leve que vira pesado devolve o dele), e
por isso ninguem trava. Quando o act diz que o job acabou (passos, `post`,
limpeza), o validar-local devolve a vez dele. O `act` roda com
`--concurrent-jobs N` e as matrizes com `max-parallel: N`.

O passo posto nunca reprova o job: se a vez falhar, ele segue sem esperar e
diz por que. A espera nao conta no `timeout-minutes` do job, porque o act nao
o aplica em modo host (medido: um job de `timeout-minutes: 1` com `sleep 75`
passou). O texto e reescrito, e nao reemitido, para nao perder comentario
nem forma; o yq confere que o workflow, sem os passos postos, e o MESMO. Se
nao for, ou se um job chamar um workflow reutilizavel LOCAL
(`uses: ./.github/workflows/x.yml`, que roda da copia do repositorio e fica
sem a vez) num workflow que tem pesado, aquele workflow roda um job por vez.

Com jobs juntos, as linhas deles se intercalam no log do workflow; cada job
tem o seu log inteiro em `<log do workflow sem .log>/<job>.log`. O resultado
de cada job e o registro (`--status`) nao mudam.

Medido nesta caixa (8 GB, 6 vCPU) em 2026-10-24, no `ci.yml` do basalto
(`--esteira-local -W ci.yml`), somando o RSS da arvore do validar-local a cada
2 s e anotando quem tinha a vez: dois leves juntos chegaram a 1,8 GB (o vitest
com cobertura do frontend ao lado do backend antes do clippy); um pesado
sozinho, a 1,7 GB; a rodada um-por-vez de antes, a 2,3 GB (o cargo). A memoria
livre da caixa nao desceu de 2,8 GB em nenhuma das rodadas. Tempo, em duas
rodadas de antes e depois: 18,1 -> 14,0 min e 15,1 -> 12,2 min, com o mesmo
resultado em cada job.

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
    VALIDAR_LOCAL_JOBS=1 validar-local   um job por vez, como antes de 2026-10
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
import typing
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


def limpar_rodadas_do_act() -> int:
    """O act, em modo host, cria para cada job uma pasta de nome sorteado em
    ~/.cache/act do sandbox (16 hex: act/, hostexecutor/ e tmp/, com a copia do
    repositorio e o `target` do Rust, ate 4 GB) e nao a apaga. Nenhuma e
    reaproveitada, porque o nome e sorteado de novo a cada job. Medido em
    2026-10-17 na caixa dev: 88 pastas, 13 GB, com o disco em 93%.

    Roda com a trava na mao, entao nenhuma rodada esta usando alguma: sai o que
    a rodada anterior deixou. Fica o resto de ~/.cache/act: o tool_cache (o
    Python, o Node e o syft dos setup-*) e os clones das actions, que sao
    reaproveitados. O cache do `actions/cache` (actcache) o proprio act poda.
    Devolve quantos MB sairam."""
    act = CACHE / "home" / ".cache" / "act"
    if not act.is_dir():
        return 0
    total = 0
    for pasta in act.iterdir():
        if pasta.is_symlink() or not re.fullmatch(r"[0-9a-f]{16}", pasta.name):
            continue
        total += sum(f.lstat().st_size for f in pasta.rglob("*") if not f.is_symlink())
        shutil.rmtree(pasta, ignore_errors=True)
    return total // (1024 * 1024)


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


def max_paralelo(texto: str, n: int) -> str:
    """`max-parallel: n` em toda `strategy:`. O act roda as entradas de uma
    matriz AO MESMO TEMPO, apesar do `--concurrent-jobs` (medido: os dois
    jobs de cenarios do E2E comecaram na mesma linha do log, e o segundo
    `apt-get update` achou a trava do primeiro). Nesta maquina elas dividem
    Docker, portas e memoria; no GitHub cada uma tinha a sua VM. Com `n` > 1,
    quem impede duas entradas pesadas juntas e a vez (ver JOBS EM PARALELO)."""
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
                bloco = [re.sub(r"^(\s*max-parallel:).*$", rf"\1 {n}", b) for b in bloco]
            else:
                bloco.insert(0, " " * recuo_filho + f"max-parallel: {n}  # validar-local")
            saida.extend(bloco)
            i = j
            continue
        i += 1
    return "\n".join(saida)


# ---------------------------------------------------------- jobs em paralelo

# O que faz um passo ser PESADO: compila (cargo, dotnet, go), sobe o E2E
# (Playwright, run-families, docker compose), constroi imagem (docker build,
# buildx), roda o Sonar, ou mexe no apt — a trava do apt desta maquina e uma so.
# `cargo --version`, `cargo tree`, `cargo audit` e `cargo fmt` nao compilam e
# nao contam.
PESADO = re.compile(
    r"\bcargo\s+(?:\+\S+\s+)?(?:build|test|check|clippy|install|llvm-cov|run|doc|bench"
    r"|nextest|tauri|rustc|fix|sqlx\s+prepare)\b"
    r"|\bdotnet\s+(?:build|test|publish|pack)\b"
    r"|\bgo\s+(?:build|test)\b"
    r"|playwright|run-families"
    r"|\bdocker[\s-]compose\b|\bdocker\s+(?:build|buildx)\b|\bbuildx\b"
    r"|sonar-scanner|sonarscanner"
    r"|\bapt(?:-get)?\s+(?:-\S+\s+)*(?:update|install|upgrade)\b",
    re.IGNORECASE,
)
USES_PESADO = re.compile(r"(docker/build-push-action|docker/bake-action|sonarsource/)", re.I)
# Um `run:` que e so uma expressao (`${{ inputs.comando_testes }}`) roda o que
# quem chama mandar: nao da para saber, entao conta como pesado.
SO_EXPRESSAO = re.compile(r"^\s*\$\{\{[^}]*\}\}\s*$")
# Script do repositorio citado num `run:` (`bash .github/scripts/run-families.sh`):
# o comando pesado mora nele, e nao no YAML.
# Uma classe so, sem quantificadores encadeados: a forma do caminho e conferida
# em `scripts_citados` (a regex antiga, com `[\w.-]+` seguido de `\.ext`,
# retrocedia de forma super-linear; Sonar S8786).
CANDIDATO_A_SCRIPT = re.compile(r"(?<![\w$/{}.-])[\w./-]+")
EXTENSOES_DE_SCRIPT = (".sh", ".bash", ".py", ".mjs", ".js", ".ts")
TRECHO_DE_CAMINHO = re.compile(r"[\w.-]+")
PRIMEIRO_TRECHO = re.compile(r"[\w-]+")
NOME_VEZ = "validar-local: espera a vez"
NAO_ARQUIVO = re.compile(r"[^\w.-]+")
PASTA_VEZ = CACHE / "vez"
ESTADO_DA_VEZ = "estado.json"


def paralelos() -> int | str:
    """Quantos jobs leves podem rodar juntos: VALIDAR_LOCAL_JOBS, padrao 2."""
    valor = os.environ.get("VALIDAR_LOCAL_JOBS", "2").strip()
    if not valor.isdigit() or int(valor) < 1:
        return f"VALIDAR_LOCAL_JOBS espera um inteiro >= 1, e nao {valor!r}"
    return int(valor)


def sem_comentario(texto: str) -> str:
    return "\n".join(ln for ln in texto.split("\n") if not ln.lstrip().startswith(("#", "//")))


def _forma_de_script(caminho: str) -> bool:
    """`[./]trecho[/trecho...].ext`: o primeiro trecho sem ponto, os outros com."""
    caminho = caminho.removeprefix("./")
    extensao = next((e for e in EXTENSOES_DE_SCRIPT if caminho.endswith(e)), None)
    if extensao is None:
        return False
    trechos = caminho[: -len(extensao)].split("/")
    return bool(PRIMEIRO_TRECHO.fullmatch(trechos[0])) and all(
        TRECHO_DE_CAMINHO.fullmatch(t) for t in trechos[1:]
    )


def _fronteira(texto: str, posicao: int) -> bool:
    """Fim do texto, ou um caractere que nao e de palavra: o `\\b` de antes."""
    return posicao == len(texto) or not (texto[posicao].isalnum() or texto[posicao] == "_")


def scripts_citados(texto: str) -> list[str]:
    """Scripts do repositorio citados no texto: em cada sequencia de caracteres
    de caminho, o maior comeco com a forma de script que termina numa fronteira
    de palavra."""
    citados = []
    for sequencia in CANDIDATO_A_SCRIPT.findall(texto):
        fim = next(
            (
                f
                for f in range(len(sequencia), 0, -1)
                if _fronteira(sequencia, f) and _forma_de_script(sequencia[:f])
            ),
            None,
        )
        if fim:
            citados.append(sequencia[:fim])
    return citados


def texto_pesado(texto: str, repo: Path, vistos: set[Path], fundo: int = 0) -> bool:
    texto = sem_comentario(texto)
    if PESADO.search(texto):
        return True
    if fundo >= 3:
        return False
    base = repo.resolve()
    for nome in scripts_citados(texto):
        alvo = (repo / nome).resolve()
        if alvo in vistos or not alvo.is_relative_to(base) or not alvo.is_file():
            continue
        vistos.add(alvo)
        if texto_pesado(alvo.read_text(errors="replace"), repo, vistos, fundo + 1):
            return True
    return False


def passo_pesado(passo: dict, repo: Path) -> bool:
    if USES_PESADO.search(str(passo.get("uses") or "")):
        return True
    run = str(passo.get("run") or "")
    if SO_EXPRESSAO.match(run):
        return True
    return texto_pesado(f"{passo.get('name') or ''}\n{run}", repo, set())


def job_pesado(job: dict, repo: Path) -> bool:
    return any(isinstance(p, dict) and passo_pesado(p, repo) for p in job.get("steps") or [])


def ler_texto(yq: Path, texto: str) -> dict:
    saida = subprocess.run(
        [str(yq), "-o=json", ".", "-"], input=texto, capture_output=True, text=True, check=True
    )
    return json.loads(saida.stdout or "{}") or {}


def _recuo(linha: str) -> int:
    return len(linha) - len(linha.lstrip())


def _conta(linha: str) -> bool:
    return bool(linha.strip()) and not linha.lstrip().startswith("#")


def portao(recuo: int, peso: str, condicao: object) -> list[str]:
    """O passo que espera a vez do job. Antes do primeiro passo, a vez de um
    job leve; antes de cada passo pesado, com o MESMO `if:` dele, a vez de
    pesado (o act avalia a condicao na hora, como avaliaria a do passo)."""
    item, p = " " * recuo, " " * (recuo + 2)
    linhas = [f'{item}- name: "{NOME_VEZ}{" (pesado)" if peso == "pesado" else ""}"']
    if condicao is not None and condicao is not True:
        linhas.append(f"{p}if: |2-")
        linhas += [f"{p}  {c}" if c else "" for c in str(condicao).rstrip("\n").split("\n")]
    chamada = f"'{sys.executable}' -I '{Path(__file__).resolve()}' --vez-pasta '{PASTA_VEZ}'"
    return [
        *linhas,
        f"{p}shell: bash",
        f"{p}working-directory: .",
        f"{p}run: |",
        f"{p}  {chamada} --vez {peso}",
    ]


def cercar(texto: str, repo: Path) -> str | None:
    """Poe a vez em todo job com `steps:`. Mexe no TEXTO, para nao perder
    comentario nem forma, e confere pelo yq que o resultado, sem os passos
    postos, e o MESMO workflow. None quando nao da para cercar com certeza."""
    if not _caminhos_cabem_no_passo():
        return None
    yq = ferramenta("yq")
    dados = ler_texto(yq, texto)
    jobs = dados.get("jobs")
    if not isinstance(jobs, dict):
        return texto
    linhas = texto.split("\n")
    antes_de = _portoes_por_linha(linhas, jobs, repo)
    if antes_de is None:
        return None
    saida: list[str] = []
    for n, linha in enumerate(linhas):
        saida += antes_de.get(n, [])
        saida.append(linha)
    novo = "\n".join(saida)
    return novo if _sem_os_portoes(ler_texto(yq, novo)) == dados else None


def _caminhos_cabem_no_passo() -> bool:
    """O passo posto leva estes caminhos entre aspas simples, numa linha so."""
    caminhos = (sys.executable, Path(__file__).resolve(), PASTA_VEZ)
    return not any(c in str(x) for x in caminhos for c in "'\n")


def _fim_do_bloco(linhas: list[str], chave: int, recuo: int) -> int:
    """A primeira linha, depois de `chave`, que ja nao e do bloco dela."""
    fim = chave + 1
    while fim < len(linhas) and not (_conta(linhas[fim]) and _recuo(linhas[fim]) <= recuo):
        fim += 1
    return fim


def _blocos_de_job(linhas: list[str]) -> list[tuple[str, int, int]] | None:
    """Cada job de `jobs:` no texto: o nome, a linha da chave e a linha onde o
    bloco acaba. None quando o texto tem uma forma que este leitor nao conhece."""
    inicio = next((i for i, ln in enumerate(linhas) if re.match(r"^jobs:\s*(#.*)?$", ln)), None)
    if inicio is None:
        return None
    blocos: list[tuple[str, int, int]] = []
    i, recuo_job = inicio + 1, None
    while i < len(linhas):
        if not _conta(linhas[i]):
            i += 1
            continue
        if _recuo(linhas[i]) == 0:
            break
        recuo_job = recuo_job or _recuo(linhas[i])
        chave = re.match(rf"^ {{{recuo_job}}}(['\"]?)([\w-]+)\1\s*:\s*(#.*)?$", linhas[i])
        if _recuo(linhas[i]) != recuo_job or not chave:
            return None
        fim = _fim_do_bloco(linhas, i, recuo_job)
        blocos.append((chave.group(2), i, fim))
        i = fim
    return blocos


def _portoes_por_linha(linhas: list[str], jobs: dict, repo: Path) -> dict[int, list[str]] | None:
    """As linhas da vez a por antes de cada linha do texto. None quando os
    itens de `steps:` no texto nao batem com os passos que o yq leu."""
    blocos = _blocos_de_job(linhas)
    if blocos is None:
        return None
    antes_de: dict[int, list[str]] = {}
    for nome, chave, fim in blocos:
        job = jobs.get(nome)
        if not (isinstance(job, dict) and "steps" in job):
            continue
        itens = _itens(linhas, chave + 1, fim)
        passos = job.get("steps") or []
        if itens is None or len(itens[1]) != len(passos):
            return None
        recuo_item, posicoes = itens
        for k, (pos, passo) in enumerate(zip(posicoes, passos, strict=True)):
            antes_de.setdefault(pos, []).extend(_portoes_do_passo(recuo_item, k == 0, passo, repo))
    return antes_de


def _portoes_do_passo(recuo: int, primeiro: bool, passo: dict, repo: Path) -> list[str]:
    """O primeiro passo pega um lugar de leve; um passo pesado, com o mesmo
    `if:` dele, espera o job ficar sozinho."""
    linhas = portao(recuo, "leve", None) if primeiro else []
    condicao = passo.get("if") if isinstance(passo, dict) else None
    if condicao is not False and passo_pesado(passo, repo):
        linhas += portao(recuo, "pesado", condicao)
    return linhas


def _sem_os_portoes(workflow: dict) -> dict:
    """O workflow lido sem os passos que o `cercar` pos."""
    for job in (workflow.get("jobs") or {}).values():
        if isinstance(job, dict) and isinstance(job.get("steps"), list):
            job["steps"] = [
                p for p in job["steps"] if not str(p.get("name", "")).startswith(NOME_VEZ)
            ]
    return workflow


def _itens(linhas: list[str], de: int, ate: int) -> tuple[int, list[int]] | None:
    """O recuo dos itens de `steps:` e a linha onde cada um comeca."""
    filhos = [n for n in range(de, ate) if _conta(linhas[n])]
    if not filhos:
        return None
    recuo_filho = _recuo(linhas[filhos[0]])
    chave = next(
        (
            n
            for n in filhos
            if _recuo(linhas[n]) == recuo_filho and re.match(r"^\s*steps:\s*(#.*)?$", linhas[n])
        ),
        None,
    )
    if chave is None:
        return None
    seguintes = [n for n in filhos if n > chave]
    if not seguintes or not linhas[seguintes[0]].lstrip().startswith("-"):
        return None
    recuo_item = _recuo(linhas[seguintes[0]])
    posicoes = []
    for n in seguintes:
        r, corpo = _recuo(linhas[n]), linhas[n].lstrip()
        if r < recuo_item or (r == recuo_item and not corpo.startswith("-")):
            break
        if r == recuo_item and (corpo.startswith("- ") or corpo == "-"):
            posicoes.append(n)
    return recuo_item, posicoes


@contextlib.contextmanager
def estado_da_vez(pasta: Path):
    with (pasta / "trava").open("a+") as trava:
        fcntl.flock(trava, fcntl.LOCK_EX)
        arquivo = pasta / ESTADO_DA_VEZ
        estado = json.loads(arquivo.read_text())
        yield estado
        temporario = arquivo.with_suffix(".tmp")
        temporario.write_text(json.dumps(estado))
        temporario.replace(arquivo)


def conceder(estado: dict) -> None:
    """Fila na ordem de chegada: um leve ocupa 1 lugar, um pesado ocupa todos.
    Um leve que chega depois de um pesado espera atras dele (o pesado nao fica
    para sempre esperando uma brecha)."""
    lugares = estado["lugares"]
    custo = {"leve": 1, "pesado": lugares}
    usados = sum(custo[p] for p in estado["ativos"].values())
    while estado["fila"]:
        token, peso = estado["fila"][0]
        if usados + custo[peso] > lugares:
            break
        estado["fila"].pop(0)
        estado["ativos"][token] = peso
        usados += custo[peso]


def preparar_vez(lugares: int) -> None:
    PASTA_VEZ.mkdir(parents=True, exist_ok=True)
    with (PASTA_VEZ / "trava").open("a+") as trava:
        fcntl.flock(trava, fcntl.LOCK_EX)
        (PASTA_VEZ / ESTADO_DA_VEZ).write_text(
            json.dumps({"lugares": lugares, "pid": os.getpid(), "fila": [], "ativos": {}})
        )


def liberar_vez(tokens: list[str]) -> None:
    if not tokens or not (PASTA_VEZ / ESTADO_DA_VEZ).exists():
        return
    with estado_da_vez(PASTA_VEZ) as estado:
        for token in tokens:
            estado["ativos"].pop(token, None)
        estado["fila"] = [f for f in estado["fila"] if f[0] not in tokens]
        conceder(estado)


def vez_no_passo(pasta: Path, peso: str) -> None:
    """O passo posto pelo `cercar`: espera a vez e sai 0. Nunca reprova o job —
    se algo daqui falhar, ele segue sem esperar e diz por que."""
    try:
        if not (pasta / ESTADO_DA_VEZ).exists():
            diz("validar-local: fora de uma rodada; segue sem esperar")
            return
        token = os.environ.get("VALIDAR_LOCAL_VEZ", "")
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            token = os.urandom(16).hex()
            if os.environ.get("GITHUB_ENV"):
                with open(os.environ["GITHUB_ENV"], "a") as env:
                    env.write(f"VALIDAR_LOCAL_VEZ={token}\n")
        # Esta linha e o que liga o token ao job no log: quando o act disser
        # que o job acabou, o validar-local devolve a vez dele.
        diz(f"validar-local: vez {token} {peso}")
        inicio = time.monotonic()
        with estado_da_vez(pasta) as estado:
            atual = estado["ativos"].get(token)
            if atual == "pesado" or atual == peso:
                return
            # Um leve que vira pesado devolve o lugar e entra no fim da fila:
            # quem espera nunca segura lugar, e por isso ninguem trava.
            estado["ativos"].pop(token, None)
            estado["fila"] = [f for f in estado["fila"] if f[0] != token] + [[token, peso]]
            conceder(estado)
            dono = estado["pid"]
        while True:
            with estado_da_vez(pasta) as estado:
                if estado["ativos"].get(token) == peso:
                    break
            try:
                os.kill(dono, 0)
            except OSError:
                diz("validar-local: a rodada acabou; segue sem esperar")
                return
            time.sleep(1)
        espera = time.monotonic() - inicio
        if espera >= 1:
            diz(f"validar-local: esperou a vez por {espera:.0f} s")
    except Exception as erro:  # noqa: BLE001 - a vez nunca reprova um job
        diz(f"validar-local: a vez falhou ({type(erro).__name__}); segue sem esperar")


def sem_publicar(raiz: Path, n: int = 1, repo: Path | None = None) -> tuple[int, bool]:
    """Nada e publicado daqui. O job `imagem` da esteira publica a tag
    `sha-<commit>` no GHCR TAMBEM no PR, de proposito (ver o README); numa
    validacao local isso seria efeito colateral. Todo `push: true` dos
    workflows vira `push: false` na COPIA da esteira: a imagem e construida e
    varrida do mesmo jeito, e nao sai da maquina.

    Com `n` > 1, cada workflow da copia ganha a vez (`cercar`); o segundo
    valor diz se todos a ganharam."""
    trocas_feitas, cercados = 0, True
    base = raiz.resolve()
    for arquivo in (raiz / DIR_GITHUB).rglob(GLOB_YAML):
        # So o que resolve para DENTRO da copia: um link simbolico na esteira
        # que apontasse para fora faria esta reescrita mexer onde nao deve.
        if arquivo.is_symlink() or not arquivo.resolve().is_relative_to(base):
            continue
        original = arquivo.read_text()
        texto = max_paralelo(original, n) if "strategy:" in original else original
        texto, trocas = re.subn(
            r"(?m)^(\s*)push: true\s*$", r"\1push: false  # validar-local: nada e publicado", texto
        )
        trocas_feitas += trocas
        if n > 1 and repo is not None and arquivo.parent.name == "workflows":
            cercado = cercar(texto, repo)
            cercados = cercados and cercado is not None
            texto = cercado if cercado is not None else texto
        if texto != original:
            # O caminho sai do rglob de uma pasta FIXA do cache e passou pela
            # contencao acima; o conteudo e o do proprio arquivo, reescrito no
            # mesmo lugar. Nao ha entrada de fora aqui (falso positivo do S2083).
            arquivo.write_text(texto)  # NOSONAR
    return trocas_feitas, cercados


def esteira(local: bool, n: int = 1, repo: Path | None = None) -> tuple[Path, bool]:
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
    _, cercados = sem_publicar(destino, n, repo)
    return destino, cercados


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


def derivar(arquivo: Path, destino: Path, n: int, repo: Path) -> tuple[Path, bool, int]:
    """Copia o workflow com o Sonar desligado, quando ele chama a esteira, e
    com a vez em cada job. Devolve tambem quantos jobs podem rodar juntos
    nele: 1 quando algum job nao pode ser cercado e ha pesado no workflow."""
    texto = arquivo.read_text()
    novo, trocas_sonar = sem_sonar(texto)
    if n > 1:
        cercado = cercar(max_paralelo(novo, n), repo)
        motivo = None if cercado is not None else "o texto dele nao deu para cercar com certeza"
        motivo = motivo or reutilizavel_local_pesado(arquivo, repo)
        if motivo:
            diz(f"  {AMARELO}{arquivo.name} roda um job por vez{FIM}: {motivo}")
            n = 1
        else:
            novo = cercado
    if n == 1:
        novo = max_paralelo(novo, 1)
    saida = dentro_de(destino, arquivo.name)
    # Gravado por um arquivo ja aberto, e nao por `Path.write_text`: o S2083
    # trata o argumento do `write_text` como caminho e marcava o CONTEUDO do
    # workflow (lido do repositorio) como se fosse ele. O caminho e o de
    # `dentro_de`, conferido contra a pasta da copia.
    with saida.open("w", encoding="utf-8") as copia:
        copia.write(novo)
    return saida, trocas_sonar > 0, n


NOME_DE_WORKFLOW = re.compile(r"[A-Za-z0-9._-]+")


def dentro_de(pasta: Path, nome: str) -> Path:
    """`pasta/nome`, so quando o nome e o de um workflow e o caminho fica
    DENTRO da pasta: o nome vem do repositorio, e a copia nao grava fora."""
    if (
        not nome.endswith((".yml", ".yaml"))
        or nome.startswith(".")
        or not NOME_DE_WORKFLOW.fullmatch(nome)
    ):
        raise ValueError(f"nome de workflow recusado: {nome!r}")
    # realpath + commonpath: a validacao que o Sonar reconhece (S2083).
    base = os.path.realpath(pasta)
    saida = os.path.realpath(os.path.join(base, nome))
    if os.path.commonpath([base, saida]) != base or os.path.dirname(saida) != base:
        raise ValueError(f"{nome!r} sairia de {base}")
    return Path(saida)


# O portao da esteira reprova o Sonar desligado sem motivo escrito. Aqui o
# motivo e este: a analise e do CI, e o validar-local nao tem o token nem a rede
# do SonarQube. So na COPIA, e so onde o `sonar_projeto` foi esvaziado aqui.
MOTIVO_SEM_SONAR = "Sonar so no CI: validar-local nao tem token nem rede para o SonarQube"


def sem_sonar(texto: str) -> tuple[str, int]:
    """`sonar_projeto` vazio e `sonar_dispensa` com o motivo, no mesmo bloco
    `with:`. Um `sonar_projeto` que ja era vazio fica como estava, com a
    dispensa que o repositorio escreveu."""
    linhas = texto.split("\n")
    saida: list[str] = []
    trocas = 0
    for linha in linhas:
        # O comentario sai antes do match (o valor acaba no primeiro `#`, como
        # antes): a regex fica sem quantificadores sobrepostos (Sonar S8786).
        achado = re.match(r"(\s*)sonar_projeto:(.*)", linha.split("#", 1)[0])
        if not achado or achado.group(2).strip() in ("", '""', "''"):
            saida.append(linha)
            continue
        recuo = achado.group(1)
        saida.append(f'{recuo}sonar_projeto: ""')
        saida.append(f'{recuo}sonar_dispensa: "{MOTIVO_SEM_SONAR}"  # validar-local')
        trocas += 1
    if trocas:
        # A dispensa que ja existisse no mesmo bloco viraria chave duplicada.
        texto_novo = "\n".join(saida)
        texto_novo = re.sub(
            r'(?m)^(\s*)sonar_dispensa:(?!\s*"' + re.escape(MOTIVO_SEM_SONAR) + r'").*\n?',
            "",
            texto_novo,
        )
        return texto_novo, trocas
    return texto, 0


def reutilizavel_local_pesado(arquivo: Path, repo: Path) -> str | None:
    """Um `uses: ./.github/workflows/x.yml` roda da copia do repositorio, que o
    validar-local nao reescreve (os jobs a testam como ela e): os jobs de la
    ficam sem a vez. Sem a vez, nada impede um deles de rodar junto de um
    pesado; entao, se houver pesado em qualquer canto do workflow — nos jobs
    dele, nos chamados de la, ou na esteira, que tem —, ele roda um por vez."""
    yq = ferramenta("yq")
    jobs = [j for j in (ler(yq, arquivo).get("jobs") or {}).values() if isinstance(j, dict)]
    locais, pesado = [], any(job_pesado(j, repo) for j in jobs)
    for job in jobs:
        uses = str(job.get("uses") or "")
        pesado = pesado or uses.startswith(f"{REPO_ESTEIRA}/.github/workflows/")
        alvo = (repo / uses).resolve() if uses.startswith("./") else None
        if alvo is None or not alvo.is_relative_to(repo.resolve()) or not alvo.is_file():
            continue
        locais.append(uses)
        chamados = (ler(yq, alvo).get("jobs") or {}).values()
        pesado = pesado or any(isinstance(j, dict) and job_pesado(j, repo) for j in chamados)
    if locais and pesado:
        return f"chama {', '.join(locais)}, que fica sem a vez, e o workflow tem job pesado"
    return None


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


def privado(dono_repo: str) -> bool:
    """A visibilidade do repositorio, como o GitHub a poe no evento.

    A esteira decide por `github.event.repository.private` onde vai o Sonar e
    se o SARIF (e com ele o CodeQL) tem onde ser publicado. Sem o campo, o
    evento daqui contava como privado, e todo repositorio PUBLICO com
    `linguagens_codeql` reprovava aqui o que passa no CI (comporta,
    2026-10-29). Na duvida — sem rede, sem login — continua privado: e o lado
    que a esteira tambem escolhe quando o campo falta.
    """
    if not re.fullmatch(r"[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*", dono_repo):
        return True
    saida = subprocess.run(
        ["gh", "api", f"repos/{dono_repo}", "--jq", ".private"], capture_output=True, text=True
    )
    return not (saida.returncode == 0 and saida.stdout.strip() == "false")


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
        "repository": {
            "full_name": dono_repo,
            "name": curto,
            "owner": {"login": dono},
            "private": privado(dono_repo),
        },
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


def _ambiente_do_act(home: Path, segredos_val: dict[str, str]) -> dict[str, str]:
    return {
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


def _comando_do_act(
    act: Path,
    workflow: Path,
    evento_json: Path,
    mapa: list[str],
    segredos_val: dict[str, str],
    job: str | None,
    n: int,
) -> list[str]:
    comando = [
        str(act),
        "pull_request",
        "-W",
        str(workflow),
        "-e",
        str(evento_json),
        *[f"-P={rotulo}=-self-hosted" for rotulo in ROTULOS],
        "--concurrent-jobs",
        str(n),
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
    return comando


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LINHA_DA_VEZ = re.compile(r"\| validar-local: vez ([0-9a-f]{32}) ")


class LogDoAct:
    """O que o log do act diz de cada job, linha a linha: o resultado, o log
    proprio de cada um (com jobs juntos, as linhas deles se intercalam no log
    do workflow) e a vez que o job devolve quando acaba."""

    def __init__(self, pasta_jobs: Path, abertos: contextlib.ExitStack) -> None:
        self.jobs: dict[str, str] = {}
        self.vezes: dict[str, list[str]] = {}
        self.por_job: dict[str, typing.TextIO] = {}
        self.pasta_jobs = pasta_jobs
        self.abertos = abertos

    def ler(self, linha: str) -> None:
        limpa = ANSI.sub("", linha).rstrip()
        if "Could not get lock /var/lib/apt" in limpa:
            self.jobs["(apt ocupado)"] = "ambiente"
        achado = linha_de_job(limpa)
        if not achado:
            return
        nome, resto = achado
        self._log_do_job(nome).write(linha)
        self._anotar_vez(nome, resto)
        self._anotar_resultado(nome, resto)

    def _log_do_job(self, nome: str) -> typing.TextIO:
        if nome not in self.por_job:
            arquivo = self.pasta_jobs / f"{NAO_ARQUIVO.sub('_', nome).strip('_')[:120]}.log"
            self.por_job[nome] = self.abertos.enter_context(arquivo.open("a", buffering=1))
        return self.por_job[nome]

    def _anotar_vez(self, nome: str, resto: str) -> None:
        vez = LINHA_DA_VEZ.match(resto + " ")
        if vez:
            self.vezes.setdefault(nome, []).append(vez.group(1))
        if "🏁  Job" in resto:
            # O job acabou (passos, `post` e limpeza): a vez dele volta.
            liberar_vez(self.vezes.pop(nome, []))

    def _anotar_resultado(self, nome: str, resto: str) -> None:
        if "🚀  Start image" in resto or ("⭐ Run Set up job" in resto and nome not in self.jobs):
            self.jobs.setdefault(nome, "rodando")
            diz(f"  {FRACO}…{FIM} {nome}")
        elif "❌  Failure - Main" in resto:
            diz(f"    {VERMELHO}falhou em {nome}:{FIM} {resto.split('Main', 1)[1].strip()}")
        elif "🏁  Job succeeded" in resto:
            self.jobs[nome] = "passou"
            diz(f"  {VERDE}passou{FIM} {nome}")
        elif "🏁  Job failed" in resto:
            self.jobs[nome] = "reprovou"
            diz(f"  {VERMELHO}reprovou{FIM} {nome}")


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
    n: int = 1,
) -> dict[str, str]:
    comando = _comando_do_act(act, workflow, evento_json, mapa, segredos_val, job, n)
    esperar_apt()
    preparar_vez(n)
    # Cada job tem tambem o seu log, inteiro, em <log sem .log>/<job>.log.
    pasta_jobs = log.with_suffix("")
    pasta_jobs.mkdir(exist_ok=True)
    with log.open("w", buffering=1) as saida, contextlib.ExitStack() as abertos:
        processo = subprocess.Popen(
            comando,
            cwd=repo,
            env=_ambiente_do_act(home, segredos_val),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert processo.stdout is not None
        leitor = LogDoAct(pasta_jobs, abertos)
        for linha in processo.stdout:
            saida.write(linha)
            leitor.ler(linha)
        codigo = processo.wait()
    jobs = leitor.jobs
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
    # O passo que o `cercar` poe em cada job chama este script assim; nao e
    # para uso direto.
    ap.add_argument("--vez", choices=("leve", "pesado"), help=argparse.SUPPRESS)
    ap.add_argument("--vez-pasta", type=Path, help=argparse.SUPPRESS)
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


def cabecalho(repo: Path, escolhidos: list[Path], args: argparse.Namespace, n: int) -> None:
    diz(f"validar-local em {repo.name} ({git('branch', '--show-current', cwd=repo) or 'HEAD'})")
    diz(
        f"  workflows: {', '.join(w.name for w in escolhidos)}"
        + (f"  job: {args.job}" if args.job else "")
    )
    diz(f"  esteira:   {'esta maquina, como esta' if args.esteira_local else REF_MAIN}")
    diz(
        f"  jobs:      {n} leve(s) por vez, pesado sozinho (VALIDAR_LOCAL_JOBS)"
        if n > 1
        else "  jobs:      um por vez (VALIDAR_LOCAL_JOBS=1)"
    )
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
    n: int,
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
            log = logs / f"{repo.name}-{carimbo}-{workflow.stem}.log"
            diz(f"\n{workflow.name} — log em {log}, e o de cada job em {log.with_suffix('')}/")
            derivado, sem_sonar, n_wf = derivar(workflow, tmp_path, n, repo)
            if sem_sonar:
                diz(f"  {FRACO}Sonar desligado{FIM}")
            resultado[workflow.name] = rodar_act(
                act, copia, derivado, args.job, home, mapa, ev, valores, log, n_wf
            )
    return resultado


def travar_a_maquina() -> typing.TextIO | None:
    """A trava de um validar-local por vez nesta maquina (os dois disputariam
    Docker e portas). Fica presa enquanto o arquivo devolvido estiver aberto."""
    CACHE.mkdir(parents=True, exist_ok=True)
    trava = (CACHE / "trava").open("w")
    try:
        fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        trava.close()
        return None
    return trava


def esteira_fora_da_main(escolhidos: list[Path]) -> str | None:
    refs = refs_da_esteira(escolhidos) - {"main"}
    if not refs:
        return None
    return (
        f"a esteira e chamada em {', '.join(sorted(refs))}, e nao em @main: as actions que "
        "ela traz nesse ref nao estao mapeadas, e o harden-runner rodaria nesta maquina"
    )


def preparar_esteira(args: argparse.Namespace, repo: Path) -> tuple[Path, int] | str:
    """A copia da esteira e quantos jobs rodam juntos. Se um workflow dela nao
    der para cercar, a copia e refeita para rodar um job por vez."""
    n = paralelos()
    if isinstance(n, str):
        return n
    git("fetch", "--quiet", "origin", "main", cwd=repo, verificar=False)
    raiz_esteira, cercada = esteira(args.esteira_local, n, repo)
    if not cercada:
        diz(f"{AMARELO}um workflow da esteira nao deu para cercar{FIM}: um job por vez")
        n = 1
        raiz_esteira, _ = esteira(args.esteira_local, n, repo)
    return raiz_esteira, n


def arvore_intocada(repo: Path, antes: str) -> bool:
    """O act roda numa copia do repositorio, e o que ele fizer nao pode aparecer
    aqui. Se aparecer, e defeito deste script ou de um passo, e a arvore que vai
    para o PR nao e mais a que foi validada."""
    depois = git("status", "--porcelain", cwd=repo)
    if depois == antes:
        return True
    novos = sorted(set(depois.splitlines()) - set(antes.splitlines()))
    diz(f"{VERMELHO}a rodada mexeu na arvore{FIM}: {', '.join(novos) or depois}")
    return False


def main() -> int:
    args = argumentos()
    if args.vez:
        vez_no_passo(args.vez_pasta or PASTA_VEZ, args.vez)
        return 0
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
    return validar(args, repo, arvore, antes)


def validar(args: argparse.Namespace, repo: Path, arvore: str, antes: str) -> int:
    """A rodada: escolhe os workflows, prepara a esteira e roda cada um no act."""
    pasta_wf = repo / DIR_GITHUB / "workflows"
    todos = sorted([*pasta_wf.glob("*.yml"), *pasta_wf.glob("*.yaml")])
    if not todos:
        return falha(f"{repo.name} nao tem .github/workflows")
    problema = maquina_pronta()
    if problema:
        return falha(problema)

    trava = travar_a_maquina()
    if trava is None:
        return falha("outro validar-local esta rodando (os dois disputariam Docker e portas)")
    liberados = limpar_rodadas_do_act()
    if liberados:
        diz(f"{FRACO}{liberados} MB de rodadas anteriores do act apagados{FIM}")

    escolhidos = escolher(args, pasta_wf, todos)
    if isinstance(escolhidos, str):
        return falha(escolhidos)
    problema = esteira_fora_da_main(escolhidos)
    if problema:
        return falha(problema)

    preparada = preparar_esteira(args, repo)
    if isinstance(preparada, str):
        return falha(preparada)
    raiz_esteira, n = preparada
    # Quem chama a esteira repassa cada segredo pelo nome (`secrets:`), entao os
    # pedidos sao os dos workflows escolhidos.
    valores, faltam = segredos(repo, escolhidos)
    cabecalho(repo, escolhidos, args, n)
    for nome, onde in sorted(faltam.items()):
        diz(f"  {AMARELO}sem valor local{FIM}: {nome} (pedido em {', '.join(onde)})")
    if args.listar:
        return 0

    inicio = time.monotonic()
    ok = lint(ferramenta("actionlint"), repo)
    try:
        resultado = rodar_workflows(repo, escolhidos, args, raiz_esteira, valores, n)
    except ValueError as erro:
        return falha(str(erro))
    minutos = (time.monotonic() - inicio) / 60
    ok = arvore_intocada(repo, antes) and ok
    ok = resumir(resultado, minutos) and ok
    if not ok:
        diz(f"\n{VERMELHO}REPROVADO{FIM}: conserte aqui antes de empurrar.")
        return 1
    return concluir(repo, arvore, args, bool(antes), ", ".join(resultado), minutos)


if __name__ == "__main__":
    sys.exit(main())

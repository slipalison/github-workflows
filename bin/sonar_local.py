#!/usr/bin/env python3
"""Sonar local: os problemas NOVOS do PR, medidos aqui, antes do push.

POR QUE EXISTE
--------------
O portao do SonarQube dos repositorios do dono exige zero problemas novos
(`new_violations = 0`). O `validar-local` roda os workflows do PR no act, mas a
analise do Sonar e do servidor, e ficava para o CI: PR verde aqui caia no Sonar
(S1135 num comentario com a palavra "todo", S3776, S3358, S2301, S7721), e cada
conserto custava outro run inteiro (~52 min no basalto).

COMO
----
Esta caixa tem dois containers do servidor MCP da Sonar, e a ferramenta
`analyze_code_snippet` roda o analisador da Sonar com o PERFIL DE QUALIDADE DO
PROJETO quando recebe o `projectKey`: as mesmas regras do portao. Por arquivo
que o PR muda, este script analisa a versao do merge-base com `origin/main` e a
atual, e chama de NOVO o problema que a base nao tinha.

    127.0.0.1:8091  SonarQube do homelab, repositorios PRIVADOS
    127.0.0.1:8090  SonarCloud, repositorios PUBLICOS

Projeto, servidor e arquivos saem de onde a esteira decide, e nao de regra
nova: o workflow chamador (`sonar_projeto`, `sonar_host`, `sonar_parametros` do
pipeline.yml; `projeto`, `host`, `parametros` do sonar.yml) ou, na falta dele, o
`sonar-project.properties`. Com `sonar_host` explicito, a Cloud vai para o 8090
e uma faixa privada para o 8091 (a regra do sonar.yml); sem ele, quem decide e a
visibilidade do repositorio, como no pipeline.yml. `sonar.sources`,
`sonar.exclusions`, `sonar.tests`, `sonar.test.inclusions` e
`sonar.test.exclusions` dizem que arquivo entra e se e TEST ou MAIN.

"NOVO" COMO O SONAR RASTREIA
----------------------------
Nao e "linha alterada": um problema e novo quando a combinacao (regra,
mensagem, texto da linha de inicio sem espacos sobrando) nao existe na base.
Assim a linha que so se deslocou nao vira falso novo, e a complexidade de uma
funcao cujo cabecalho nao mudou, mas que passou de 15, aparece. Dois detalhes:
um problema igual a outro que a base tinha so conta como existente uma vez (o
segundo e novo), e os NUMEROS da mensagem sao descartados na comparacao (a
funcao que ja estava acima de 15 e foi a 18 e o mesmo problema, como no Sonar).

O QUE ELE NAO MEDE (e o CI mede)
--------------------------------
Linguagem sem analisador no MCP (Rust, por exemplo): entra na contagem "so o CI
mede", nunca em silencio. A cobertura do codigo novo e a duplicacao tambem sao
so do CI. E a analise do snippet nao enxerga outros arquivos: regra que
depende do projeto inteiro fica para o CI.

SAIDA E CODIGOS
---------------
`arquivo:linha regra mensagem` por problema novo e um resumo. Codigo 0 limpo (ou
sem projeto Sonar), 1 com problema novo, 2 quando nao deu para medir (MCP fora,
helper sem token, projeto ilegivel).

SEGREDO
-------
O token vem do helper de headers desta caixa (`~/.local/bin/sonarqube-*-mcp-
headers`), le-se em memoria e so vai ao MCP em 127.0.0.1. Nunca aparece em
stdout, stderr, argumento de processo ou arquivo: tudo o que se imprime passa
por `limpar`, um erro HTTP e relatado so pelo codigo, e o redirecionamento nao
e seguido (o urllib reenviaria o cabecalho ao destino).

CALIBRAGEM
----------
O `analyze_code_snippet` analisa o arquivo SOZINHO, sem tsconfig nem tipos, e as
excecoes do SonarJS que dependem de tipo deixam de valer: ele acusa `void
promessa()` (S3735) e `x?: string | undefined` (S4782) que o servidor, que ve o
projeto, nao acusa. Ligado assim, o portao reprovaria codigo certo e empurraria
o agente a "consertar" soltando a promessa. `--calibrar` mede isso: analisa com o
MCP todo arquivo das `sonar.sources` na main (base vazia, e nao diff) e compara
com o que o PROPRIO servidor tem aberto nela (OPEN e CONFIRMED, paginado). Regra
que o MCP acusa e o servidor nao tem em lugar nenhum e "nao espelhada aqui"; a
que bate com o servidor (mesmo arquivo e linha) e "confiavel". O JSON
(`nao_espelhadas` com a contagem e um exemplo, a versao do MCP e a data) vai
versionado na raiz do repositorio medido como `.sonar-local.json`, e o
sonar_local.py o le: regra de `nao_espelhadas` nao reprova (nem no hook, para o
agente nao reescrever codigo certo) e sai numa linha "aviso, o CI decide".
Sem o arquivo, tudo reprova como antes.

--hook
------
Recebe na stdin o JSON do hook `PostToolUse` do Claude Code
(`tool_input.file_path`) e analisa so aquele arquivo. Com problema novo, a lista
vai para o stderr e o codigo e 2: o agente que editou a recebe de volta. Sem
problema, com arquivo fora do Sonar ou sem condicao de medir, sai 0 em silencio:
editar nunca trava por causa do MCP. Teto de 25 s.

USO
    sonar_local.py                 o PR inteiro (merge-base com origin/main)
    sonar_local.py --repo DIR      outro repositorio
    sonar_local.py --hook          um arquivo, pela stdin do hook
    sonar_local.py --calibrar [--somente src/] > .sonar-local.json   gera o JSON
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REF_MAIN = "origin/main"
PRAZO_HOOK_S = 25.0
PRAZO_CHAMADA_S = 120.0
PARALELO = 3
CALIBRACAO = ".sonar-local.json"
ORIGEM_CENTRAL = "central"
ESTEIRA = Path(__file__).resolve().parent.parent
PAGINA_ISSUES = 500
STATUS_ABERTOS = ["OPEN", "CONFIRMED"]
LIMITE_ARQUIVO = 1_000_000
ENTRADAS_DO_CI = ("pipeline", "sonar")
REGRA_CLOUD = re.compile(r"sonarcloud\.io|sonarqube\.com")
# A faixa privada que o sonar.yml entende como "so a tailnet alcanca".
REGRA_PRIVADA = re.compile(
    r"^https?://(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)"
)
CHAMADOR = re.compile(r"github-workflows/\.github/workflows/(pipeline|sonar)\.ya?ml")

LOOPBACK = ("127.0.0.1", "localhost", "::1")
BIN = Path.home() / ".local" / "bin"
SERVIDORES = {
    "homelab": (
        "http://127.0.0.1:8091/mcp",
        BIN / "sonarqube-homelab-mcp-headers",
        "docker start sonarqube-homelab-mcp  (o container do SonarQube do homelab, porta 8091)",
    ),
    "cloud": (
        "http://127.0.0.1:8090/mcp",
        BIN / "sonarqube-mcp-headers",
        f"{BIN / 'sonarqube-mcp-up'}  (o container do SonarCloud, porta 8090)",
    ),
}

# Extensao -> `language` da ferramenta analyze_code_snippet.
LINGUAGENS = {
    ".ts": "ts",
    ".mts": "ts",
    ".cts": "ts",
    ".tsx": "tsx",
    ".js": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".jsx": "jsx",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".html": "html",
    ".htm": "html",
    ".py": "py",
    ".java": "java",
    ".go": "go",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".tf": "terraform",
    ".xml": "xml",
    ".php": "php",
    ".rb": "ruby",
    ".vue": "vue",
}
# O que o Sonar do CI analisa e o MCP nao: conta em "so o CI mede".
# Medido em si mesmo (github-workflows#35): o MCP achou os 10 problemas comuns do
# Sonar e nenhum dos 2 `pythonsecurity:*`, que sao analise de fluxo do servidor.
NOTA_PYTHON = "em Python, a analise de fluxo de seguranca (pythonsecurity)"
C_E_CPP = "C/C++"
SO_O_CI = {
    ".rs": "Rust",
    ".cs": "C#",
    ".swift": "Swift",
    ".scala": "Scala",
    ".c": C_E_CPP,
    ".cpp": C_E_CPP,
    ".h": C_E_CPP,
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
}


class NaoMediu(Exception):
    """Nao deu para medir. A mensagem e escrita para ser impressa."""


# Os valores secretos que o helper devolveu. `limpar` os tira de qualquer texto
# antes de ele sair; e a ultima barreira, depois de nao os colocar em lugar nenhum.
_SEGREDOS: set[str] = set()


def limpar(texto: str) -> str:
    for segredo in _SEGREDOS:
        texto = texto.replace(segredo, "***")
    return texto


def diz(texto: str = "", destino=None) -> None:
    print(limpar(texto), file=destino or sys.stdout, flush=True)


# ------------------------------------------------------------------- prazo


class Prazo:
    def __init__(self, segundos: float | None) -> None:
        self.fim = None if segundos is None else time.monotonic() + segundos

    def para_chamada(self) -> float:
        if self.fim is None:
            return PRAZO_CHAMADA_S
        restante = self.fim - time.monotonic()
        if restante <= 0.5:
            raise NaoMediu("o prazo acabou")
        return min(PRAZO_CHAMADA_S, restante)


# --------------------------------------------------------------------- git


def git(repo: Path, *args: str) -> str:
    saida = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if saida.returncode != 0:
        raise NaoMediu(f"git {args[0]} falhou: {saida.stderr.strip()[:200]}")
    return saida.stdout


def texto_da_base(repo: Path, base: str, caminho: str) -> str | None:
    saida = subprocess.run(["git", "show", f"{base}:{caminho}"], cwd=repo, capture_output=True)
    return saida.stdout.decode("utf-8", "replace") if saida.returncode == 0 else None


def merge_base(repo: Path, ref: str | None) -> str:
    for candidata in (ref or REF_MAIN, "main"):
        saida = subprocess.run(
            ["git", "merge-base", candidata, "HEAD"], cwd=repo, capture_output=True, text=True
        )
        if saida.returncode == 0 and saida.stdout.strip():
            return saida.stdout.strip()
    raise NaoMediu("sem merge-base com origin/main (rode `git fetch origin main`)")


def arquivos_mudados(repo: Path, base: str) -> list[tuple[str, str | None]]:
    """(caminho atual, caminho na base) dos arquivos do PR: commitados, na
    arvore de trabalho e nao rastreados. O caminho da base e None se o arquivo
    e novo; o de um renomeado e o antigo."""
    partes = git(repo, "diff", "--name-status", "-z", "-M", base).split("\0")
    achados: dict[str, str | None] = {}
    i = 0
    while i < len(partes) and partes[i]:
        status = partes[i]
        if status[0] in "RC" and i + 2 < len(partes):
            achados[partes[i + 2]] = partes[i + 1]
            i += 3
            continue
        if status[0] != "D":
            achados[partes[i + 1]] = None if status[0] == "A" else partes[i + 1]
        i += 2
    novos = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    for caminho in filter(None, novos):
        achados[caminho] = None
    return sorted(achados.items())


# ---------------------------------------------------------- projeto Sonar


@dataclass
class Projeto:
    chave: str
    host: str = ""
    parametros: dict[str, str] = field(default_factory=dict)
    dispensa: str = ""  # o `sonar_dispensa` de quem chama, quando a chave e vazia


def _validar():
    """O validar_local.py ao lado: `privado` e `ferramenta` (o yq com sha fixo)."""
    pasta = str(Path(__file__).resolve().parent)
    if pasta not in sys.path:
        sys.path.insert(0, pasta)
    import validar_local

    return validar_local


def ler_propriedades(texto: str) -> dict[str, str]:
    saida: dict[str, str] = {}
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha or linha[0] in "#!" or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        saida[chave.strip()] = valor.strip()
    return saida


def _ler_yaml(arquivo: Path) -> dict:
    try:
        yq = _validar().ferramenta("yq")
    except (OSError, RuntimeError):
        raise NaoMediu(
            "sem o yq para ler os workflows (cache do validar-local, sem rede)"
        ) from None
    saida = subprocess.run(
        [str(yq), "-o=json", ".", str(arquivo)], capture_output=True, text=True, timeout=30
    )
    if saida.returncode != 0:
        raise NaoMediu(f"o yq nao leu {arquivo.name}")
    return json.loads(saida.stdout) or {}


def _entradas(job_with: dict, nome: str) -> Projeto:
    pipeline = nome == "pipeline"
    chave = str(job_with.get("sonar_projeto" if pipeline else "projeto") or "").strip()
    host = str(job_with.get("sonar_host" if pipeline else "host") or "").strip()
    parametros = str(job_with.get("sonar_parametros" if pipeline else "parametros") or "")
    if "${{" in host:
        host = ""
    dispensa = str(job_with.get("sonar_dispensa") or "").strip()
    return Projeto(chave, host, ler_propriedades(parametros), dispensa)


def chamadores(repo: Path) -> list[Projeto]:
    achados: list[Projeto] = []
    pasta = repo / ".github" / "workflows"
    for arquivo in sorted([*pasta.glob("*.yml"), *pasta.glob("*.yaml")]):
        if not CHAMADOR.search(arquivo.read_text(errors="replace")):
            continue
        documento = _ler_yaml(arquivo)
        if "workflow_call" in json.dumps(documento.get("on", documento.get("true"))):
            continue  # um reutilizavel da propria esteira repassa `inputs.*`: nao e um PR
        for job in (documento.get("jobs") or {}).values():
            usado = CHAMADOR.search(str(job.get("uses", "")))
            if usado:
                achados.append(_entradas(job.get("with") or {}, usado.group(1)))
    return achados


def propriedades_do_repo(repo: Path) -> dict[str, str]:
    arquivo = repo / "sonar-project.properties"
    return ler_propriedades(arquivo.read_text(errors="replace")) if arquivo.is_file() else {}


def descobrir(repo: Path) -> Projeto | None:
    """O projeto Sonar do repositorio. Chave vazia: o workflow o dispensou. None: nao
    ha chamador da esteira nem sonar-project.properties."""
    propriedades = propriedades_do_repo(repo)
    achados = chamadores(repo)
    if achados:
        projeto = next((p for p in achados if p.chave), None)
        if projeto is None:
            return Projeto("", dispensa=next((p.dispensa for p in achados if p.dispensa), ""))
        projeto.parametros = {**propriedades, **projeto.parametros}
    elif propriedades.get("sonar.projectKey"):
        projeto = Projeto(propriedades["sonar.projectKey"], "", propriedades)
    else:
        return None
    if "${{" in projeto.chave:
        raise NaoMediu(
            f"a chave do projeto Sonar e uma expressao ({projeto.chave}): nao sei resolver"
        )
    return projeto


def dono_e_repo(repo: Path) -> str:
    nome = git(repo, "config", "--get", "remote.origin.url").strip()
    return re.sub(r"(\.git)?$", "", nome.split("github.com")[-1].lstrip(":/"))


def visibilidade_privada(dono_repo: str) -> bool:
    forcada = os.environ.get("SONAR_LOCAL_VISIBILIDADE")  # dos testes: privado | publico
    if forcada in ("privado", "publico"):
        return forcada == "privado"
    return _validar().privado(dono_repo)


def escolher_servidor(projeto: Projeto, repo: Path, privado=None) -> str:
    """homelab ou cloud, como o pipeline.yml: `sonar_host` explicito vence, e sem
    ele a visibilidade do repositorio (privado vai para o homelab)."""
    if projeto.host:
        if REGRA_CLOUD.search(projeto.host):
            return "cloud"
        if REGRA_PRIVADA.search(projeto.host):
            return "homelab"
        raise NaoMediu(
            f"o host Sonar {projeto.host} nao tem MCP nesta caixa (so o homelab e a Cloud)"
        )
    decide = privado or visibilidade_privada
    return "homelab" if decide(dono_e_repo(repo)) else "cloud"


# ------------------------------------------------------ arquivos do Sonar


@dataclass
class Padroes:
    fontes: list[str]
    testes: list[str]
    exclusoes: list[str]
    inclusoes_teste: list[str]
    exclusoes_teste: list[str]


def _lista(valor: str | None) -> list[str]:
    return [item.strip() for item in (valor or "").split(",") if item.strip()]


def padroes(parametros: dict[str, str]) -> Padroes:
    return Padroes(
        _lista(parametros.get("sonar.sources")) or ["."],
        _lista(parametros.get("sonar.tests")),
        _lista(parametros.get("sonar.exclusions")),
        _lista(parametros.get("sonar.test.inclusions")),
        _lista(parametros.get("sonar.test.exclusions")),
    )


def _ant(padrao: str) -> re.Pattern[str]:
    """Glob no estilo do Ant, o que o Sonar usa: `**` atravessa pastas, `*` e `?`
    ficam dentro de uma."""
    padrao = padrao.removeprefix("./")
    if padrao.endswith("/"):
        padrao += "**"
    regex, i = "", 0
    while i < len(padrao):
        if padrao.startswith("**/", i):
            regex, i = regex + "(?:.*/)?", i + 3
        elif padrao.startswith("**", i):
            regex, i = regex + ".*", i + 2
        elif padrao[i] == "*":
            regex, i = regex + "[^/]*", i + 1
        elif padrao[i] == "?":
            regex, i = regex + "[^/]", i + 1
        else:
            regex, i = regex + re.escape(padrao[i]), i + 1
    return re.compile(regex)


def _casa(caminho: str, lista: list[str]) -> bool:
    return any(_ant(padrao).fullmatch(caminho) for padrao in lista)


def _sob(caminho: str, pastas: list[str]) -> bool:
    for pasta in pastas:
        pasta = pasta.removeprefix("./").rstrip("/")
        if pasta in ("", ".") or caminho == pasta or caminho.startswith(pasta + "/"):
            return True
    return False


def classificar(caminho: str, p: Padroes) -> str | None:
    """MAIN, TEST, ou None se o Sonar nao olha o arquivo."""
    em_fontes, em_testes = _sob(caminho, p.fontes), _sob(caminho, p.testes)
    if p.inclusoes_teste:
        eh_teste = (em_fontes or em_testes) and _casa(caminho, p.inclusoes_teste)
    else:
        eh_teste = em_testes
    if eh_teste:
        return None if _casa(caminho, p.exclusoes_teste) else "TEST"
    if em_fontes and not _casa(caminho, p.exclusoes):
        return "MAIN"
    return None


def linguagem_de(caminho: str) -> str | None:
    nome = Path(caminho).name
    if nome == "Dockerfile" or nome.startswith("Dockerfile.") or nome.endswith(".dockerfile"):
        return "docker"
    return LINGUAGENS.get(Path(caminho).suffix.lower())


# ------------------------------------------------------------------- MCP


class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    """O urllib reenvia os cabecalhos (o token) ao destino de um 3xx. Aqui o 3xx
    vira erro."""

    def redirect_request(self, *args, **kwargs):
        return None


def ler_helper(helper: Path) -> dict[str, str]:
    try:
        saida = subprocess.run([str(helper)], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise NaoMediu(f"nao consegui rodar {helper.name}") from None
    if saida.returncode != 0:
        raise NaoMediu(
            f"{helper.name} saiu com {saida.returncode} (sem token em ~/.config/sonarqube?)"
        )
    try:
        cabecalhos = json.loads(saida.stdout)
        ok = isinstance(cabecalhos, dict) and all(
            isinstance(k, str) and isinstance(v, str) and "\n" not in k + v and "\r" not in k + v
            for k, v in cabecalhos.items()
        )
    except ValueError:
        ok = False
    if not ok:
        raise NaoMediu(f"{helper.name} nao devolveu um objeto JSON de cabecalhos")
    for valor in cabecalhos.values():
        _SEGREDOS.add(valor)
        _SEGREDOS.add(valor.removeprefix("Bearer ").strip())
    _SEGREDOS.discard("")
    return cabecalhos


def _corpo(texto: str, tipo: str, id_: int | None) -> dict:
    if not texto.strip():
        return {}
    if "event-stream" in tipo:
        mensagens = []
        for linha in texto.splitlines():
            if linha.startswith("data:"):
                try:
                    mensagens.append(json.loads(linha[5:].strip()))
                except ValueError:
                    continue
        certas = [m for m in mensagens if isinstance(m, dict) and m.get("id") == id_]
        return (certas or mensagens or [{}])[-1]
    try:
        return json.loads(texto)
    except ValueError:
        raise NaoMediu("o MCP respondeu algo que nao e JSON") from None


class ClienteMcp:
    def __init__(self, nome: str, prazo: Prazo) -> None:
        url, helper, self.como_subir = SERVIDORES[nome]
        self.url = os.environ.get("SONAR_LOCAL_URL") or url
        helper = Path(os.environ.get("SONAR_LOCAL_HELPER") or helper)
        if urllib.parse.urlparse(self.url).hostname not in LOOPBACK:
            raise NaoMediu("o MCP so e falado em 127.0.0.1: o token nao sai da caixa")
        self.prazo = prazo
        self.cabecalhos = {
            **ler_helper(helper),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.sessao: str | None = None
        self.abre = urllib.request.build_opener(_SemRedirecionar)
        self.proximo_id = 1
        self.iniciado = False
        self.versao = "desconhecida"

    def _enviar(self, mensagem: dict) -> dict:
        cabecalhos = dict(self.cabecalhos)
        if self.sessao:
            cabecalhos["Mcp-Session-Id"] = self.sessao
        pedido = urllib.request.Request(
            self.url, data=json.dumps(mensagem).encode(), headers=cabecalhos, method="POST"
        )
        try:
            with self.abre.open(pedido, timeout=self.prazo.para_chamada()) as resposta:
                texto = resposta.read().decode("utf-8", "replace")
                tipo = resposta.headers.get("Content-Type", "")
                self.sessao = resposta.headers.get("Mcp-Session-Id") or self.sessao
        except urllib.error.HTTPError as erro:
            # So o codigo: o objeto do erro guarda os cabecalhos do pedido.
            dica = " (token recusado)" if erro.code in (401, 403) else ""
            raise NaoMediu(f"o MCP respondeu HTTP {erro.code}{dica}") from None
        except OSError:
            raise NaoMediu(f"nao consegui falar com o MCP em {self.url}") from None
        corpo = _corpo(texto, tipo, mensagem.get("id"))
        if "error" in corpo:
            erro_rpc = corpo["error"] if isinstance(corpo["error"], dict) else {}
            raise NaoMediu(f"o MCP recusou: {str(erro_rpc.get('message', ''))[:200]}")
        return corpo

    def iniciar(self) -> None:
        resposta = self._enviar(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "sonar-local", "version": "1"},
                },
            }
        )
        informacao = (resposta.get("result") or {}).get("serverInfo") or {}
        self.versao = str(informacao.get("version") or "desconhecida")
        self._enviar({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.iniciado = True

    def chamar(self, ferramenta: str, argumentos: dict) -> dict:
        self.proximo_id += 1
        corpo = self._enviar(
            {
                "jsonrpc": "2.0",
                "id": self.proximo_id,
                "method": "tools/call",
                "params": {"name": ferramenta, "arguments": argumentos},
            }
        )
        resultado = corpo.get("result") or {}
        conteudos = resultado.get("content") or [{}]
        texto = str(conteudos[0].get("text", ""))
        if resultado.get("isError"):
            raise NaoMediu(f"{ferramenta} falhou: {texto[:200]}")
        dados = resultado.get("structuredContent")
        if not isinstance(dados, dict):
            try:
                dados = json.loads(texto)
            except ValueError:
                raise NaoMediu(f"{ferramenta} devolveu algo que nao e JSON") from None
        return dados

    def analisar(self, conteudo: str, linguagem: str, projeto: str, escopo: str) -> list[dict]:
        dados = self.chamar(
            "analyze_code_snippet",
            {
                "fileContent": conteudo,
                "language": linguagem,
                "projectKey": projeto,
                "scope": escopo,
            },
        )
        return [i for i in dados.get("issues") or [] if isinstance(i, dict)]


# ------------------------------------------------------------ calibragem


def sufixo(regra: str) -> str:
    """`typescript:S3735` e `javascript:S3735` sao a mesma regra: S3735."""
    return regra.rsplit(":", 1)[-1]


def pasta_central() -> Path:
    """As calibragens centrais, por chave de projeto: `calibragem/<chave>.json` desta
    esteira. Dependem do perfil do servidor e da imagem do MCP, e nao do
    repositorio, e chegam a todo clone sem esperar a `main` dele."""
    return Path(os.environ.get("SONAR_LOCAL_CALIBRAGEM") or ESTEIRA / "calibragem")


def _ler_calibragem(arquivo: Path, origem: str) -> tuple[set[str], str]:
    """(as regras de `nao_espelhadas`, a versao do MCP que a mediu)."""
    try:
        dados = json.loads(arquivo.read_text())
        regras = dados["nao_espelhadas"]
        if not isinstance(regras, dict | list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise NaoMediu(
            f"{origem} ilegivel: precisa de `nao_espelhadas` (rode --calibrar)"
        ) from None
    return {sufixo(str(regra)) for regra in regras}, str(dados.get("versao_mcp") or "")


def ler_calibracoes(repo: Path, chave: str) -> tuple[dict[str, str], dict[str, str]]:
    """(regra -> de onde veio a excecao, origem -> versao do MCP gravada). A central
    (`calibragem/<chave>.json`) e a do repositorio (`.sonar-local.json`) SOMAM: a
    do repositorio nao substitui a central."""
    fontes: list[tuple[Path, str]] = []
    if CHAVE_SEGURA.fullmatch(chave):
        central = pasta_central() / f"{chave}.json"
        if central.is_file():
            fontes.append((central, ORIGEM_CENTRAL))
    local = repo / CALIBRACAO
    if local.is_file():
        fontes.append((local, CALIBRACAO))
    origens: dict[str, list[str]] = {}
    versoes: dict[str, str] = {}
    for arquivo, origem in fontes:
        regras, versoes[origem] = _ler_calibragem(arquivo, origem)
        for regra in regras:
            origens.setdefault(regra, []).append(origem)
    return {regra: " + ".join(o) for regra, o in origens.items()}, versoes


# ---------------------------------------------------------- o que e novo


@dataclass(frozen=True)
class Achado:
    regra: str
    mensagem: str
    linha: int
    texto: str  # a linha de inicio, sem espacos sobrando

    @property
    def chave(self) -> tuple[str, str, str]:
        return (self.regra, re.sub(r"\d+", "#", self.mensagem), self.texto)


def achados_de(issues: list[dict], conteudo: str) -> list[Achado]:
    linhas = conteudo.splitlines()
    saida = []
    for issue in issues:
        linha = int(((issue.get("textRange") or {}).get("startLine")) or 0)
        texto = " ".join(linhas[linha - 1].split()) if 0 < linha <= len(linhas) else ""
        saida.append(
            Achado(
                str(issue.get("ruleKey", "?")), str(issue.get("primaryMessage", "")), linha, texto
            )
        )
    return saida


def a_mais_proxima(linhas: list[int], linha: int) -> int:
    return min(linhas, key=lambda candidata: abs(candidata - linha))


def so_os_novos(atuais: list[Achado], da_base: list[Achado]) -> list[Achado]:
    """O que a base nao tinha. Cada problema da base casa com UM da versao
    atual (o de linha mais proxima): um segundo igual e novo."""
    livres: dict[tuple, list[int]] = collections.defaultdict(list)
    for antigo in da_base:
        livres[antigo.chave].append(antigo.linha)
    novos = []
    for atual in sorted(atuais, key=lambda a: a.linha):
        candidatas = livres.get(atual.chave)
        if candidatas:
            candidatas.remove(a_mais_proxima(candidatas, atual.linha))
        else:
            novos.append(atual)
    return novos


@dataclass(frozen=True)
class Alvo:
    caminho: str
    antigo: str | None
    escopo: str
    linguagem: str


@dataclass(frozen=True)
class Problema:
    arquivo: str
    linha: int
    regra: str
    mensagem: str


def _plural(n: int, um: str, varios: str) -> str:
    return f"{n} {um if n == 1 else varios}"


@dataclass
class Resultado:
    novos: list[Problema] = field(default_factory=list)
    analisados: int = 0
    so_o_ci: collections.Counter = field(default_factory=collections.Counter)
    nao_medidos: list[str] = field(default_factory=list)
    avisos: collections.Counter = field(default_factory=collections.Counter)
    origens: dict[str, str] = field(default_factory=dict)  # regra -> de onde veio a excecao
    recalibrar: list[str] = field(default_factory=list)
    notas: list[str] = field(default_factory=list)  # o que o MCP nao ve, e so o CI mede
    sem_projeto: bool = False
    motivo_sem_projeto: str = ""

    def linhas(self) -> list[str]:
        ordem = sorted(self.novos, key=lambda p: (p.arquivo, p.linha, p.regra))
        return [f"{p.arquivo}:{p.linha} {p.regra} {p.mensagem}" for p in ordem]

    def resumo(self) -> str:
        if self.sem_projeto:
            motivo = f" (dispensado: {self.motivo_sem_projeto})" if self.motivo_sem_projeto else ""
            return f"este repositorio nao tem projeto Sonar{motivo}: nada a medir"
        arquivos = len({p.arquivo for p in self.novos})
        if self.novos:
            novos = _plural(len(self.novos), "problema novo", "problemas novos")
            em = _plural(arquivos, "arquivo", "arquivos")
            medido = f"{novos} em {em} ({_plural(self.analisados, 'analisado', 'analisados')})"
        else:
            medido = f"0 problemas novos em {_plural(self.analisados, 'arquivo', 'arquivos')}"
        if self.so_o_ci:
            fora = ", ".join(f"{nome} {n}" for nome, n in sorted(self.so_o_ci.items()))
            medido += f"; {sum(self.so_o_ci.values())} so o CI mede ({fora})"
        if self.nao_medidos:
            medido += f"; {len(self.nao_medidos)} sem medir"
        for nota in self.notas:
            medido += f"; so o CI mede, {nota}"
        for recalibrar in self.recalibrar:
            medido += f"; {recalibrar}"
        return medido

    def aviso(self) -> str:
        """As regras que o MCP acusa e o servidor nao (`.sonar-local.json`): o CI decide."""
        if not self.avisos:
            return ""
        regras = ", ".join(
            f"{regra} \u00d7{n} ({self.origens.get(regra, '?')})"
            for regra, n in sorted(self.avisos.items())
        )
        return f"aviso, o CI decide: {regras}"

    def codigo(self) -> int:
        if self.novos:
            return 1
        return 2 if self.nao_medidos else 0

    def motivo_do_2(self) -> str:
        return f"{len(self.nao_medidos)} arquivo(s) sem medir: {'; '.join(self.nao_medidos[:3])}"


def _ler_atual(repo: Path, caminho: str) -> str | None:
    dados = (repo / caminho).read_bytes()
    return None if b"\0" in dados else dados.decode("utf-8", "replace")


def novos_do_arquivo(
    cliente: ClienteMcp, projeto: str, repo: Path, base: str, alvo: Alvo
) -> list[Problema]:
    conteudo = _ler_atual(repo, alvo.caminho)
    if conteudo is None:
        return []
    atuais = achados_de(cliente.analisar(conteudo, alvo.linguagem, projeto, alvo.escopo), conteudo)
    if not atuais:
        return []
    conteudo_base = texto_da_base(repo, base, alvo.antigo) if alvo.antigo else None
    da_base: list[Achado] = []
    if conteudo_base is not None:
        if conteudo_base == conteudo:
            return []
        da_base = achados_de(
            cliente.analisar(conteudo_base, alvo.linguagem, projeto, alvo.escopo), conteudo_base
        )
    return [
        Problema(alvo.caminho, a.linha, a.regra, a.mensagem) for a in so_os_novos(atuais, da_base)
    ]


def separar(
    repo: Path, arquivos: list[tuple[str, str | None]], p: Padroes
) -> tuple[list[Alvo], collections.Counter]:
    alvos: list[Alvo] = []
    so_o_ci: collections.Counter = collections.Counter()
    for caminho, antigo in arquivos:
        escopo = classificar(caminho, p)
        arquivo = repo / caminho
        if escopo is None or arquivo.is_symlink() or not arquivo.is_file():
            continue
        linguagem = linguagem_de(caminho)
        if linguagem and arquivo.stat().st_size > LIMITE_ARQUIVO:
            so_o_ci["arquivo grande"] += 1
        elif linguagem:
            alvos.append(Alvo(caminho, antigo, escopo, linguagem))
        elif Path(caminho).suffix.lower() in SO_O_CI:
            so_o_ci[SO_O_CI[Path(caminho).suffix.lower()]] += 1
    return alvos, so_o_ci


def abrir_cliente(servidor: str, prazo: Prazo) -> ClienteMcp:
    cliente = ClienteMcp(servidor, prazo)
    try:
        cliente.iniciar()
    except NaoMediu as erro:
        raise NaoMediu(f"{erro}. Suba o MCP: {cliente.como_subir}") from None
    return cliente


def existe_no_sonarcloud(chave: str, prazo: Prazo) -> bool:
    try:
        cliente = abrir_cliente("cloud", prazo)
        dados = cliente.chamar("search_my_sonarqube_projects", {"q": chave})
    except NaoMediu:
        return False
    return any(isinstance(p, dict) and p.get("key") == chave for p in dados.get("projects") or [])


def projeto_do_repo(
    repo: Path, forcado: Projeto | None = None, prazo: Prazo | None = None
) -> Projeto | None:
    """O projeto Sonar a medir. Sem chave no workflow (analise automatica do
    SonarCloud, ou sem chamador) e com o repositorio publico, `<dono>_<repo>` se o
    MCP do SonarCloud confirmar que ele existe."""
    projeto = forcado or descobrir(repo)
    if projeto is not None and projeto.chave:
        return projeto
    try:
        dono, _, nome = dono_e_repo(repo).partition("/")
    except NaoMediu:
        return projeto
    if not (dono and nome) or visibilidade_privada(f"{dono}/{nome}"):
        return projeto
    chave = f"{dono}_{nome}"
    if not existe_no_sonarcloud(chave, prazo or Prazo(None)):
        return projeto
    return Projeto(
        chave, "", {**propriedades_do_repo(repo), **(projeto.parametros if projeto else {})}
    )


def _somar(
    resultado: Resultado, alvo: Alvo, saida: list[Problema] | str, ignoradas: dict[str, str]
):
    if isinstance(saida, str):
        resultado.nao_medidos.append(f"{alvo.caminho}: {saida}")
        return
    resultado.analisados += 1
    for problema in saida:
        regra = sufixo(problema.regra)
        if regra in ignoradas:
            resultado.avisos[regra] += 1
            resultado.origens[regra] = ignoradas[regra]
        else:
            resultado.novos.append(problema)


def medir(
    repo: Path,
    projeto: Projeto,
    servidor: str,
    ref: str | None = None,
    apenas: str | None = None,
    prazo: Prazo | None = None,
) -> Resultado:
    prazo = prazo or Prazo(None)
    ignoradas, versoes = ler_calibracoes(repo, projeto.chave)
    base = merge_base(repo, ref)
    arquivos = arquivos_mudados(repo, base)
    if apenas is not None:
        arquivos = [par for par in arquivos if par[0] == apenas]
    alvos, so_o_ci = separar(repo, arquivos, padroes(projeto.parametros))
    resultado = Resultado(so_o_ci=so_o_ci)
    if any(alvo.linguagem == "py" for alvo in alvos):
        resultado.notas.append(NOTA_PYTHON)
    if not alvos:
        return resultado
    cliente = abrir_cliente(servidor, prazo)
    for origem, versao in versoes.items():
        if versao and versao != cliente.versao:
            resultado.recalibrar.append(f"calibracao de {versao}: recalibre ({origem})")

    def um(alvo: Alvo) -> tuple[Alvo, list[Problema] | str]:
        try:
            return alvo, novos_do_arquivo(cliente, projeto.chave, repo, base, alvo)
        except NaoMediu as erro:
            return alvo, str(erro)

    with concurrent.futures.ThreadPoolExecutor(PARALELO) as pool:
        for alvo, saida in pool.map(um, alvos):
            _somar(resultado, alvo, saida, ignoradas)
    return resultado


def rodar(repo: Path, ref: str | None = None, projeto_forcado: Projeto | None = None) -> Resultado:
    """O PR inteiro. NaoMediu se nao deu para decidir nem o projeto nem o servidor."""
    projeto = projeto_do_repo(repo, projeto_forcado)
    if projeto is None or not projeto.chave:
        return Resultado(sem_projeto=True, motivo_sem_projeto=projeto.dispensa if projeto else "")
    return medir(repo, projeto, escolher_servidor(projeto, repo), ref)


# --------------------------------------------------------------- calibrar


def ref_da_main(repo: Path, ref: str | None) -> str:
    for candidata in (ref or REF_MAIN, "main"):
        saida = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidata],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if saida.returncode == 0:
            return candidata
    raise NaoMediu("sem origin/main nem main para calibrar")


def issues_do_servidor(cliente: ClienteMcp, chave: str) -> list[tuple[str, int, str]]:
    """O que o PROPRIO servidor tem aberto no projeto: (arquivo, linha, regra)."""
    achados: list[tuple[str, int, str]] = []
    pagina = 1
    while True:
        dados = cliente.chamar(
            "search_sonar_issues_in_projects",
            {
                "projectKeys": [chave],
                "issueStatuses": STATUS_ABERTOS,
                "pageSize": PAGINA_ISSUES,
                "pageIndex": pagina,
            },
        )
        issues = [i for i in dados.get("issues") or [] if isinstance(i, dict)]
        for issue in issues:
            componente = str(issue.get("component", ""))
            caminho = componente.split(":", 1)[1] if ":" in componente else componente
            linha = int((issue.get("textRange") or {}).get("startLine") or 0)
            achados.append((caminho, linha, sufixo(str(issue.get("rule", "")))))
        total = int((dados.get("paging") or {}).get("total") or 0)
        if not issues or pagina * PAGINA_ISSUES >= total:
            return achados
        pagina += 1


def classificar_regras(
    do_mcp: list[tuple[str, int, str]], do_servidor: list[tuple[str, int, str]]
) -> dict:
    """Cada achado do MCP: `confiavel` se o servidor tem a mesma regra no mesmo
    arquivo e linha; `nao_espelhada` se o servidor nao tem a regra em lugar
    nenhum; `divergente` se tem a regra, mas noutro lugar (so informa)."""
    posicoes = set(do_servidor)
    regras_do_servidor = {regra for _, _, regra in do_servidor}
    confiaveis: collections.Counter = collections.Counter()
    divergentes: collections.Counter = collections.Counter()
    ocorrencias: collections.Counter = collections.Counter()
    exemplos: dict[str, str] = {}
    for caminho, linha, regra in sorted(do_mcp):
        if (caminho, linha, regra) in posicoes:
            confiaveis[regra] += 1
        elif regra in regras_do_servidor:
            divergentes[regra] += 1
        else:
            ocorrencias[regra] += 1
            exemplos.setdefault(regra, f"{caminho}:{linha}")
    return {
        "nao_espelhadas": {
            regra: {"ocorrencias": n, "exemplo": exemplos[regra]}
            for regra, n in sorted(ocorrencias.items())
        },
        "confiaveis": dict(sorted(confiaveis.items())),
        "divergentes": dict(sorted(divergentes.items())),
    }


def alvos_da_ref(repo: Path, ref: str, p: Padroes, somente: tuple[str, ...]) -> list[Alvo]:
    alvos = []
    for caminho in filter(None, git(repo, "ls-tree", "-r", "-z", "--name-only", ref).split("\0")):
        escopo, linguagem = classificar(caminho, p), linguagem_de(caminho)
        if escopo and linguagem and (not somente or caminho.startswith(tuple(somente))):
            alvos.append(Alvo(caminho, None, escopo, linguagem))
    return alvos


def achados_na_ref(
    cliente: ClienteMcp, repo: Path, ref: str, chave: str, alvo: Alvo
) -> tuple[list[tuple[str, int, str]], str | None]:
    conteudo = texto_da_base(repo, ref, alvo.caminho)
    if conteudo is None or "\0" in conteudo or len(conteudo) > LIMITE_ARQUIVO:
        return [], None
    try:
        issues = cliente.analisar(conteudo, alvo.linguagem, chave, alvo.escopo)
    except NaoMediu as erro:
        return [], f"{alvo.caminho}: {erro}"
    return [(alvo.caminho, a.linha, sufixo(a.regra)) for a in achados_de(issues, conteudo)], None


def calibrar(
    repo: Path,
    projeto: Projeto,
    servidor: str,
    ref: str | None = None,
    somente: tuple[str, ...] = (),
    aviso=None,
) -> dict:
    """O `.sonar-local.json`: o que o MCP acusa na main e o servidor nao."""
    ref = ref_da_main(repo, ref)
    alvos = alvos_da_ref(repo, ref, padroes(projeto.parametros), somente)
    cliente = abrir_cliente(servidor, Prazo(None))
    do_servidor = issues_do_servidor(cliente, projeto.chave)
    do_mcp: list[tuple[str, int, str]] = []
    sem_medir: list[str] = []

    def um(alvo: Alvo) -> tuple[list[tuple[str, int, str]], str | None]:
        return achados_na_ref(cliente, repo, ref, projeto.chave, alvo)

    with concurrent.futures.ThreadPoolExecutor(PARALELO) as pool:
        for i, (achados, erro) in enumerate(pool.map(um, alvos), 1):
            do_mcp.extend(achados)
            sem_medir.extend([erro] if erro else [])
            if aviso and i % 25 == 0:
                aviso(f"sonar-local: calibrando, {i}/{len(alvos)} arquivos")
    return {
        "projeto": projeto.chave,
        "ref": git(repo, "rev-parse", "--short", ref).strip(),
        "versao_mcp": cliente.versao,
        "data": time.strftime("%Y-%m-%d"),
        "arquivos_analisados": len(alvos) - len(sem_medir),
        "arquivos_sem_medir": len(sem_medir),
        "problemas_do_servidor": len(do_servidor),
        **classificar_regras(do_mcp, do_servidor),
    }


# -------------------------------------------------------------------- CLI


def raiz_do_repo(inicio: Path) -> Path:
    return Path(git(inicio, "rev-parse", "--show-toplevel").strip())


def imprimir(resultado: Resultado) -> int:
    for linha in resultado.linhas():
        diz(linha)
    diz(f"sonar-local: {resultado.resumo()}")
    if resultado.aviso():
        diz(f"sonar-local: {resultado.aviso()}")
    codigo = resultado.codigo()
    if codigo == 2:
        diz(f"sonar-local: nao consegui medir: {resultado.motivo_do_2()}", sys.stderr)
    return codigo


def _estourou(*_: object) -> None:
    raise TimeoutError("prazo do hook")


def confinar(caminho: str | Path, raizes: list[str]) -> str | None:
    """O caminho real (links resolvidos) se ele fica DENTRO de uma das raizes, e
    None se nao: um link que aponta para fora nao passa. `realpath` +
    `commonpath`, a forma que o Sonar reconhece (S2083, S6549, S8707)."""
    real = os.path.realpath(caminho)
    for raiz in raizes:
        base = os.path.realpath(raiz)
        if os.path.commonpath([base, real]) == base:
            return real
    return None


def hook(entrada, onde: Path) -> int:
    """PostToolUse: so um arquivo DENTRO do repositorio, e edicao nunca trava por
    causa do MCP. O repositorio e o do diretorio do hook (`--repo`, o atual), e nao
    o que o arquivo diz: um `file_path` fora dele, ou um link para fora, sai 0."""
    prazo = Prazo(PRAZO_HOOK_S)
    # O `Prazo` corta as chamadas ao MCP; o alarme corta o resto (o yq, o `gh`).
    signal.signal(signal.SIGALRM, _estourou)
    signal.alarm(max(1, int(PRAZO_HOOK_S)))
    try:
        repo = raiz_do_repo(onde)
        real = confinar(json.load(entrada)["tool_input"]["file_path"], [str(repo)])
        if real is None:
            return 0
        relativo = Path(os.path.relpath(real, os.path.realpath(repo))).as_posix()
        projeto = projeto_do_repo(repo, prazo=prazo)
        if projeto is None or not projeto.chave:
            return 0
        alvos, _ = separar(repo, [(relativo, relativo)], padroes(projeto.parametros))
        if not alvos:
            return 0
        resultado = medir(
            repo, projeto, escolher_servidor(projeto, repo), apenas=relativo, prazo=prazo
        )
    except Exception:  # noqa: BLE001 - o hook nunca atrapalha a edicao
        return 0
    finally:
        signal.alarm(0)
    if not resultado.novos:
        return 0
    diz(f"sonar-local: {resultado.resumo()}", sys.stderr)
    for linha in resultado.linhas():
        diz(linha, sys.stderr)
    return 2


def argumentos(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Detalhes no cabecalho de bin/sonar_local.py.",
    )
    ap.add_argument("--repo", type=Path, default=Path.cwd(), help="o repositorio (padrao: o atual)")
    ap.add_argument("--base", help=f"ref do merge-base (padrao: {REF_MAIN})")
    ap.add_argument(
        "--projeto",
        metavar="CHAVE",
        help="a chave do projeto Sonar, quando o workflow a dispensa (ex.: a analise automatica "
        "do SonarCloud); os parametros saem do sonar-project.properties",
    )
    ap.add_argument("--hook", action="store_true", help="hook PostToolUse: JSON na stdin")
    ap.add_argument(
        "--calibrar",
        action="store_true",
        help="gera o .sonar-local.json: o que o MCP acusa na main e o servidor nao",
    )
    ap.add_argument(
        "--somente",
        action="append",
        default=[],
        metavar="PREFIXO",
        help="com --calibrar: so os arquivos sob este caminho (repetivel)",
    )
    return ap.parse_args(argv)


REF_SEGURA = re.compile(r"\w[\w./~^@{}-]*")
CHAVE_SEGURA = re.compile(r"[\w.:-]+")


def argumentos_seguros(args: argparse.Namespace) -> None:
    """O que vem da linha de comando so entra se for do tamanho que se espera: a
    ref e a chave sao palavras (nada que o git leia como opcao), `--somente` e
    relativo e fica dentro do repositorio. Nenhum caminho da linha de comando e
    aberto para escrita: o JSON da calibragem sai no stdout (`> arquivo`)."""
    if args.base and not REF_SEGURA.fullmatch(args.base):
        raise NaoMediu(f"--base espera uma ref do git, e nao {args.base!r}")
    if args.projeto and not CHAVE_SEGURA.fullmatch(args.projeto):
        raise NaoMediu(f"--projeto espera uma chave Sonar, e nao {args.projeto!r}")
    for prefixo in args.somente:
        normal = os.path.normpath(prefixo)
        if os.path.isabs(normal) or normal == ".." or normal.startswith(".." + os.sep):
            raise NaoMediu(f"--somente espera um caminho dentro do repositorio: {prefixo!r}")


def projeto_pedido(repo: Path, args: argparse.Namespace) -> Projeto | None:
    """`--projeto`: a chave vem de fora (o workflow a dispensa) e os parametros do
    sonar-project.properties. Sem ele, quem decide e o workflow chamador."""
    if not args.projeto:
        return None
    return Projeto(args.projeto, "", propriedades_do_repo(repo))


def rodar_calibragem(repo: Path, args: argparse.Namespace) -> None:
    projeto = projeto_do_repo(repo, projeto_pedido(repo, args))
    if projeto is None or not projeto.chave:
        diz("sonar-local: este repositorio nao tem projeto Sonar: nada a calibrar", sys.stderr)
        return
    calibragem = calibrar(
        repo,
        projeto,
        escolher_servidor(projeto, repo),
        args.base,
        tuple(args.somente),
        lambda texto: diz(texto, sys.stderr),
    )
    texto = json.dumps(calibragem, indent=2, ensure_ascii=False) + "\n"
    diz(texto.rstrip("\n"))
    diz(
        f"sonar-local: calibrado em {calibragem['arquivos_analisados']} arquivos: "
        f"{len(calibragem['nao_espelhadas'])} regra(s) nao espelhada(s), "
        f"{len(calibragem['confiaveis'])} confiavel(is)",
        sys.stderr,
    )


def main(argv: list[str] | None = None, entrada=None) -> int:
    args = argumentos(argv)
    if args.hook:
        return hook(entrada or sys.stdin, args.repo)
    try:
        repo = raiz_do_repo(args.repo)
        argumentos_seguros(args)
        if args.calibrar:
            rodar_calibragem(repo, args)
            return 0
        resultado = rodar(repo, args.base, projeto_pedido(repo, args))
    except NaoMediu as erro:
        diz(f"sonar-local: nao consegui medir: {erro}", sys.stderr)
        return 2
    except Exception as erro:  # noqa: BLE001 - nunca deixa um traceback chegar perto do token
        diz(f"sonar-local: nao consegui medir: erro inesperado ({type(erro).__name__})", sys.stderr)
        return 2
    if resultado.sem_projeto:
        diz(f"sonar-local: {resultado.resumo()}")
        return 0
    return imprimir(resultado)


if __name__ == "__main__":
    sys.exit(main())

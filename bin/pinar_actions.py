#!/usr/bin/env python3
"""Mantem todo `uses:` fixado por SHA, com a lista de versoes num lugar so.

POR QUE FIXAR POR SHA
---------------------
Uma tag de git e um ponteiro movel. `actions/checkout@v4` nao diz qual codigo
roda: diz "o que o dono do repositorio estiver chamando de v4 hoje". Quem
controla aquele repositorio pode reapontar a tag, e o commit novo roda dentro do
job — com o GITHUB_TOKEN do run e com os secrets que o job recebe. Foi assim que
o `tj-actions/changed-files` alcancou milhares de pipelines em 2025.

O `simulator-ccb` usa `@v6`, `@v5`, `@v3`. O `TranslateReader` fixa tudo por SHA
e acerta — mas paga o preco: a mesma versao do `harden-runner` aparece em doze
arquivos, e atualizar e editar doze lugares na mao, torcendo para nao esquecer
nenhum.

POR QUE ESTE SCRIPT
-------------------
Aqui a lista de versoes e DADO, em `actions.lock.json`, e o YAML e gerado a
partir dela. Sao tres modos:

    --verificar   falha se algum `uses:` estiver por tag, ou se o SHA no YAML
                  divergir do lock. E o que roda no CI deste repositorio.
    --aplicar     reescreve os `uses:` dos workflows com o SHA do lock.
    --atualizar   consulta a API do GitHub, traz a versao mais nova de cada
                  action para o lock e aplica. Abre a porta para revisar o diff
                  antes de commitar — atualizar action e mudar codigo de
                  terceiro que roda com os seus secrets, entao passa por leitura.

O comentario `# vX.Y.Z` ao lado do SHA nao e enfeite: sem ele ninguem consegue
ler um diff de quarenta caracteres hexadecimais e dizer se subiu ou desceu.

USO
    python bin/pinar_actions.py --verificar
    python bin/pinar_actions.py --aplicar
    python bin/pinar_actions.py --atualizar            # precisa de gh ou GITHUB_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LOCK = RAIZ / "actions.lock.json"

# Casa `uses: owner/repo[/sub/path]@ref` com o comentario opcional depois.
# Actions locais (`uses: ./.github/...`) e workflows reutilizaveis do proprio
# repositorio ficam de fora de proposito: eles nao sao codigo de terceiro.
USES = re.compile(
    r"(?P<inicio>^\s*(?:-\s*)?uses:\s*)"
    r"(?P<dono>[A-Za-z0-9][\w.-]*)/(?P<repo>[\w.-]+)"
    r"(?P<sub>(?:/[\w.-]+)*)"
    r"@(?P<ref>[^\s#]+)"
    r"(?P<resto>[^\n]*)$",
    re.MULTILINE,
)
SHA = re.compile(r"^[0-9a-f]{40}$")


def carregar() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8"))


def salvar(dados: dict) -> None:
    LOCK.write_text(json.dumps(dados, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def arquivos() -> list[Path]:
    alvos = sorted((RAIZ / ".github" / "workflows").glob("*.yml"))
    alvos += sorted((RAIZ / ".github" / "actions").glob("*/action.yml"))
    return alvos


def api(caminho: str) -> dict:
    """GET na API do GitHub, pelo `gh` se houver, senao por HTTP.

    O `gh` ja carrega a credencial da maquina; o caminho HTTP existe para rodar
    dentro do CI, onde `gh` existe mas o token vem por ambiente.
    """
    if os.environ.get("GITHUB_TOKEN"):
        req = urllib.request.Request(
            f"https://api.github.com/{caminho}",
            headers={
                "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "pinar-actions",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resposta:
            return json.load(resposta)

    saida = subprocess.run(["gh", "api", caminho], capture_output=True, text=True, check=True)
    return json.loads(saida.stdout)


def versao_mais_nova(acao: str) -> str:
    """Maior tag no formato vN.N.N.

    NAO se usa a release "latest": o `github/codeql-action` publica ali o bundle
    do CodeQL (`codeql-bundle-v2.27.0`), que nao e a versao da action. Pegar a
    release latest ali resulta num SHA que existe e nao e o que se quer — e o
    erro so aparece quando a action deixa de funcionar.
    """
    tags = [t["name"] for t in api(f"repos/{acao}/tags?per_page=100")]
    semver = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    if not semver:
        raise SystemExit(f"{acao}: nenhuma tag vN.N.N encontrada")
    return max(semver, key=lambda t: [int(n) for n in t[1:].split(".")])


def sha_da_tag(acao: str, tag: str) -> str:
    return api(f"repos/{acao}/commits/{tag}")["sha"]


def percorrer(texto: str, acoes: dict, problemas: list[str], arquivo: Path, proprio: str) -> str:
    def troca(m: re.Match) -> str:
        nome = f"{m['dono']}/{m['repo']}"
        # Auto-referencia fica como esta. Os workflows deste repositorio se
        # compoem entre si por @main; fixar isso por SHA exigiria um commit a
        # mais a cada mudanca interna so para reapontar o anterior, e nao compra
        # seguranca nenhuma — e o mesmo codigo, do mesmo dono, na mesma revisao.
        if nome == proprio:
            return m.group(0)
        entrada = acoes.get(nome)
        if entrada is None:
            problemas.append(
                f"{arquivo.relative_to(RAIZ)}: `{nome}` nao esta em actions.lock.json. "
                "Acrescente com versao e SHA, ou rode --atualizar."
            )
            return m.group(0)
        if not SHA.match(m["ref"]):
            problemas.append(
                f"{arquivo.relative_to(RAIZ)}: `{nome}@{m['ref']}` esta por tag, nao por SHA."
            )
        elif m["ref"] != entrada["sha"]:
            problemas.append(
                f"{arquivo.relative_to(RAIZ)}: `{nome}` esta em {m['ref'][:12]} "
                f"e o lock diz {entrada['sha'][:12]}."
            )
        return f"{m['inicio']}{nome}{m['sub']}@{entrada['sha']} # {entrada['versao']}"

    return USES.sub(troca, texto)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--verificar", action="store_true")
    grupo.add_argument("--aplicar", action="store_true")
    grupo.add_argument("--atualizar", action="store_true")
    args = ap.parse_args()

    lock = carregar()
    acoes = lock["actions"]
    proprio = lock.get("_proprio", "")

    if args.atualizar:
        for nome, entrada in acoes.items():
            tag = versao_mais_nova(nome)
            sha = sha_da_tag(nome, tag)
            if (tag, sha) != (entrada["versao"], entrada["sha"]):
                print(f"  {nome}: {entrada['versao']} -> {tag}")
                entrada["versao"], entrada["sha"] = tag, sha
            else:
                print(f"  {nome}: {tag} (sem mudanca)")
        salvar(lock)

    problemas: list[str] = []
    mudados: list[Path] = []
    for arquivo in arquivos():
        original = arquivo.read_text(encoding="utf-8")
        novo = percorrer(original, acoes, problemas, arquivo, proprio)
        if novo != original and not args.verificar:
            arquivo.write_text(novo, encoding="utf-8")
            mudados.append(arquivo)

    if args.verificar:
        # dict.fromkeys em vez de set: tira a repeticao e preserva a ordem, para
        # a saida ficar na ordem dos arquivos e nao aleatoria a cada run.
        problemas = list(dict.fromkeys(problemas))
        if problemas:
            for p in problemas:
                print(f"::error::{p}")
            print(
                f"\n{len(problemas)} `uses:` fora do lock. "
                "Rode `python bin/pinar_actions.py --aplicar`."
            )
            return 1
        print(f"Todos os `uses:` de {len(arquivos())} arquivo(s) estao fixados pelo lock.")
        return 0

    for arquivo in mudados:
        print(f"  reescrito {arquivo.relative_to(RAIZ)}")
    # Problema que --aplicar nao resolve sozinho: action ausente do lock.
    ausentes = [p for p in problemas if "nao esta em actions.lock.json" in p]
    for p in ausentes:
        print(f"::error::{p}")
    return 1 if ausentes else 0


if __name__ == "__main__":
    sys.exit(main())

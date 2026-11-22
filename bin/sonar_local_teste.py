#!/usr/bin/env python3
"""Testes de bin/sonar_local.py e do passo do Sonar local no validar_local.py.

O que importa aqui nao e "acha o problema": e "NAO chama de novo o que ja
existia", "NAO deixa o token sair" e "NAO registra a rodada com problema novo".
Por isso cada teste tem uma ISCA: a classe `Iscas` estraga a regra que ele
protege (uma troca de texto no codigo, numa copia de bin/) e exige que ESSE teste
reprove com a regra estragada. Um teste que nunca foi visto vermelho nao prova
nada, e uma troca que deixa de casar tambem reprova (o codigo mudou, a isca nao).

O servidor MCP e falso, em thread (`http.server`), e o helper de headers tambem:
um script que devolve um token-canario. O canario nunca pode aparecer na saida.

    python3 bin/sonar_local_teste.py
    SONAR_LOCAL_BIN_DIR=<copia de bin/> python3 bin/sonar_local_teste.py Classe.teste
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(os.environ.get("SONAR_LOCAL_BIN_DIR") or Path(__file__).resolve().parent)
sys.path.insert(0, str(BIN))

import sonar_local  # noqa: E402
import validar_local  # noqa: E402

SCRIPT = BIN / "sonar_local.py"
CANARIO = "CANARIO-sonar-local-4f9c2b7e1d"

WORKFLOW = """\
name: ci
on: pull_request
jobs:
  esteira:
    uses: slipalison/github-workflows/.github/workflows/pipeline.yml@main
    with:
      sonar_projeto: meu-projeto
      sonar_host: http://10.10.0.13:30900
      sonar_parametros: |
        sonar.sources=src
        sonar.exclusions=src/gerado/**
        sonar.tests=src
        sonar.test.inclusions=**/*.test.ts
"""

BASE_A = "export function a() {\n  // TODO fazer\n  return 1;\n}\n"


def achado(regra: str, mensagem: str, linha: int) -> dict:
    return {
        "ruleKey": regra,
        "primaryMessage": mensagem,
        "severity": "INFO",
        "textRange": {"startLine": linha, "endLine": linha},
    }


def regras_do_sonar(argumentos: dict) -> list[dict]:
    """O que o analisador "acha": TODO (S1135) e funcao com mais de tres `if (`
    (S3776, no cabecalho, com o numero na mensagem)."""
    conteudo = argumentos["fileContent"]
    ifs = conteudo.count("if (")
    achados = []
    for n, linha in enumerate(conteudo.splitlines(), 1):
        if "TODO" in linha:
            achados.append(
                achado(
                    "typescript:S1135", 'Complete the task associated to this "TODO" comment.', n
                )
            )
        if re.match(r"function \w+", linha) and ifs > 3:
            achados.append(
                achado(
                    "typescript:S3776",
                    f"Refactor this function to reduce its Cognitive Complexity from {ifs} to"
                    " the 15 allowed.",
                    n,
                )
            )
    return achados


class McpFalso:
    """Um servidor MCP em 127.0.0.1, numa thread. `modo`: ok | 500 | 401 | rpc |
    redirect | sse | lento."""

    def __init__(self, modo: str = "ok", destino_do_redirect: str = "") -> None:
        self.modo = modo
        self.destino = destino_do_redirect
        self.chamadas: list[dict] = []
        self.buscas: list[dict] = []  # os argumentos de search_sonar_issues_in_projects
        self.do_servidor: list[dict] = []  # o que o servidor "tem aberto"
        self.projetos_cloud: list[str] = []  # o que search_my_sonarqube_projects acha
        self.consultas: list[dict] = []  # os argumentos de search_my_sonarqube_projects
        self.autorizacoes: list[str | None] = []
        servidor = self

        class Tratador(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                servidor.autorizacoes.append(self.headers.get("Authorization"))
                servidor.responder(self, corpo)

            def do_GET(self) -> None:
                # So o redirecionamento seguido chega aqui, e com os cabecalhos que o urllib copiou.
                servidor.autorizacoes.append(self.headers.get("Authorization"))
                servidor.enviar(self, 200, "{}")

        class Silencioso(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address) -> None:
                pass  # o cliente que desistiu (prazo, redirecionamento) nao e erro do teste

        self.http = Silencioso(("127.0.0.1", 0), Tratador)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.http.server_address[1]}/mcp"

    def __enter__(self) -> McpFalso:
        self.thread.start()
        return self

    def __exit__(self, *args) -> None:
        self.http.shutdown()
        self.http.server_close()

    def enviar(self, tratador, codigo: int, corpo: str = "", **cabecalhos: str) -> None:
        dados = corpo.encode()
        tratador.send_response(codigo)
        tratador.send_header("Content-Length", str(len(dados)))
        for nome, valor in cabecalhos.items():
            tratador.send_header(nome.replace("_", "-"), valor)
        tratador.end_headers()
        tratador.wfile.write(dados)

    def responder(self, tratador, pedido: dict) -> None:
        autorizacao = self.autorizacoes[-1]
        if self.modo == "500":
            return self.enviar(tratador, 500, f"quebrou com {autorizacao}")
        if self.modo == "401" or autorizacao != f"Bearer {CANARIO}":
            return self.enviar(tratador, 401, f"token {autorizacao} recusado")
        if self.modo == "redirect":
            return self.enviar(tratador, 302, "", Location=self.destino)
        if self.modo == "lento":
            time.sleep(5)
        metodo = pedido.get("method")
        if metodo == "notifications/initialized":
            return self.enviar(tratador, 202)
        sem_sessao = tratador.headers.get("Mcp-Session-Id") != "sessao-1"
        if self.modo == "sse" and metodo != "initialize" and sem_sessao:
            return self.enviar(tratador, 400, "sem sessao")
        if metodo == "initialize":
            resultado = {"serverInfo": {"name": "falso", "version": "falso-1.0"}}
        elif self.modo == "rpc":
            erro = {"code": -32000, "message": f"nao deu, o token era {CANARIO}"}
            return self.json(tratador, {"jsonrpc": "2.0", "id": pedido["id"], "error": erro})
        else:
            argumentos = pedido["params"]["arguments"]
            if pedido["params"]["name"] == "search_my_sonarqube_projects":
                self.consultas.append(argumentos)
                achados = [{"key": k} for k in self.projetos_cloud if k == argumentos.get("q")]
                texto = json.dumps({"projects": achados})
            elif pedido["params"]["name"] == "search_sonar_issues_in_projects":
                self.buscas.append(argumentos)
                texto = json.dumps(self.pagina(argumentos))
            else:
                self.chamadas.append(argumentos)
                texto = json.dumps({"issues": regras_do_sonar(argumentos)})
            resultado = {"content": [{"type": "text", "text": texto}], "isError": False}
        self.json(tratador, {"jsonrpc": "2.0", "id": pedido["id"], "result": resultado})

    def pagina(self, argumentos: dict) -> dict:
        tamanho, indice = argumentos["pageSize"], argumentos["pageIndex"]
        abertas = [i for i in self.do_servidor if i["status"] in argumentos["issueStatuses"]]
        fatia = abertas[(indice - 1) * tamanho : indice * tamanho]
        return {"issues": fatia, "paging": {"pageIndex": indice, "total": len(abertas)}}

    def json(self, tratador, mensagem: dict) -> None:
        if self.modo == "sse":
            corpo = f"event: message\ndata: {json.dumps(mensagem)}\n\n"
            self.enviar(
                tratador,
                200,
                corpo,
                Content_Type="text/event-stream",
                Mcp_Session_Id="sessao-1",
            )
        else:
            self.enviar(tratador, 200, json.dumps(mensagem), Content_Type="application/json")

    def conteudos(self) -> list[str]:
        return [c["fileContent"] for c in self.chamadas]


def do_servidor(arquivo: str, linha: int, regra: str, status: str = "OPEN") -> dict:
    return {
        "rule": f"typescript:{regra}",
        "component": f"meu-projeto:{arquivo}",
        "status": status,
        "textRange": {"startLine": linha},
    }


def dorme(_repo: Path) -> None:
    time.sleep(4)


def porta_fechada() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}/mcp"


class Oficina:
    """Um repositorio git descartavel, na branch `feat`, com `origin/main` na base,
    mais o helper falso de headers."""

    def __init__(self, workflow: str | None = WORKFLOW) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name) / "demo"
        self.raiz.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "teste@exemplo.invalido")
        self.git("config", "user.name", "teste")
        self.git("config", "commit.gpgsign", "false")
        self.git("remote", "add", "origin", "https://github.com/exemplo/demo.git")
        if workflow is not None:
            self.escrever(".github/workflows/ci.yml", workflow)
        self.escrever("src/a.ts", BASE_A)
        self.escrever("README.md", "# demo\n")
        self.commitar("base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "-q", "-b", "feat")
        self.helper = self.criar_helper(CANARIO, 0)

    def criar_helper(self, token: str, codigo: int) -> Path:
        helper = Path(self.tmp.name) / f"helper{codigo}.sh"
        helper.write_text(
            f'#!/bin/sh\nprintf \'{{"Authorization":"Bearer {token}"}}\'\nexit {codigo}\n'
        )
        helper.chmod(0o755)
        return helper

    def git(self, *args: str) -> str:
        saida = subprocess.run(
            ["git", *args], cwd=self.raiz, capture_output=True, text=True, encoding="utf-8"
        )
        if saida.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {saida.stderr}")
        return saida.stdout.strip()

    def escrever(self, caminho: str, texto: str) -> Path:
        arquivo = self.raiz / caminho
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(texto)
        return arquivo

    def commitar(self, mensagem: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", mensagem)

    def ambiente(self, url: str, visibilidade: str = "privado") -> dict[str, str]:
        return {
            "SONAR_LOCAL_URL": url,
            "SONAR_LOCAL_HELPER": str(self.helper),
            "SONAR_LOCAL_VISIBILIDADE": visibilidade,
        }

    def rodar(
        self,
        url: str,
        *argumentos: str,
        entrada: str | None = None,
        visibilidade: str = "privado",
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.raiz), *argumentos],
            capture_output=True,
            text=True,
            input=entrada,
            env={**os.environ, **self.ambiente(url, visibilidade)},
            timeout=60,
        )

    def calibrar(self, url: str, **opcoes) -> dict:
        with mock.patch.dict(os.environ, self.ambiente(url)):
            projeto = sonar_local.descobrir(self.raiz)
            return sonar_local.calibrar(self.raiz, projeto, "homelab", **opcoes)

    def medir(self, url: str) -> sonar_local.Resultado:
        with mock.patch.dict(os.environ, self.ambiente(url)):
            return sonar_local.rodar(self.raiz)

    def limpar(self) -> None:
        self.tmp.cleanup()


class Base(unittest.TestCase):
    def oficina(self, workflow: str | None = WORKFLOW) -> Oficina:
        o = Oficina(workflow)
        self.addCleanup(o.limpar)
        return o


def linhas_dos_novos(resultado: sonar_local.Resultado) -> list[tuple[str, int, str]]:
    return [(p.arquivo, p.linha, p.regra) for p in resultado.novos]


class Novos(Base):
    def test_linha_deslocada_nao_e_nova(self):
        o = self.oficina()
        o.escrever("src/a.ts", "// cabecalho\n// outro\n// mais um\n" + BASE_A)
        with McpFalso() as mcp:
            resultado = o.medir(mcp.url)
        self.assertEqual(linhas_dos_novos(resultado), [])
        self.assertEqual(resultado.codigo(), 0)
        self.assertEqual(resultado.analisados, 1)

    def test_o_mesmo_problema_de_novo_e_novo(self):
        o = self.oficina()
        o.escrever(
            "src/a.ts", BASE_A + "\nexport function b() {\n  // TODO fazer\n  return 2;\n}\n"
        )
        with McpFalso() as mcp:
            resultado = o.medir(mcp.url)
        self.assertEqual(linhas_dos_novos(resultado), [("src/a.ts", 7, "typescript:S1135")])
        self.assertEqual(resultado.codigo(), 1)

    def test_complexidade_que_cresceu_sem_mudar_o_cabecalho_e_nova(self):
        o = self.oficina()
        corpo = "function f(a) {\n" + "  if (a) { a++; }\n" * 2 + "  return a;\n}\n"
        o.escrever("src/f.ts", corpo)
        o.commitar("f simples")
        o.git("update-ref", "refs/remotes/origin/main", "HEAD")
        o.escrever(
            "src/f.ts", corpo.replace("  return a;", "  if (a) { a--; }\n" * 3 + "  return a;")
        )
        with McpFalso() as mcp:
            resultado = o.medir(mcp.url)
        self.assertEqual(linhas_dos_novos(resultado), [("src/f.ts", 1, "typescript:S3776")])

    def test_funcao_ja_acima_do_limite_que_cresce_nao_e_nova(self):
        o = self.oficina()
        corpo = "function f(a) {\n" + "  if (a) { a++; }\n" * 4 + "  return a;\n}\n"
        o.escrever("src/f.ts", corpo)
        o.commitar("f complexa")
        o.git("update-ref", "refs/remotes/origin/main", "HEAD")
        o.escrever(
            "src/f.ts", corpo.replace("  return a;", "  if (a) { a--; }\n" * 2 + "  return a;")
        )
        with McpFalso() as mcp:
            resultado = o.medir(mcp.url)
        self.assertEqual(linhas_dos_novos(resultado), [])

    def test_os_tres_lugares_do_pr(self):
        o = self.oficina()
        todo = "// TODO {}\nexport const x = 1;\n"
        o.escrever("src/commitado.ts", todo.format("c"))
        o.commitar("commitado")
        o.escrever("src/indexado.ts", todo.format("i"))
        o.git("add", "src/indexado.ts")
        o.escrever("src/solto.ts", todo.format("s"))
        o.escrever("src/a.ts", BASE_A.replace("return 1", "return 3"))
        with McpFalso() as mcp:
            resultado = o.medir(mcp.url)
        self.assertEqual(
            sorted(p.arquivo for p in resultado.novos),
            ["src/commitado.ts", "src/indexado.ts", "src/solto.ts"],
        )
        self.assertEqual(resultado.analisados, 4)

    def test_arquivo_de_teste_vai_com_scope_test(self):
        o = self.oficina()
        o.escrever("src/b.test.ts", "// arquivo de teste\nexport const t = 1;\n")
        o.escrever("src/c.ts", "// arquivo de producao\nexport const c = 1;\n")
        with McpFalso() as mcp:
            o.medir(mcp.url)
        escopos = {c["fileContent"].split("\n")[0]: c["scope"] for c in mcp.chamadas}
        self.assertEqual(escopos, {"// arquivo de teste": "TEST", "// arquivo de producao": "MAIN"})
        self.assertEqual({c["projectKey"] for c in mcp.chamadas}, {"meu-projeto"})
        self.assertEqual({c["language"] for c in mcp.chamadas}, {"ts"})

    def test_exclusions_tira_o_arquivo(self):
        o = self.oficina()
        o.escrever("src/gerado/x.ts", "// TODO gerado\n")
        o.escrever("lib/y.ts", "// TODO fora das fontes\n")
        with McpFalso() as mcp:
            resultado = o.medir(mcp.url)
        self.assertEqual(mcp.chamadas, [])
        self.assertEqual(resultado.analisados, 0)
        self.assertEqual(resultado.novos, [])

    def test_rust_conta_em_so_o_ci(self):
        o = self.oficina()
        o.escrever("src/m.rs", "fn main() {}\n")
        o.escrever("src/n.rs", "fn n() {}\n")
        o.escrever("src/c.ts", "export const c = 1;\n")
        with McpFalso() as mcp:
            resultado = o.medir(mcp.url)
        self.assertEqual(resultado.so_o_ci["Rust"], 2)
        self.assertIn("2 so o CI mede (Rust 2)", resultado.resumo())
        self.assertEqual(mcp.conteudos(), ["export const c = 1;\n"])

    def test_so_rust_nao_precisa_do_mcp(self):
        o = self.oficina()
        o.escrever("src/m.rs", "fn main() {}\n")
        resultado = o.medir(porta_fechada())
        self.assertEqual(resultado.codigo(), 0)
        self.assertEqual(resultado.so_o_ci["Rust"], 1)

    def test_a_visibilidade_do_repositorio_escolhe_o_servidor(self):
        projeto = sonar_local.Projeto("x")
        raiz = self.oficina().raiz
        self.assertEqual(sonar_local.escolher_servidor(projeto, raiz, lambda _: True), "homelab")
        self.assertEqual(sonar_local.escolher_servidor(projeto, raiz, lambda _: False), "cloud")
        nuvem = sonar_local.Projeto("x", "https://sonarcloud.io")
        self.assertEqual(sonar_local.escolher_servidor(nuvem, raiz, lambda _: True), "cloud")
        casa = sonar_local.Projeto("x", "http://10.10.0.13:30900")
        self.assertEqual(sonar_local.escolher_servidor(casa, raiz, lambda _: False), "homelab")
        with self.assertRaises(sonar_local.NaoMediu):
            sonar_local.escolher_servidor(sonar_local.Projeto("x", "https://outro.exemplo"), raiz)

    def test_sem_projeto_sonar_nao_chama_o_mcp(self):
        o = self.oficina(WORKFLOW.replace("sonar_projeto: meu-projeto", 'sonar_projeto: ""'))
        o.escrever("src/c.ts", "// TODO\n")
        with McpFalso() as mcp:
            resultado = o.medir(mcp.url)
        self.assertTrue(resultado.sem_projeto)
        self.assertEqual(mcp.chamadas, [])
        processo = o.rodar(porta_fechada())
        self.assertEqual(processo.returncode, 0, processo.stderr)
        self.assertIn("nao tem projeto Sonar", processo.stdout)

    def test_reutilizavel_que_so_repassa_inputs_nao_e_um_chamador(self):
        reutilizavel = WORKFLOW.replace("on: pull_request", "on:\n  workflow_call:").replace(
            "sonar_projeto: meu-projeto", "sonar_projeto: ${{ inputs.sonar_projeto }}"
        )
        o = self.oficina(reutilizavel)
        self.assertIsNone(sonar_local.descobrir(o.raiz))

    def test_chave_que_e_expressao_num_chamador_nao_mede(self):
        o = self.oficina(WORKFLOW.replace("meu-projeto", "${{ vars.PROJETO }}"))
        with self.assertRaises(sonar_local.NaoMediu):
            sonar_local.descobrir(o.raiz)

    def test_sonar_project_properties_quando_nao_ha_chamador(self):
        o = self.oficina(workflow=None)
        o.escrever("sonar-project.properties", "sonar.projectKey=do-arquivo\nsonar.sources=src\n")
        projeto = sonar_local.descobrir(o.raiz)
        self.assertEqual(projeto.chave, "do-arquivo")
        self.assertEqual(projeto.parametros["sonar.sources"], "src")


COMPLEXA = "function f(a) {\n" + "  if (a) { a++; }\n" * 4 + "  return a;\n}\n"


class Calibracao(Base):
    """`--calibrar` e a leitura do `.sonar-local.json`."""

    def com_a_main_calibravel(self) -> Oficina:
        """Na main: um TODO (S1135) em a.ts e uma funcao complexa (S3776) em f.ts."""
        o = self.oficina()
        o.escrever("src/f.ts", COMPLEXA)
        o.commitar("f")
        o.git("update-ref", "refs/remotes/origin/main", "HEAD")
        return o

    def test_regra_que_o_servidor_nao_tem_e_nao_espelhada_e_a_que_bate_e_confiavel(self):
        o = self.com_a_main_calibravel()
        with McpFalso() as mcp:
            mcp.do_servidor = [do_servidor("src/a.ts", 2, "S1135")]
            calibragem = o.calibrar(mcp.url)
        self.assertEqual(
            calibragem["nao_espelhadas"], {"S3776": {"ocorrencias": 1, "exemplo": "src/f.ts:1"}}
        )
        self.assertEqual(calibragem["confiaveis"], {"S1135": 1})
        self.assertEqual(calibragem["divergentes"], {})
        self.assertEqual(calibragem["versao_mcp"], "falso-1.0")
        self.assertRegex(calibragem["data"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(calibragem["projeto"], "meu-projeto")
        self.assertEqual(calibragem["arquivos_analisados"], 2)

    def test_a_base_e_vazia_o_que_ja_estava_na_main_tambem_entra(self):
        o = self.com_a_main_calibravel()
        with McpFalso() as mcp:
            calibragem = o.calibrar(mcp.url)
        self.assertEqual(sorted(calibragem["nao_espelhadas"]), ["S1135", "S3776"])
        self.assertEqual(calibragem["nao_espelhadas"]["S1135"]["exemplo"], "src/a.ts:2")

    def test_regra_que_o_servidor_tem_noutro_lugar_e_divergente_e_nao_vira_excecao(self):
        o = self.com_a_main_calibravel()
        with McpFalso() as mcp:
            mcp.do_servidor = [do_servidor("src/a.ts", 30, "S1135")]
            calibragem = o.calibrar(mcp.url)
        self.assertEqual(calibragem["divergentes"], {"S1135": 1})
        self.assertNotIn("S1135", calibragem["nao_espelhadas"])

    def test_pergunta_ao_servidor_so_os_abertos_do_projeto_e_pagina(self):
        o = self.com_a_main_calibravel()
        with McpFalso() as mcp, mock.patch.object(sonar_local, "PAGINA_ISSUES", 1):
            mcp.do_servidor = [
                do_servidor("src/x.ts", 1, "S1"),
                do_servidor("src/y.ts", 1, "S2", "CONFIRMED"),
                do_servidor("src/a.ts", 2, "S1135"),
                do_servidor("src/z.ts", 1, "S3", "FIXED"),
            ]
            calibragem = o.calibrar(mcp.url)
            self.assertEqual(calibragem["problemas_do_servidor"], 3)
            self.assertEqual(calibragem["confiaveis"], {"S1135": 1})
            self.assertEqual(len(mcp.buscas), 3)
            for busca in mcp.buscas:
                self.assertEqual(busca["projectKeys"], ["meu-projeto"])
                self.assertEqual(busca["issueStatuses"], ["OPEN", "CONFIRMED"])

    def test_somente_limita_os_arquivos(self):
        o = self.com_a_main_calibravel()
        with McpFalso() as mcp:
            calibragem = o.calibrar(mcp.url, somente=("src/f",))
        self.assertEqual(calibragem["arquivos_analisados"], 1)
        self.assertEqual(sorted(calibragem["nao_espelhadas"]), ["S3776"])

    def test_pela_linha_de_comando_o_json_sai_no_stdout_e_o_canario_nao(self):
        o = self.com_a_main_calibravel()
        with McpFalso() as mcp:
            mcp.do_servidor = [do_servidor("src/a.ts", 2, "S1135")]
            processo = o.rodar(mcp.url, "--calibrar")
            arquivo = Path(o.tmp.name) / "calibragem.json"
            gravado = o.rodar(mcp.url, "--calibrar", "--saida", str(arquivo))
        self.assertEqual(processo.returncode, 0, processo.stderr)
        self.assertEqual(json.loads(processo.stdout)["nao_espelhadas"]["S3776"]["ocorrencias"], 1)
        self.assertEqual(json.loads(arquivo.read_text()), json.loads(processo.stdout))
        self.assertEqual(gravado.stdout, "")
        self.assertNotIn(CANARIO, processo.stdout + processo.stderr)

    def calibragem_de(self, *regras: str) -> str:
        return json.dumps(
            {"nao_espelhadas": {r: {"ocorrencias": 2, "exemplo": "a:1"} for r in regras}}
        )

    def test_regra_listada_nao_reprova_e_vira_aviso_so_a_listada(self):
        o = self.oficina()
        o.escrever(sonar_local.CALIBRACAO, self.calibragem_de("S1135"))
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        o.escrever("src/f.ts", COMPLEXA)
        with McpFalso() as mcp:
            resultado = o.medir(mcp.url)
            processo = o.rodar(mcp.url)
        self.assertEqual(linhas_dos_novos(resultado), [("src/f.ts", 1, "typescript:S3776")])
        self.assertEqual(resultado.aviso(), "aviso, o CI decide: S1135 \u00d71")
        self.assertEqual(processo.returncode, 1, processo.stderr)
        self.assertIn("sonar-local: aviso, o CI decide: S1135 \u00d71", processo.stdout)
        self.assertNotIn("S1135 Complete", processo.stdout)

    def test_so_a_regra_listada_o_codigo_sai_0(self):
        o = self.oficina()
        o.escrever(sonar_local.CALIBRACAO, self.calibragem_de("S1135"))
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        with McpFalso() as mcp:
            processo = o.rodar(mcp.url)
        self.assertEqual(processo.returncode, 0, processo.stderr)

    def test_sem_o_arquivo_a_mesma_regra_reprova(self):
        o = self.oficina()
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        with McpFalso() as mcp:
            self.assertEqual(o.medir(mcp.url).codigo(), 1)

    def test_o_hook_nao_devolve_ao_agente_a_regra_listada(self):
        o = self.oficina()
        o.escrever(sonar_local.CALIBRACAO, self.calibragem_de("S1135"))
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        entrada = json.dumps({"tool_input": {"file_path": str(o.raiz / "src/a.ts")}})
        with McpFalso() as mcp:
            processo = o.rodar(mcp.url, "--hook", entrada=entrada)
            self.assertNotEqual(mcp.chamadas, [])
        self.assertEqual((processo.returncode, processo.stdout, processo.stderr), (0, "", ""))

    def test_arquivo_ilegivel_nao_vira_isencao(self):
        o = self.oficina()
        o.escrever(sonar_local.CALIBRACAO, "{nao e json")
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        with McpFalso() as mcp:
            processo = o.rodar(mcp.url)
        self.assertEqual(processo.returncode, 2, processo.stdout)
        self.assertIn(".sonar-local.json ilegivel", processo.stderr)


class Padroes(unittest.TestCase):
    def test_classificacao_pelos_parametros_do_basalto(self):
        p = sonar_local.padroes(
            {
                "sonar.sources": "backend/crates,frontend/src",
                "sonar.tests": "backend/crates",
                "sonar.exclusions": "frontend/dist/**,backend/target/**,backend/crates/*/tests/**",
                "sonar.test.inclusions": "**/*.test.tsx,**/*.test.ts,backend/crates/*/tests/**",
            }
        )
        c = sonar_local.classificar
        self.assertEqual(c("frontend/src/a.tsx", p), "MAIN")
        self.assertEqual(c("frontend/src/a.test.tsx", p), "TEST")
        self.assertEqual(c("frontend/src/pasta/b.test.ts", p), "TEST")
        self.assertEqual(c("backend/crates/x/src/lib.rs", p), "MAIN")
        self.assertEqual(c("backend/crates/x/tests/t.rs", p), "TEST")
        self.assertIsNone(c("frontend/dist/a.js", p))
        self.assertIsNone(c("e2e/steps/a.ts", p))
        self.assertIsNone(c("README.md", p))

    def test_sem_sources_tudo_entra(self):
        p = sonar_local.padroes({})
        self.assertEqual(sonar_local.classificar("qualquer/coisa.ts", p), "MAIN")

    def test_glob_do_ant(self):
        casa = sonar_local._casa
        self.assertTrue(casa("a/b/c.ts", ["**/*.ts"]))
        self.assertTrue(casa("c.ts", ["**/*.ts"]))
        self.assertFalse(casa("a/b/c.ts", ["*.ts"]))
        self.assertTrue(casa("src/x/y.ts", ["src/**"]))
        self.assertTrue(casa("src/x/y.ts", ["src/"]))
        self.assertFalse(casa("srcx/y.ts", ["src/**"]))
        self.assertTrue(casa("a/b.ts", ["a/?.ts"]))

    def test_linguagens(self):
        ling = sonar_local.linguagem_de
        self.assertEqual(ling("a/b.tsx"), "tsx")
        self.assertEqual(ling("a/Dockerfile"), "docker")
        self.assertEqual(ling("a/b.py"), "py")
        self.assertIsNone(ling("a/b.rs"))
        self.assertIsNone(ling("a/b.md"))


class Falhas(Base):
    def saida(self, processo: subprocess.CompletedProcess) -> str:
        return processo.stdout + processo.stderr

    def com_problema(self) -> Oficina:
        o = self.oficina()
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        return o

    def test_problema_novo_sai_1_com_arquivo_linha_regra_e_resumo(self):
        o = self.com_problema()
        with McpFalso() as mcp:
            processo = o.rodar(mcp.url)
        self.assertEqual(processo.returncode, 1, self.saida(processo))
        linhas = processo.stdout.splitlines()
        self.assertEqual(
            linhas[0],
            'src/a.ts:1 typescript:S1135 Complete the task associated to this "TODO" comment.',
        )
        self.assertEqual(linhas[1], "sonar-local: 1 problema novo em 1 arquivo (1 analisado)")
        self.assertNotIn(CANARIO, self.saida(processo))

    def test_limpo_sai_0(self):
        o = self.oficina()
        o.escrever("src/a.ts", BASE_A.replace("return 1", "return 2"))
        with McpFalso() as mcp:
            processo = o.rodar(mcp.url)
        self.assertEqual(processo.returncode, 0, self.saida(processo))
        self.assertEqual(processo.stdout.strip(), "sonar-local: 0 problemas novos em 1 arquivo")

    def test_mcp_fora_sai_2_e_diz_como_subir(self):
        o = self.com_problema()
        processo = o.rodar(porta_fechada())
        self.assertEqual(processo.returncode, 2, self.saida(processo))
        self.assertIn("nao consegui falar com o MCP", processo.stderr)
        self.assertIn("docker start sonarqube-homelab-mcp", processo.stderr)
        self.assertNotIn(CANARIO, self.saida(processo))

    def test_http_500_e_401_saem_2_sem_o_canario(self):
        o = self.com_problema()
        for modo, esperado in (("500", "HTTP 500"), ("401", "HTTP 401")):
            with self.subTest(modo), McpFalso(modo) as mcp:
                processo = o.rodar(mcp.url)
                self.assertEqual(processo.returncode, 2, self.saida(processo))
                self.assertIn(esperado, processo.stderr)
                self.assertNotIn(CANARIO, self.saida(processo))

    def test_erro_do_servidor_com_o_canario_no_texto_sai_limpo(self):
        o = self.com_problema()
        with McpFalso("rpc") as mcp:
            processo = o.rodar(mcp.url)
        self.assertEqual(processo.returncode, 2, self.saida(processo))
        self.assertIn("nao deu, o token era ***", processo.stderr)
        self.assertNotIn(CANARIO, self.saida(processo))

    def test_redirecionamento_nao_e_seguido_e_o_token_nao_vai_junto(self):
        o = self.com_problema()
        with McpFalso() as outro, McpFalso("redirect", outro.url) as mcp:
            processo = o.rodar(mcp.url)
            self.assertEqual(outro.autorizacoes, [])
        self.assertEqual(processo.returncode, 2, self.saida(processo))
        self.assertIn("HTTP 302", processo.stderr)

    def test_helper_sem_token_sai_2(self):
        o = self.com_problema()
        o.helper = o.criar_helper("nada", 3)
        with McpFalso() as mcp:
            processo = o.rodar(mcp.url)
            self.assertEqual(mcp.autorizacoes, [])
        self.assertEqual(processo.returncode, 2, self.saida(processo))
        self.assertIn("saiu com 3", processo.stderr)

    def test_url_fora_do_loopback_nao_recebe_o_token(self):
        o = self.com_problema()
        processo = o.rodar("http://exemplo.invalid/mcp")
        self.assertEqual(processo.returncode, 2, self.saida(processo))
        self.assertIn("127.0.0.1", processo.stderr)

    def test_resposta_em_sse_com_sessao(self):
        o = self.com_problema()
        with McpFalso("sse") as mcp:
            processo = o.rodar(mcp.url)
        self.assertEqual(processo.returncode, 1, self.saida(processo))
        self.assertIn("src/a.ts:1 typescript:S1135", processo.stdout)


class Hook(Base):
    def entrada(self, o: Oficina, caminho: str) -> str:
        return json.dumps({"tool_input": {"file_path": str(o.raiz / caminho)}})

    def test_problema_novo_sai_2_com_a_lista_no_stderr(self):
        o = self.oficina()
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        with McpFalso() as mcp:
            processo = o.rodar(mcp.url, "--hook", entrada=self.entrada(o, "src/a.ts"))
        self.assertEqual(processo.returncode, 2, processo.stderr)
        self.assertIn("src/a.ts:1 typescript:S1135", processo.stderr)
        self.assertEqual(processo.stdout, "")
        self.assertNotIn(CANARIO, processo.stderr)

    def test_sem_problema_novo_sai_0_em_silencio(self):
        o = self.oficina()
        o.escrever("src/a.ts", "// cabecalho\n" + BASE_A)
        with McpFalso() as mcp:
            processo = o.rodar(mcp.url, "--hook", entrada=self.entrada(o, "src/a.ts"))
        self.assertEqual((processo.returncode, processo.stdout, processo.stderr), (0, "", ""))

    def test_arquivo_fora_do_sonar_sai_0_sem_chamar_o_mcp(self):
        o = self.oficina()
        o.escrever("src/gerado/x.ts", "// TODO gerado\n")
        o.escrever("README.md", "TODO\n")
        with McpFalso() as mcp:
            for caminho in ("src/gerado/x.ts", "README.md"):
                processo = o.rodar(mcp.url, "--hook", entrada=self.entrada(o, caminho))
                self.assertEqual((processo.returncode, processo.stderr), (0, ""), caminho)
            self.assertEqual(mcp.chamadas, [])

    def test_mcp_fora_sai_0_em_silencio(self):
        o = self.oficina()
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        processo = o.rodar(porta_fechada(), "--hook", entrada=self.entrada(o, "src/a.ts"))
        self.assertEqual((processo.returncode, processo.stdout, processo.stderr), (0, "", ""))

    def test_entrada_que_nao_e_do_hook_sai_0(self):
        o = self.oficina()
        for entrada in ("", "nao e json", "{}", '{"tool_input":{}}'):
            processo = o.rodar(porta_fechada(), "--hook", entrada=entrada)
            self.assertEqual((processo.returncode, processo.stderr), (0, ""), entrada)

    def test_o_prazo_do_hook_corta_o_servidor_lento(self):
        o = self.oficina()
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        with (
            McpFalso("lento") as mcp,
            mock.patch.object(sonar_local, "PRAZO_HOOK_S", 1.0),
            mock.patch.dict(os.environ, o.ambiente(mcp.url)),
        ):
            inicio = time.monotonic()
            codigo = sonar_local.main(
                ["--hook", "--repo", str(o.raiz)], io.StringIO(self.entrada(o, "src/a.ts"))
            )
            gasto = time.monotonic() - inicio
        self.assertEqual(codigo, 0)
        self.assertLess(gasto, 4)

    def test_o_alarme_do_hook_corta_o_que_nao_e_chamada_ao_mcp(self):
        o = self.oficina()
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        with (
            mock.patch.object(sonar_local, "PRAZO_HOOK_S", 1.0),
            mock.patch.object(sonar_local, "descobrir", side_effect=dorme),
        ):
            inicio = time.monotonic()
            codigo = sonar_local.main(
                ["--hook", "--repo", str(o.raiz)], io.StringIO(self.entrada(o, "src/a.ts"))
            )
            gasto = time.monotonic() - inicio
        self.assertEqual(codigo, 0)
        self.assertLess(gasto, 3)


SEM_SONAR = """\
name: ci
on: pull_request
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: echo oi
"""
_ferramenta_de_verdade = validar_local.ferramenta


def ferramenta_falsa(nome: str):
    """O yq e de verdade (o sonar_local o usa); o actionlint nao roda aqui."""
    return _ferramenta_de_verdade(nome) if nome == "yq" else Path("/nada")


# Sem `sonar.sources`: tudo e fonte, e e o que deixaria um caminho de fora passar.
WORKFLOW_TUDO = WORKFLOW.replace("        sonar.sources=src\n", "").replace(
    "        sonar.tests=src\n", ""
)


class Confinamento(Base):
    """O que chega de fora (o JSON do hook e a linha de comando) fica no repositorio."""

    def hook(self, o: Oficina, url: str, caminho: Path) -> subprocess.CompletedProcess:
        entrada = json.dumps({"tool_input": {"file_path": str(caminho)}})
        return o.rodar(url, "--hook", entrada=entrada)

    def fora(self, o: Oficina) -> Path:
        arquivo = Path(o.tmp.name) / "fora" / "x.ts"
        arquivo.parent.mkdir()
        arquivo.write_text("// TODO de fora do repositorio\n")
        return arquivo

    def test_o_hook_nao_le_arquivo_fora_do_repositorio(self):
        o = self.oficina(WORKFLOW_TUDO)
        fora = self.fora(o)
        with McpFalso() as mcp:
            processo = self.hook(o, mcp.url, fora)
            traversal = self.hook(o, mcp.url, o.raiz / "src" / ".." / ".." / "fora" / "x.ts")
            self.assertEqual(mcp.chamadas, [])
        self.assertEqual((processo.returncode, processo.stderr), (0, ""))
        self.assertEqual((traversal.returncode, traversal.stderr), (0, ""))

    def test_o_hook_nao_segue_link_que_aponta_para_fora(self):
        o = self.oficina(WORKFLOW_TUDO)
        (o.raiz / "src" / "link.ts").symlink_to(self.fora(o))
        with McpFalso() as mcp:
            processo = self.hook(o, mcp.url, o.raiz / "src" / "link.ts")
            self.assertEqual(mcp.chamadas, [])
        self.assertEqual((processo.returncode, processo.stderr), (0, ""))

    def test_o_hook_ainda_le_o_arquivo_de_dentro(self):
        o = self.oficina(WORKFLOW_TUDO)
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        with McpFalso() as mcp:
            processo = self.hook(o, mcp.url, o.raiz / "src" / "a.ts")
        self.assertEqual(processo.returncode, 2, processo.stderr)

    def test_saida_so_no_repositorio_ou_no_diretorio_temporario(self):
        o = self.oficina()
        for destino in ("/usr/share/sonar-local-teste.json", "../../../../usr/share/x.json"):
            with self.subTest(destino), McpFalso() as mcp:
                processo = o.rodar(mcp.url, "--calibrar", "--saida", destino)
                self.assertEqual(processo.returncode, 2, processo.stdout)
                self.assertIn("--saida fica dentro do repositorio", processo.stderr)
                self.assertEqual(mcp.chamadas, [])
        self.assertFalse(Path("/usr/share/sonar-local-teste.json").exists())

    def test_saida_por_link_que_sai_do_diretorio_temporario_tambem_e_recusada(self):
        o = self.oficina()
        link = Path(o.tmp.name) / "link"
        link.symlink_to("/usr/share")
        with McpFalso() as mcp:
            processo = o.rodar(mcp.url, "--calibrar", "--saida", str(link / "x.json"))
        self.assertEqual(processo.returncode, 2, processo.stdout)
        self.assertIn("--saida fica dentro do repositorio", processo.stderr)

    def test_saida_dentro_do_repositorio_e_aceita(self):
        o = self.oficina()
        with McpFalso() as mcp:
            processo = o.rodar(mcp.url, "--calibrar", "--saida", str(o.raiz / "cal.json"))
        self.assertEqual(processo.returncode, 0, processo.stderr)
        self.assertIn("nao_espelhadas", (o.raiz / "cal.json").read_text())

    def test_somente_base_e_projeto_so_entram_no_formato_esperado(self):
        o = self.oficina()
        casos = (
            (("--calibrar", "--somente", "../fora"), "--somente espera"),
            (("--calibrar", "--somente", "/etc"), "--somente espera"),
            (("--calibrar", "--somente", "src/../../fora"), "--somente espera"),
            (("--base=--upload-pack=x",), "--base espera"),
            (("--projeto", "a b;c"), "--projeto espera"),
        )
        with McpFalso() as mcp:
            for argumentos, mensagem in casos:
                with self.subTest(argumentos):
                    processo = o.rodar(mcp.url, *argumentos)
                    self.assertEqual(processo.returncode, 2, processo.stdout)
                    self.assertIn(mensagem, processo.stderr)
            self.assertEqual(mcp.chamadas, [])


class ProjetoAutomatico(Base):
    """Sem `sonar_projeto` e publico: `<dono>_<repo>`, se o SonarCloud o confirma."""

    DISPENSADO = WORKFLOW.replace(
        "      sonar_projeto: meu-projeto\n",
        '      sonar_projeto: ""\n      sonar_dispensa: "analise automatica do SonarCloud"\n',
    )

    def medir(self, o: Oficina, mcp: McpFalso, visibilidade: str):
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        return o.rodar(mcp.url, visibilidade=visibilidade)

    def test_publico_com_o_projeto_no_sonarcloud_e_medido(self):
        o = self.oficina(self.DISPENSADO)
        with McpFalso() as mcp:
            mcp.projetos_cloud = ["exemplo_demo"]
            processo = self.medir(o, mcp, "publico")
            self.assertEqual({c["projectKey"] for c in mcp.chamadas}, {"exemplo_demo"})
        self.assertEqual(processo.returncode, 1, processo.stderr)
        self.assertIn("src/a.ts:1 typescript:S1135", processo.stdout)

    def test_publico_sem_o_projeto_no_sonarcloud_nao_ha_o_que_medir(self):
        o = self.oficina(self.DISPENSADO)
        with McpFalso() as mcp:
            processo = self.medir(o, mcp, "publico")
            self.assertEqual(mcp.consultas, [{"q": "exemplo_demo"}])
            self.assertEqual(mcp.chamadas, [])
        self.assertEqual(processo.returncode, 0, processo.stderr)
        self.assertIn("nao tem projeto Sonar", processo.stdout)

    def test_privado_nem_pergunta_ao_sonarcloud(self):
        o = self.oficina(self.DISPENSADO)
        with McpFalso() as mcp:
            mcp.projetos_cloud = ["exemplo_demo"]
            processo = self.medir(o, mcp, "privado")
            self.assertEqual(mcp.consultas, [])
        self.assertEqual(processo.returncode, 0, processo.stderr)

    def test_a_chave_do_workflow_vence_a_automatica(self):
        o = self.oficina()
        with McpFalso() as mcp:
            mcp.projetos_cloud = ["exemplo_demo"]
            self.medir(o, mcp, "publico")
            self.assertEqual(mcp.consultas, [])
            self.assertEqual({c["projectKey"] for c in mcp.chamadas}, {"meu-projeto"})


class NotaDoPython(Base):
    def test_arquivo_python_diz_que_a_analise_de_fluxo_so_o_ci_mede(self):
        o = self.oficina(WORKFLOW_TUDO)
        o.escrever("src/m.py", "x = 1\n")
        with McpFalso() as mcp:
            resultado = o.medir(mcp.url)
        self.assertIn(
            "so o CI mede, em Python, a analise de fluxo de seguranca", resultado.resumo()
        )
        self.assertEqual({c["language"] for c in mcp.chamadas}, {"py"})

    def test_sem_python_a_nota_nao_aparece(self):
        o = self.oficina()
        o.escrever("src/a.ts", BASE_A.replace("return 1", "return 2"))
        with McpFalso() as mcp:
            self.assertNotIn("pythonsecurity", o.medir(mcp.url).resumo())


class ValidarLocal(Base):
    def rodada(
        self,
        o: Oficina,
        url: str,
        motivo: str | None = None,
        workflows_ok: bool = True,
        filtro: bool = False,
        **opcoes,
    ):
        """A `validar` inteira, com o act trocado por um falso que conta as chamadas."""
        args = argparse.Namespace(
            job="ci" if filtro else None,
            workflow=None,
            listar=False,
            esteira_local=False,
            sem_sonar_local=motivo,
            **opcoes,
        )
        arvore = o.git("rev-parse", "HEAD^{tree}")
        self.act = mock.Mock(
            return_value={"ci.yml": {"job": "passou" if workflows_ok else "falhou"}}
        )
        saida = io.StringIO()
        with (
            mock.patch.dict(os.environ, o.ambiente(url)),
            contextlib.redirect_stdout(saida),
            mock.patch.object(validar_local, "maquina_pronta", return_value=None),
            mock.patch.object(validar_local, "travar_a_maquina", return_value=object()),
            mock.patch.object(validar_local, "limpar_rodadas_do_act", return_value=0),
            mock.patch.object(validar_local, "preparar_esteira", return_value=(o.raiz, 1)),
            mock.patch.object(validar_local, "segredos", return_value=({}, {})),
            mock.patch.object(validar_local, "ferramenta", ferramenta_falsa),
            mock.patch.object(validar_local, "lint", return_value=True),
            mock.patch.object(validar_local, "rodar_workflows", self.act),
        ):
            codigo = validar_local.validar(args, o.raiz, arvore, "")
        registrado = (validar_local.registro(o.raiz) / arvore).is_file()
        # Sob o act a saida e um terminal, e o validar_local colore o que diz.
        texto = re.sub(r"\x1b\[[0-9;]*m", "", saida.getvalue())
        self.assertNotIn(CANARIO, texto)
        return codigo, registrado, texto, arvore

    def com_problema(self) -> Oficina:
        o = self.oficina()
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        o.commitar("problema")
        return o

    def test_com_problema_novo_a_rodada_fica_vermelha_e_nao_registra(self):
        o = self.com_problema()
        with McpFalso() as mcp:
            codigo, registrado, texto, _ = self.rodada(o, mcp.url)
        self.assertEqual((codigo, registrado), (1, False), texto)
        self.assertIn("src/a.ts:1 typescript:S1135", texto)
        self.assertIn("REPROVADO antes dos workflows", texto)
        self.assertIn("sonar_local.py` ate sair 0", texto)
        self.act.assert_not_called()

    def test_limpo_registra_e_diz_o_que_foi_medido_e_o_que_so_o_ci_mede(self):
        o = self.oficina()
        o.escrever("src/a.ts", BASE_A.replace("return 1", "return 2"))
        o.escrever("src/m.rs", "fn main() {}\n")
        o.commitar("limpo")
        with McpFalso() as mcp:
            codigo, registrado, texto, arvore = self.rodada(o, mcp.url)
        self.assertEqual((codigo, registrado), (0, True), texto)
        self.act.assert_called_once()
        self.assertEqual(len(mcp.chamadas), 2, "o Sonar local e medido uma vez so")
        self.assertIn("Sonar local: 0 problemas novos em 1 arquivo; 1 so o CI mede (Rust 1)", texto)
        self.assertLess(texto.index("Sonar local:"), texto.index("Resumo"))
        self.assertIn("So o CI mede: Rust, cobertura do codigo novo e duplicacao", texto)
        self.assertNotIn("o Sonar so o CI mede", texto)
        self.assertIn("Sonar local 0 novos", (validar_local.registro(o.raiz) / arvore).read_text())

    def test_sem_conseguir_medir_fica_vermelha_com_a_instrucao(self):
        o = self.com_problema()
        codigo, registrado, texto, _ = self.rodada(o, porta_fechada())
        self.assertEqual((codigo, registrado), (1, False), texto)
        self.assertIn("nao consegui medir", texto)
        self.assertIn("docker start sonarqube-homelab-mcp", texto)
        self.assertIn("--sem-sonar-local", texto)
        self.act.assert_not_called()

    def test_sem_sonar_local_com_motivo_registra_e_o_motivo_aparece(self):
        o = self.com_problema()
        codigo, registrado, texto, arvore = self.rodada(
            o, porta_fechada(), motivo="MCP da Sonar em manutencao"
        )
        self.assertEqual((codigo, registrado), (0, True), texto)
        self.act.assert_called_once()
        self.assertIn("DISPENSADO", texto)
        self.assertIn("MCP da Sonar em manutencao", texto)
        registro = (validar_local.registro(o.raiz) / arvore).read_text()
        self.assertIn("Sonar local dispensado (MCP da Sonar em manutencao)", registro)

    def test_sem_sonar_local_nao_perdoa_problema_novo(self):
        o = self.com_problema()
        with McpFalso() as mcp:
            codigo, registrado, texto, _ = self.rodada(o, mcp.url, motivo="quero passar")
        self.assertEqual((codigo, registrado), (1, False), texto)
        self.act.assert_not_called()

    def test_o_aviso_das_regras_calibradas_aparece_e_a_rodada_passa(self):
        o = self.oficina()
        o.escrever(
            sonar_local.CALIBRACAO, json.dumps({"nao_espelhadas": {"S1135": {"ocorrencias": 1}}})
        )
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        o.commitar("calibrado")
        with McpFalso() as mcp:
            codigo, registrado, texto, _ = self.rodada(o, mcp.url)
        self.assertEqual((codigo, registrado), (0, True), texto)
        self.assertIn("aviso, o CI decide: S1135 \u00d71", texto)

    def test_workflow_vermelho_nao_registra_mesmo_com_o_sonar_limpo(self):
        o = self.oficina()
        o.escrever("src/a.ts", BASE_A.replace("return 1", "return 2"))
        o.commitar("limpo")
        with McpFalso() as mcp:
            codigo, registrado, texto, _ = self.rodada(o, mcp.url, workflows_ok=False)
        self.assertEqual((codigo, registrado), (1, False), texto)
        self.act.assert_called_once()

    def test_com_filtro_o_sonar_local_roda_primeiro_do_mesmo_jeito(self):
        o = self.com_problema()
        with McpFalso() as mcp:
            codigo, registrado, texto, _ = self.rodada(o, mcp.url, filtro=True)
        self.assertEqual((codigo, registrado), (1, False), texto)
        self.act.assert_not_called()

    def test_repositorio_sem_projeto_sonar_registra(self):
        o = self.oficina(workflow=SEM_SONAR)
        o.escrever("src/a.ts", "// TODO\n")
        o.commitar("sem sonar")
        codigo, registrado, texto, _ = self.rodada(o, porta_fechada())
        self.assertEqual((codigo, registrado), (0, True), texto)
        self.assertIn("nao tem projeto Sonar", texto)

    def test_o_motivo_nao_pode_ser_vazio(self):
        args = argparse.Namespace(workflow=None, job=None, sem_sonar_local="  ")
        self.assertIn("motivo", validar_local.argumento_invalido(args))


# ------------------------------------------------------------------ iscas


def _s(arquivo: str, antes: str, depois: str, teste: str, nome: str, extra: tuple = ()):
    """Uma isca: a troca principal e, se a regra tem uma segunda barreira que
    esconderia a falha, as trocas que a desligam tambem."""
    return (nome, [(arquivo, antes, depois), *extra], teste)


ISCAS = [
    _s(
        "sonar_local.py",
        'return (self.regra, re.sub(r"\\d+", "#", self.mensagem), self.texto)',
        'return (self.regra, re.sub(r"\\d+", "#", self.mensagem), self.texto, self.linha)',
        "Novos.test_linha_deslocada_nao_e_nova",
        "a chave do problema inclui a linha",
    ),
    _s(
        "sonar_local.py",
        'return (self.regra, re.sub(r"\\d+", "#", self.mensagem), self.texto)',
        "return (self.regra, self.mensagem, self.texto)",
        "Novos.test_funcao_ja_acima_do_limite_que_cresce_nao_e_nova",
        "os numeros da mensagem entram na chave",
    ),
    _s(
        "sonar_local.py",
        "candidatas.remove(a_mais_proxima(candidatas, atual.linha))",
        "pass",
        "Novos.test_o_mesmo_problema_de_novo_e_novo",
        "um problema da base casa com varios",
    ),
    _s(
        "sonar_local.py",
        "if conteudo_base == conteudo:",
        "if True:",
        "Novos.test_complexidade_que_cresceu_sem_mudar_o_cabecalho_e_nova",
        "a base e a versao atual (tudo e antigo)",
    ),
    _s(
        "sonar_local.py",
        '"-M", base)',
        '"-M", "HEAD")',
        "Novos.test_os_tres_lugares_do_pr",
        "o diff e contra o HEAD, e nao contra o merge-base",
    ),
    _s(
        "sonar_local.py",
        '"ls-files", "--others", "--exclude-standard", "-z")',
        '"ls-files", "--deleted", "--exclude-standard", "-z")',
        "Novos.test_os_tres_lugares_do_pr",
        "os nao rastreados ficam de fora",
    ),
    _s(
        "sonar_local.py",
        'return None if _casa(caminho, p.exclusoes_teste) else "TEST"',
        'return None if _casa(caminho, p.exclusoes_teste) else "MAIN"',
        "Novos.test_arquivo_de_teste_vai_com_scope_test",
        "arquivo de teste vai como MAIN",
    ),
    _s(
        "sonar_local.py",
        "if em_fontes and not _casa(caminho, p.exclusoes):",
        "if em_fontes:",
        "Novos.test_exclusions_tira_o_arquivo",
        "sonar.exclusions e ignorado",
    ),
    _s(
        "sonar_local.py",
        "if em_fontes and not _casa(caminho, p.exclusoes):",
        "if em_fontes:",
        "Hook.test_arquivo_fora_do_sonar_sai_0_sem_chamar_o_mcp",
        "o hook analisa arquivo excluido",
    ),
    _s(
        "sonar_local.py",
        '".rs": "Rust",',
        "",
        "Novos.test_rust_conta_em_so_o_ci",
        "Rust some da contagem em silencio",
    ),
    _s(
        "sonar_local.py",
        'return "homelab" if decide(dono_e_repo(repo)) else "cloud"',
        'return "cloud" if decide(dono_e_repo(repo)) else "homelab"',
        "Novos.test_a_visibilidade_do_repositorio_escolhe_o_servidor",
        "privado vai para a Cloud",
    ),
    _s(
        "sonar_local.py",
        "    if projeto is None or not projeto.chave:\n        return Resultado(",
        "    if projeto is None:\n        return Resultado(",
        "Novos.test_sem_projeto_sonar_nao_chama_o_mcp",
        "projeto dispensado (chave vazia) e medido",
    ),
    _s(
        "sonar_local.py",
        'if "workflow_call" in json.dumps(documento.get("on", documento.get("true"))):',
        "if False:",
        "Novos.test_reutilizavel_que_so_repassa_inputs_nao_e_um_chamador",
        "o reutilizavel da esteira conta como chamador",
    ),
    _s(
        "sonar_local.py",
        'raise NaoMediu(f"nao consegui falar com o MCP em {self.url}") from None',
        "return {}",
        "Falhas.test_mcp_fora_sai_2_e_diz_como_subir",
        "MCP fora vira 'limpo'",
    ),
    _s(
        "sonar_local.py",
        'raise NaoMediu(f"o MCP respondeu HTTP {erro.code}{dica}") from None',
        'raise NaoMediu(f"o MCP respondeu HTTP {erro.code}{dica} {self.cabecalhos}") from None',
        "Falhas.test_http_500_e_401_saem_2_sem_o_canario",
        "o erro HTTP conta os cabecalhos (e `limpar` nao os cobre)",
        extra=(
            (
                "sonar_local.py",
                "    for segredo in _SEGREDOS:\n",
                "    for segredo in ():\n",
            ),
        ),
    ),
    _s(
        "sonar_local.py",
        '    for segredo in _SEGREDOS:\n        texto = texto.replace(segredo, "***")\n',
        '    for segredo in ():\n        texto = texto.replace(segredo, "***")\n',
        "Falhas.test_erro_do_servidor_com_o_canario_no_texto_sai_limpo",
        "a saida nao passa por `limpar`",
    ),
    _s(
        "sonar_local.py",
        "self.abre = urllib.request.build_opener(_SemRedirecionar)",
        "self.abre = urllib.request.build_opener()",
        "Falhas.test_redirecionamento_nao_e_seguido_e_o_token_nao_vai_junto",
        "o redirecionamento e seguido, com o token",
    ),
    _s(
        "sonar_local.py",
        "if urllib.parse.urlparse(self.url).hostname not in LOOPBACK:",
        "if False:",
        "Falhas.test_url_fora_do_loopback_nao_recebe_o_token",
        "o token vai para qualquer URL",
    ),
    _s(
        "sonar_local.py",
        'self.sessao = resposta.headers.get("Mcp-Session-Id") or self.sessao',
        "pass",
        "Falhas.test_resposta_em_sse_com_sessao",
        "a sessao do MCP e esquecida",
    ),
    _s(
        "sonar_local.py",
        "    for linha in resultado.linhas():\n        diz(linha, sys.stderr)\n    return 2\n",
        "    for linha in resultado.linhas():\n        diz(linha, sys.stderr)\n    return 0\n",
        "Hook.test_problema_novo_sai_2_com_a_lista_no_stderr",
        "o hook nao devolve o problema ao agente",
    ),
    _s(
        "sonar_local.py",
        "    except Exception:  # noqa: BLE001 - o hook nunca atrapalha a edicao\n"
        "        return 0\n",
        "    except Exception:  # noqa: BLE001 - o hook nunca atrapalha a edicao\n"
        "        return 2\n",
        "Hook.test_mcp_fora_sai_0_em_silencio",
        "o hook trava a edicao quando o MCP cai",
    ),
    _s(
        "sonar_local.py",
        "arquivo = repo / CALIBRACAO",
        'arquivo = repo / "outro.json"',
        "Calibracao.test_regra_listada_nao_reprova_e_vira_aviso_so_a_listada",
        "o .sonar-local.json nao e lido",
    ),
    _s(
        "sonar_local.py",
        "if regra in ignoradas:",
        "if regra in set():",
        "Calibracao.test_o_hook_nao_devolve_ao_agente_a_regra_listada",
        "o hook devolve a regra calibrada ao agente",
    ),
    _s(
        "sonar_local.py",
        "if regra in ignoradas:",
        "if True:",
        "Calibracao.test_regra_listada_nao_reprova_e_vira_aviso_so_a_listada",
        "toda regra vira aviso, e nao so a listada",
    ),
    _s(
        "sonar_local.py",
        "        raise NaoMediu(\n"
        '            f"{CALIBRACAO} ilegivel: precisa de `nao_espelhadas` (rode --calibrar)"\n'
        "        ) from None",
        "        return set()",
        "Calibracao.test_arquivo_ilegivel_nao_vira_isencao",
        "arquivo ilegivel e lido como 'sem calibragem'",
    ),
    _s(
        "sonar_local.py",
        "            confiaveis[regra] += 1\n        elif regra in regras_do_servidor:",
        "            ocorrencias[regra] += 1\n"
        "            exemplos.setdefault(regra, caminho)\n"
        "        elif regra in regras_do_servidor:",
        "Calibracao.test_regra_que_o_servidor_nao_tem_e_nao_espelhada_e_a_que_bate_e_confiavel",
        "o que bate com o servidor tambem vira nao espelhada",
    ),
    _s(
        "sonar_local.py",
        "        elif regra in regras_do_servidor:\n            divergentes[regra] += 1",
        "        elif False:\n            divergentes[regra] += 1",
        "Calibracao.test_regra_que_o_servidor_tem_noutro_lugar_e_divergente_e_nao_vira_excecao",
        "a regra que o servidor tem noutro lugar vira excecao",
    ),
    _s(
        "sonar_local.py",
        "        pagina += 1\n",
        "        break\n",
        "Calibracao.test_pergunta_ao_servidor_so_os_abertos_do_projeto_e_pagina",
        "so a primeira pagina do servidor e lida",
    ),
    _s(
        "sonar_local.py",
        'STATUS_ABERTOS = ["OPEN", "CONFIRMED"]',
        'STATUS_ABERTOS = ["OPEN"]',
        "Calibracao.test_pergunta_ao_servidor_so_os_abertos_do_projeto_e_pagina",
        "os CONFIRMED do servidor nao contam",
    ),
    _s(
        "sonar_local.py",
        "(not somente or caminho.startswith(tuple(somente)))",
        "True",
        "Calibracao.test_somente_limita_os_arquivos",
        "--somente e ignorado",
    ),
    _s(
        "sonar_local.py",
        'git(repo, "ls-tree", "-r", "-z", "--name-only", ref)',
        'git(repo, "diff", "--name-only", "-z", ref)',
        "Calibracao.test_a_base_e_vazia_o_que_ja_estava_na_main_tambem_entra",
        "a calibragem le o diff, e nao a main inteira",
    ),
    _s(
        "validar_local.py",
        '        if resultado.aviso():\n            diz(f"  {AMARELO}{resultado.aviso()}{FIM}")\n',
        "",
        "ValidarLocal.test_o_aviso_das_regras_calibradas_aparece_e_a_rodada_passa",
        "a rodada esconde o aviso do CI",
    ),
    _s(
        "sonar_local.py",
        "        if os.path.commonpath([base, real]) == base:",
        "        if True:",
        "Confinamento.test_o_hook_nao_le_arquivo_fora_do_repositorio",
        "o hook le arquivo fora do repositorio (e o diff do `medir` tambem deixa)",
        extra=(
            (
                "sonar_local.py",
                "arquivos = [par for par in arquivos if par[0] == apenas]",
                "arquivos = [(apenas, apenas)]",
            ),
        ),
    ),
    _s(
        "sonar_local.py",
        "    real = os.path.realpath(caminho)\n",
        "    real = os.path.abspath(caminho)\n",
        "Confinamento.test_o_hook_nao_segue_link_que_aponta_para_fora",
        "o hook segue link para fora (e `separar` tambem deixa)",
        extra=(
            (
                "sonar_local.py",
                "if escopo is None or arquivo.is_symlink() or",
                "if escopo is None or",
            ),
        ),
    ),
    _s(
        "sonar_local.py",
        "    if args.saida and confinar(args.saida, raizes_de_saida(repo)) is None:\n",
        "    if False:\n",
        "Confinamento.test_saida_so_no_repositorio_ou_no_diretorio_temporario",
        "--saida grava onde a pessoa mandar (e `gravar_dentro` tambem deixa)",
        extra=(
            (
                "sonar_local.py",
                "        if os.path.commonpath([pasta, alvo]) == pasta:",
                "        if True:",
            ),
        ),
    ),
    _s(
        "sonar_local.py",
        "    real = os.path.realpath(caminho)\n",
        "    real = os.path.abspath(caminho)\n",
        "Confinamento.test_saida_por_link_que_sai_do_diretorio_temporario_tambem_e_recusada",
        "o link para fora do temporario passa (e `gravar_dentro` tambem deixa)",
        extra=(
            (
                "sonar_local.py",
                "    alvo = os.path.realpath(caminho)\n",
                "    alvo = os.path.abspath(caminho)\n",
            ),
        ),
    ),
    _s(
        "sonar_local.py",
        'if os.path.isabs(normal) or normal == ".." or normal.startswith(".." + os.sep):',
        "if False:",
        "Confinamento.test_somente_base_e_projeto_so_entram_no_formato_esperado",
        "--somente aceita caminho de fora",
    ),
    _s(
        "sonar_local.py",
        "if args.base and not REF_SEGURA.fullmatch(args.base):",
        "if False:",
        "Confinamento.test_somente_base_e_projeto_so_entram_no_formato_esperado",
        "--base aceita o que o git le como opcao",
    ),
    _s(
        "sonar_local.py",
        "if args.projeto and not CHAVE_SEGURA.fullmatch(args.projeto):",
        "if False:",
        "Confinamento.test_somente_base_e_projeto_so_entram_no_formato_esperado",
        "--projeto aceita qualquer texto",
    ),
    _s(
        "sonar_local.py",
        'if not (dono and nome) or visibilidade_privada(f"{dono}/{nome}"):',
        "if not (dono and nome):",
        "ProjetoAutomatico.test_privado_nem_pergunta_ao_sonarcloud",
        "o repositorio privado procura projeto no SonarCloud",
    ),
    _s(
        "sonar_local.py",
        "if not existe_no_sonarcloud(chave, prazo or Prazo(None)):",
        "if False:",
        "ProjetoAutomatico.test_publico_sem_o_projeto_no_sonarcloud_nao_ha_o_que_medir",
        "usa a chave sem o SonarCloud confirmar que ela existe",
    ),
    _s(
        "sonar_local.py",
        "    if projeto is not None and projeto.chave:\n        return projeto\n    try:",
        "    if False:\n        return projeto\n    try:",
        "ProjetoAutomatico.test_a_chave_do_workflow_vence_a_automatica",
        "a chave automatica vence a do workflow",
    ),
    _s(
        "sonar_local.py",
        "        resultado.notas.append(NOTA_PYTHON)",
        "        pass",
        "NotaDoPython.test_arquivo_python_diz_que_a_analise_de_fluxo_so_o_ci_mede",
        "Python nao diz que o fluxo de seguranca so o CI mede",
    ),
    _s(
        "sonar_local.py",
        "    signal.alarm(max(1, int(PRAZO_HOOK_S)))\n",
        "",
        "Hook.test_o_alarme_do_hook_corta_o_que_nao_e_chamada_ao_mcp",
        "o hook espera o yq e o gh sem teto",
    ),
    _s(
        "sonar_local.py",
        "        return min(PRAZO_CHAMADA_S, restante)",
        "        return PRAZO_CHAMADA_S",
        "Hook.test_o_prazo_do_hook_corta_o_servidor_lento",
        "o teto de 25 s nao corta a chamada (e o alarme tambem nao)",
        extra=(("sonar_local.py", "    signal.alarm(max(1, int(PRAZO_HOOK_S)))\n", ""),),
    ),
    _s(
        "validar_local.py",
        "    if not sonar_ok:\n        diz(",
        "    if False:\n        diz(",
        "ValidarLocal.test_com_problema_novo_a_rodada_fica_vermelha_e_nao_registra",
        "problema novo nao para a rodada",
    ),
    _s(
        "validar_local.py",
        "    sonar_ok, feito_sonar = medir_sonar(repo, args)\n    if not sonar_ok:",
        '    sonar_ok, feito_sonar = True, ""\n    if not sonar_ok:',
        "ValidarLocal.test_com_problema_novo_a_rodada_fica_vermelha_e_nao_registra",
        "o Sonar local nao e medido antes dos workflows",
    ),
    _s(
        "validar_local.py",
        '    feito = ", ".join([*resultado, feito_sonar])',
        '    feito = ", ".join(resultado)',
        "ValidarLocal.test_limpo_registra_e_diz_o_que_foi_medido_e_o_que_so_o_ci_mede",
        "o registro esquece o que o Sonar local mediu",
    ),
    _s(
        "validar_local.py",
        "    return fechar(args, repo, arvore, antes, resultado, minutos, ok, feito_sonar)",
        "    return fechar(\n"
        "        args, repo, arvore, antes, resultado, minutos, ok, medir_sonar(repo, args)[1]\n"
        "    )",
        "ValidarLocal.test_limpo_registra_e_diz_o_que_foi_medido_e_o_que_so_o_ci_mede",
        "o Sonar local e medido de novo no fim",
    ),
    _s(
        "validar_local.py",
        "    motivo = args.sem_sonar_local\n    if motivo:\n",
        "    motivo = args.sem_sonar_local\n    if False:\n",
        "ValidarLocal.test_sem_sonar_local_com_motivo_registra_e_o_motivo_aparece",
        "--sem-sonar-local nao libera",
    ),
    _s(
        "validar_local.py",
        "    motivo = args.sem_sonar_local\n    if motivo:\n",
        '    motivo = args.sem_sonar_local or "qualquer um"\n    if motivo:\n',
        "ValidarLocal.test_sem_conseguir_medir_fica_vermelha_com_a_instrucao",
        "nao conseguir medir passa sem a opcao",
    ),
    _s(
        "validar_local.py",
        '        return False, ""\n    if nao_mediu is None:',
        '        return True, ""\n    if nao_mediu is None:',
        "ValidarLocal.test_sem_sonar_local_nao_perdoa_problema_novo",
        "problema novo passa",
    ),
]


@unittest.skipIf(os.environ.get("SONAR_LOCAL_BIN_DIR"), "ja e uma isca")
class Iscas(unittest.TestCase):
    """Cada teste reprova quando a regra que ele protege e estragada."""

    def test_cada_isca_reprova_o_teste_dela(self):
        sem_vermelho = []
        for nome, trocas, teste in ISCAS:
            with self.subTest(nome), tempfile.TemporaryDirectory() as tmp:
                copia = Path(tmp) / "bin"
                shutil.copytree(
                    Path(__file__).resolve().parent,
                    copia,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                for arquivo, antes, depois in trocas:
                    texto = (copia / arquivo).read_text()
                    self.assertEqual(texto.count(antes), 1, f"{nome}: a isca nao casa uma vez so")
                    (copia / arquivo).write_text(texto.replace(antes, depois))
                rodada = subprocess.run(
                    [sys.executable, str(copia / "sonar_local_teste.py"), teste],
                    capture_output=True,
                    text=True,
                    env={**os.environ, "SONAR_LOCAL_BIN_DIR": str(copia)},
                    timeout=120,
                )
                if rodada.returncode == 0:
                    sem_vermelho.append(nome)
        self.assertEqual(sem_vermelho, [], "iscas que o teste nao pegou")


if __name__ == "__main__":
    unittest.main()

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
            resultado = {"serverInfo": {"name": "falso"}}
        elif self.modo == "rpc":
            erro = {"code": -32000, "message": f"nao deu, o token era {CANARIO}"}
            return self.json(tratador, {"jsonrpc": "2.0", "id": pedido["id"], "error": erro})
        else:
            argumentos = pedido["params"]["arguments"]
            self.chamadas.append(argumentos)
            texto = json.dumps({"issues": regras_do_sonar(argumentos)})
            resultado = {"content": [{"type": "text", "text": texto}], "isError": False}
        self.json(tratador, {"jsonrpc": "2.0", "id": pedido["id"], "result": resultado})

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

    def ambiente(self, url: str) -> dict[str, str]:
        return {"SONAR_LOCAL_URL": url, "SONAR_LOCAL_HELPER": str(self.helper)}

    def rodar(
        self, url: str, *argumentos: str, entrada: str | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.raiz), *argumentos],
            capture_output=True,
            text=True,
            input=entrada,
            env={**os.environ, **self.ambiente(url)},
            timeout=60,
        )

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
            codigo = sonar_local.main(["--hook"], io.StringIO(self.entrada(o, "src/a.ts")))
            gasto = time.monotonic() - inicio
        self.assertEqual(codigo, 0)
        self.assertLess(gasto, 4)

    def test_o_alarme_do_hook_corta_o_que_nao_e_chamada_ao_mcp(self):
        o = self.oficina()
        o.escrever("src/a.ts", "// TODO de novo\n" + BASE_A)
        with (
            mock.patch.object(sonar_local, "PRAZO_HOOK_S", 1.0),
            mock.patch.object(sonar_local, "descobrir", lambda _repo: time.sleep(4)),
        ):
            inicio = time.monotonic()
            codigo = sonar_local.main(["--hook"], io.StringIO(self.entrada(o, "src/a.ts")))
            gasto = time.monotonic() - inicio
        self.assertEqual(codigo, 0)
        self.assertLess(gasto, 3)


class ValidarLocal(Base):
    def rodada(self, o: Oficina, url: str, motivo: str | None = None, workflows_ok: bool = True):
        args = argparse.Namespace(job=None, workflow=None, sem_sonar_local=motivo)
        arvore = o.git("rev-parse", "HEAD^{tree}")
        saida = io.StringIO()
        with (
            mock.patch.dict(os.environ, o.ambiente(url)),
            contextlib.redirect_stdout(saida),
        ):
            codigo = validar_local.fechar(
                args, o.raiz, arvore, "", {"ci.yml": {"job": "passou"}}, 0.1, workflows_ok
            )
        registrado = (validar_local.registro(o.raiz) / arvore).is_file()
        texto = saida.getvalue()
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
        self.assertIn("REPROVADO", texto)

    def test_limpo_registra_e_diz_o_que_foi_medido_e_o_que_so_o_ci_mede(self):
        o = self.oficina()
        o.escrever("src/a.ts", BASE_A.replace("return 1", "return 2"))
        o.escrever("src/m.rs", "fn main() {}\n")
        o.commitar("limpo")
        with McpFalso() as mcp:
            codigo, registrado, texto, arvore = self.rodada(o, mcp.url)
        self.assertEqual((codigo, registrado), (0, True), texto)
        self.assertIn("Sonar local: 0 problemas novos em 1 arquivo; 1 so o CI mede (Rust 1)", texto)
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

    def test_sem_sonar_local_com_motivo_registra_e_o_motivo_aparece(self):
        o = self.com_problema()
        codigo, registrado, texto, arvore = self.rodada(
            o, porta_fechada(), motivo="MCP da Sonar em manutencao"
        )
        self.assertEqual((codigo, registrado), (0, True), texto)
        self.assertIn("DISPENSADO", texto)
        self.assertIn("MCP da Sonar em manutencao", texto)
        registro = (validar_local.registro(o.raiz) / arvore).read_text()
        self.assertIn("Sonar local dispensado (MCP da Sonar em manutencao)", registro)

    def test_sem_sonar_local_nao_perdoa_problema_novo(self):
        o = self.com_problema()
        with McpFalso() as mcp:
            codigo, registrado, texto, _ = self.rodada(o, mcp.url, motivo="quero passar")
        self.assertEqual((codigo, registrado), (1, False), texto)

    def test_workflow_vermelho_nao_chega_ao_sonar(self):
        o = self.com_problema()
        with McpFalso() as mcp:
            codigo, registrado, _, _ = self.rodada(o, mcp.url, workflows_ok=False)
            self.assertEqual(mcp.chamadas, [])
        self.assertEqual((codigo, registrado), (1, False))

    def test_repositorio_sem_projeto_sonar_registra(self):
        o = self.oficina(workflow=None)
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
        "candidatas.remove(min(candidatas, key=lambda linha: abs(linha - atual.linha)))",
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
        "        ok = sonar_ok and ok\n",
        "        ok = ok\n",
        "ValidarLocal.test_com_problema_novo_a_rodada_fica_vermelha_e_nao_registra",
        "a rodada registra com problema novo",
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
    _s(
        "validar_local.py",
        "    if ok:\n        sonar_ok, feito_sonar = medir_sonar(repo, args)",
        "    if True:\n        sonar_ok, feito_sonar = medir_sonar(repo, args)",
        "ValidarLocal.test_workflow_vermelho_nao_chega_ao_sonar",
        "o Sonar roda com o workflow vermelho",
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

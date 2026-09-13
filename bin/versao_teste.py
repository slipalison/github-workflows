#!/usr/bin/env python3
"""Testes de bin/versao.py: um repositorio git descartavel por caso.

O teste que importa nao e "calcula 1.1.0": e "REPROVA o commit sem tipo", "escolhe
v1.10.0 e nao v1.9.0" e "ignora a tag movel v1". Os casos rodam o script como
processo, do jeito que a esteira roda, e leem o que ele escreve em GITHUB_OUTPUT.

    python3 bin/versao_teste.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "versao.py"


class Repositorio:
    def __init__(self, raiz: Path) -> None:
        self.raiz = raiz
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "teste@exemplo.invalido")
        self.git("config", "user.name", "teste")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        r = subprocess.run(
            ["git", *args], cwd=self.raiz, capture_output=True, text=True, encoding="utf-8"
        )
        if r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
        return r.stdout.strip()

    def commit(self, *paragrafos: str) -> str:
        args = ["commit", "-q", "--allow-empty"]
        for p in paragrafos:
            args += ["-m", p]
        self.git(*args)
        return self.git("rev-parse", "HEAD")

    def tag(self, nome: str) -> None:
        self.git("tag", nome)

    def calcular(self, *extra: str) -> tuple[int, dict[str, str], str, str]:
        saida = self.raiz / "saida.txt"
        painel = self.raiz / "painel.md"
        notas = self.raiz / "notas.md"
        for arquivo in (saida, painel, notas):
            arquivo.unlink(missing_ok=True)
        env = dict(os.environ, GITHUB_OUTPUT=str(saida), GITHUB_STEP_SUMMARY=str(painel))
        r = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "calcular",
                "--repo",
                str(self.raiz),
                "--notas",
                str(notas),
                *extra,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        pares = {}
        if saida.exists():
            for linha in saida.read_text(encoding="utf-8").splitlines():
                chave, _, valor = linha.partition("=")
                pares[chave] = valor
        texto_notas = notas.read_text(encoding="utf-8") if notas.exists() else ""
        return r.returncode, pares, r.stdout + r.stderr, texto_notas


def conferir(mensagem: str) -> int:
    with tempfile.TemporaryDirectory() as d:
        arquivo = Path(d) / "COMMIT_EDITMSG"
        arquivo.write_text(mensagem, encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "conferir", str(arquivo)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return r.returncode


class Calcular(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Repositorio(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def caso(self, tag: str | None, mensagens: list[str | tuple[str, ...]], *extra: str):
        self.repo.commit("feat: base")
        if tag:
            self.repo.tag(tag)
        for m in mensagens:
            self.repo.commit(*(m if isinstance(m, tuple) else (m,)))
        return self.repo.calcular(*extra)

    def test_sem_tag_usa_a_inicial(self):
        rc, s, _, _ = self.caso(None, ["fix: x"])
        self.assertEqual((rc, s["versao"], s["salto"], s["anterior"]), (0, "1.0.0", "inicial", ""))
        rc, s, _, _ = self.repo.calcular("--inicial", "0.1.0")
        self.assertEqual((rc, s["versao"]), (0, "0.1.0"))

    def test_fix_e_patch(self):
        rc, s, _, _ = self.caso("v1.0.0", ["fix: conserto"])
        self.assertEqual((rc, s["versao"], s["salto"], s["tag"]), (0, "1.0.1", "patch", "v1.0.1"))

    def test_docs_e_chore_tambem_sao_patch(self):
        rc, s, _, _ = self.caso("v1.0.0", ["docs: leia", "chore: arrume"])
        self.assertEqual((rc, s["versao"]), (0, "1.0.1"))

    def test_feat_e_minor(self):
        rc, s, _, _ = self.caso("v1.2.3", ["feat(api): novidade"])
        self.assertEqual((rc, s["versao"], s["salto"]), (0, "1.3.0", "minor"))

    def test_exclamacao_e_major(self):
        rc, s, _, _ = self.caso("v1.2.3", ["feat!: quebra"])
        self.assertEqual((rc, s["versao"], s["salto"]), (0, "2.0.0", "major"))

    def test_rodape_breaking_change_e_major(self):
        rc, s, _, _ = self.caso("v1.2.3", [("refactor: muda", "BREAKING CHANGE: contrato mudou")])
        self.assertEqual((rc, s["versao"]), (0, "2.0.0"))
        rc, s, _, _ = self.caso("v3.0.0", [("fix: muda", "BREAKING-CHANGE: com hifen")])
        self.assertEqual(s["versao"], "4.0.0")

    def test_zero_x_nao_tem_regra_especial(self):
        rc, s, _, _ = self.caso("v0.1.0", ["feat!: quebra"])
        self.assertEqual(s["versao"], "1.0.0")

    def test_o_maior_salto_vence(self):
        rc, s, _, _ = self.caso("v1.0.0", ["fix: a", "feat: b", "chore: c"])
        self.assertEqual((rc, s["versao"], s["salto"]), (0, "1.1.0", "minor"))

    def test_sem_tipo_reprova_por_padrao_mas_escreve_as_saidas(self):
        rc, s, log, _ = self.caso("v1.0.0", ["mensagem sem tipo nenhum"])
        self.assertEqual(rc, 1)
        self.assertIn("::error::Commit fora do Conventional Commits", log)
        self.assertEqual((s["versao"], s["salto"], s["sem_tipo"]), ("1.0.0", "nenhum", "1"))

    def test_tipo_desconhecido_e_sem_tipo(self):
        rc, s, _, _ = self.caso("v1.0.0", ["feta: erro de digitacao"])
        self.assertEqual((rc, s["sem_tipo"]), (1, "1"))

    def test_sem_tipo_como_patch(self):
        rc, s, _, _ = self.caso("v1.0.0", ["mensagem solta"], "--sem-tipo", "patch")
        self.assertEqual((rc, s["versao"], s["salto"]), (0, "1.0.1", "patch"))

    def test_sem_tipo_ignorado(self):
        rc, s, _, _ = self.caso("v1.0.0", ["mensagem solta"], "--sem-tipo", "ignorar")
        self.assertEqual((rc, s["versao"], s["salto"]), (0, "1.0.0", "nenhum"))

    def test_novos_so_reprova_o_que_entrou_agora(self):
        self.repo.commit("feat: base")
        self.repo.tag("v1.0.0")
        velho = self.repo.commit("mensagem solta que ja entrou")
        self.repo.commit("fix: agora sim")
        rc, s, _, texto = self.repo.calcular("--novos", f"{velho}..HEAD")
        self.assertEqual((rc, s["versao"], s["sem_tipo"]), (0, "1.0.1", "1"))
        self.assertIn("### Sem tipo", texto)
        self.assertIn("mensagem solta que ja entrou", texto)

    def test_head_tageado_nao_salta(self):
        rc, s, _, _ = self.caso("v2.5.1", [])
        self.assertEqual((rc, s["versao"], s["salto"], s["commits"]), (0, "2.5.1", "nenhum", "0"))

    def test_a_maior_tag_vence_como_numero_e_nao_como_texto(self):
        self.repo.commit("feat: a")
        self.repo.tag("v1.9.0")
        self.repo.commit("feat: b")
        self.repo.tag("v1.10.0")
        self.repo.tag("v1")
        self.repo.commit("fix: c")
        rc, s, _, _ = self.repo.calcular()
        self.assertEqual((s["anterior"], s["versao"]), ("v1.10.0", "1.10.1"))

    def test_revert_automatico_e_patch(self):
        rc, s, _, _ = self.caso("v1.0.0", ['Revert "feat: x"'])
        self.assertEqual((rc, s["versao"]), (0, "1.0.1"))

    def test_esperada_diferente_reprova(self):
        rc, s, log, _ = self.caso("v1.0.0", ["fix: x"], "--esperada", "9.9.9")
        self.assertEqual(rc, 1)
        self.assertIn("Esperava 9.9.9", log)
        rc, _, _, _ = self.repo.calcular("--esperada", "1.0.1")
        self.assertEqual(rc, 0)

    def test_notas_agrupam_por_secao(self):
        rc, _, _, texto = self.caso(
            "v1.0.0", ["feat(api)!: quebra", "feat: nova", "fix: conserto", "docs: leia"]
        )
        ordem = [texto.index(s) for s in ("Quebras", "Novidades", "Correções", "Manutenção")]
        self.assertEqual(ordem, sorted(ordem))
        self.assertIn("- **api**: quebra", texto)


class Conferir(unittest.TestCase):
    def test_aceita(self):
        for m in (
            "fix: conserto",
            "feat(api)!: quebra",
            "Merge branch 'x'",
            'Revert "feat: x"',
            "fixup! fix: y",
            "# comentario do git\nchore: com comentario acima\n# outro",
            "refactor: x\n\nBREAKING CHANGE: y",
        ):
            self.assertEqual(conferir(m), 0, m)

    def test_recusa(self):
        for m in (
            "mensagem sem tipo",
            "feta: erro de digitacao",
            "fix:sem espaco",
            "Fix: maiuscula",
            "fix : espaco antes",
            "",
            "# so comentario\n",
        ):
            self.assertEqual(conferir(m), 1, m)


if __name__ == "__main__":
    unittest.main()

"""Testes de coleta, preservação das respostas e falhas, sem API externa."""

import csv
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.ETL.coletores import coleta_contratos as coletor


CORPO = (
    '{ "resultado": [{"idCompra":"00123","objeto":"Aquisição de medicamentos",'
    '"valorGlobal":14211.00000000,"novoCampo":null},{"idCompra":"00123"}],'
    '"totalRegistros":2,"totalPaginas":1,"paginasRestantes":0 }\n'
).encode("utf-8")
VAZIO = b'{"resultado":[],"totalRegistros":0,"totalPaginas":0,"paginasRestantes":0}'


def resposta(status=200, corpo=CORPO):
    resultado = requests.Response()
    resultado.status_code = status
    resultado._content = corpo
    resultado._content_consumed = True
    resultado.url = "https://dadosabertos.compras.gov.br" + coletor.ENDPOINT
    return resultado


class TestColeta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        self.entrada = self.raiz / "ids.txt"
        self.entrada.write_text("00123\n", encoding="utf-8")
        self.outputs = self.raiz / "outputs"
        self.sessao = Mock(spec=requests.Session)
        self.sessao.get.return_value = resposta()
        for substituicao in (
            patch.object(coletor, "PASTA_OUTPUTS", self.outputs),
            patch.object(coletor, "sessao_http", return_value=self.sessao),
            patch.object(coletor, "load_dotenv"),
            patch.dict(os.environ, {"API_INTERVAL_SECONDS": "0"}, clear=True),
        ):
            substituicao.start()
            self.addCleanup(substituicao.stop)

    def executa(self, *extras, tipo="idCompra", alias="teste"):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return coletor.main([
                "--tipo", tipo, "--arquivo-ids", str(self.entrada),
                "--alias", alias, *extras,
            ])

    def le_csv(self, nome):
        with (self.outputs / "teste" / nome).open(encoding="utf-8", newline="") as arquivo:
            return list(csv.DictReader(arquivo))

    def test_salva_bytes_originais_e_envelope_com_varios_contratos(self):
        self.assertEqual(self.executa(), 0)
        pasta = self.outputs / "teste"
        self.assertEqual((pasta / "000001.json").read_bytes(), CORPO)
        monitor = self.le_csv("monitoramento.csv")[0]
        self.assertEqual(monitor["codigo"], "00123")
        self.assertEqual(monitor["arquivo"], "000001.json")
        self.assertEqual(monitor["status"], "ok")
        self.assertEqual(monitor["http_status"], "200")
        self.assertIn("+00:00", monitor["inicio"])
        self.assertEqual(self.le_csv("erros.csv"), [])
        self.assertTrue((pasta / "coleta.log").exists())
        self.sessao.get.assert_called_once_with(
            "https://dadosabertos.compras.gov.br" + coletor.ENDPOINT,
            params={"tipo": "idCompra", "codigo": "00123"}, timeout=30,
        )
        self.sessao.close.assert_called_once()

    def test_pncp_com_barra_e_zeros_e_enviado_como_parametro(self):
        codigo = "00394445000101-2-000005/2026"
        self.entrada.write_text(codigo, encoding="utf-8")
        self.assertEqual(self.executa(tipo="numeroControlePncpContrato"), 0)
        self.assertEqual(self.sessao.get.call_args.kwargs["params"], {
            "tipo": "numeroControlePncpContrato", "codigo": codigo,
        })
        monitor = self.le_csv("monitoramento.csv")[0]
        self.assertEqual(monitor["codigo"], codigo)
        self.assertIn("%2F2026", monitor["url"])
        self.assertEqual(monitor["arquivo"], "000001.json")

    def test_bom_linhas_vazias_ordem_e_duplicados(self):
        self.entrada.write_text("\ufeff\n00123\n\n00456\n00123\n", encoding="utf-8")
        self.sessao.get.side_effect = [resposta(), resposta(), resposta()]
        self.assertEqual(self.executa(), 0)
        monitor = self.le_csv("monitoramento.csv")
        self.assertEqual([r["codigo"] for r in monitor], ["00123", "00456", "00123"])
        self.assertEqual([r["linha_entrada"] for r in monitor], ["2", "4", "5"])
        self.assertEqual([r["arquivo"] for r in monitor], ["000001.json", "000002.json", "000003.json"])

    def test_limite_conta_somente_ids_nao_vazios(self):
        self.entrada.write_text("\n00123\n\n00456\n", encoding="utf-8")
        self.assertEqual(self.executa("--limite", "1"), 0)
        self.sessao.get.assert_called_once()

    def test_resposta_vazia_e_preservada(self):
        self.sessao.get.return_value = resposta(corpo=VAZIO)
        self.assertEqual(self.executa(), 0)
        self.assertEqual((self.outputs / "teste" / "000001.json").read_bytes(), VAZIO)
        self.assertEqual(self.le_csv("monitoramento.csv")[0]["status"], "sem_dados")

    def test_204_nao_cria_json(self):
        self.sessao.get.return_value = resposta(204, b"")
        self.assertEqual(self.executa(), 0)
        self.assertEqual(list((self.outputs / "teste").glob("*.json")), [])
        monitor = self.le_csv("monitoramento.csv")[0]
        self.assertEqual((monitor["http_status"], monitor["status"], monitor["arquivo"]), ("204", "sem_dados", ""))

    def test_falhas_individuais_nao_interrompem_ids_seguintes(self):
        self.entrada.write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
        self.sessao.get.side_effect = [
            resposta(400, b'{}'), resposta(503, b'{}'),
            resposta(200, b'<html>indisponivel</html>'), requests.Timeout("timeout"), resposta(),
        ]
        self.assertEqual(self.executa(), 1)
        monitor = self.le_csv("monitoramento.csv")
        self.assertEqual([r["status"] for r in monitor], ["falha"] * 4 + ["ok"])
        self.assertEqual([r["http_status"] for r in monitor], ["400", "503", "200", "", "200"])
        self.assertEqual(len(self.le_csv("erros.csv")), 4)
        self.assertEqual([p.name for p in (self.outputs / "teste").glob("*.json")], ["000005.json"])

    def test_falha_na_gravacao_atomica_nao_deixa_json_parcial(self):
        self.entrada.write_text("1\n2\n", encoding="utf-8")
        self.sessao.get.side_effect = [resposta(), resposta()]
        substitui_original = Path.replace

        def substitui(caminho, destino):
            if destino.name == "000001.json":
                raise OSError("Falha simulada de escrita")
            return substitui_original(caminho, destino)

        with patch.object(Path, "replace", substitui):
            self.assertEqual(self.executa(), 1)
        pasta = self.outputs / "teste"
        self.assertFalse((pasta / "000001.json").exists())
        self.assertFalse(list(pasta.glob("*.part")))
        self.assertEqual((pasta / "000002.json").read_bytes(), CORPO)

    def test_numeros_nao_finitos_nao_sao_aceitos_como_json_valido(self):
        for indice, constante in enumerate((b"NaN", b"Infinity", b"-Infinity")):
            with self.subTest(constante=constante):
                self.sessao.get.return_value = resposta(corpo=b'{"valor":' + constante + b'}')
                alias = f"invalido-{indice}"
                self.assertEqual(self.executa(alias=alias), 1)
                self.assertFalse(list((self.outputs / alias).glob("*.json")))

    def test_alias_existente_preserva_conteudo_e_nao_faz_requisicoes(self):
        pasta = self.outputs / "teste"
        pasta.mkdir(parents=True)
        existente = pasta / "000001.json"
        existente.write_bytes(b"original")
        with self.assertRaises(SystemExit) as erro:
            self.executa()
        self.assertEqual(erro.exception.code, 2)
        self.assertEqual(existente.read_bytes(), b"original")
        self.sessao.get.assert_not_called()

    def test_entrada_vazia_inexistente_ou_utf8_invalido_falha_antes_da_coleta(self):
        for conteudo in (b" \n\n", b"\xff", None):
            with self.subTest(conteudo=conteudo):
                if conteudo is None:
                    self.entrada.unlink()
                else:
                    self.entrada.write_bytes(conteudo)
                with self.assertRaises(SystemExit) as erro:
                    self.executa()
                self.assertEqual(erro.exception.code, 2)
                self.assertFalse(self.outputs.exists())
        self.sessao.get.assert_not_called()

    def test_argumentos_invalidos_nao_criam_outputs(self):
        for extras, alias, tipo in (
            ([], "../fora", "idCompra"),
            ([], "..", "idCompra"),
            ([], "C:\\fora", "idCompra"),
            (["--limite", "0"], "teste", "idCompra"),
            (["--limite", "-1"], "teste", "idCompra"),
            ([], "teste", "invalido"),
        ):
            with self.subTest(extras=extras, alias=alias, tipo=tipo):
                with self.assertRaises(SystemExit) as erro:
                    self.executa(*extras, alias=alias, tipo=tipo)
                self.assertEqual(erro.exception.code, 2)
        self.assertFalse(self.outputs.exists())
        self.sessao.get.assert_not_called()

    def test_intervalo_configuravel_entre_consultas(self):
        self.entrada.write_text("1\n2\n3\n", encoding="utf-8")
        self.sessao.get.side_effect = [resposta(), resposta(), resposta()]
        with patch.dict(os.environ, {"API_INTERVAL_SECONDS": "0.5"}), patch.object(coletor.time, "sleep") as espera:
            self.assertEqual(self.executa(), 0)
        self.assertEqual([c.args[0] for c in espera.call_args_list], [0.5, 0.5])


class TestConfiguracao(unittest.TestCase):
    def test_valores_padrao(self):
        with patch.dict(os.environ, {}, clear=True):
            config = coletor.configuracao()
        self.assertEqual((config.timeout, config.intervalo), (30, 1))

    def test_configuracao_invalida(self):
        for nome, valor in (
            ("API_TIMEOUT_SECONDS", "0"), ("API_TIMEOUT_SECONDS", "NaN"),
            ("API_TIMEOUT_SECONDS", "texto"), ("API_INTERVAL_SECONDS", "-1"),
            ("API_INTERVAL_SECONDS", "inf"), ("COMPRAS_API_BASE_URL", "invalido"),
            ("COMPRAS_API_BASE_URL", "https://example.com/?x=1"),
        ):
            with self.subTest(nome=nome, valor=valor), patch.dict(os.environ, {nome: valor}, clear=True):
                with self.assertRaises(ValueError):
                    coletor.configuracao()

    def test_cli_funciona_fora_do_repositorio(self):
        with tempfile.TemporaryDirectory() as diretorio:
            resultado = subprocess.run(
                [sys.executable, str(Path(coletor.__file__).resolve()), "--help"],
                cwd=diretorio, capture_output=True, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("--arquivo-ids", resultado.stdout)
        self.assertEqual(coletor.PASTA_OUTPUTS, coletor.RAIZ_REPO / "src" / "ETL" / "outputs")


class TestRetentativas(unittest.TestCase):
    def requisita(self, codigos):
        recebidas = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                indice = len(recebidas)
                recebidas.append(self.path)
                status = codigos[min(indice, len(codigos) - 1)]
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "2")
                self.end_headers()
                self.wfile.write(CORPO)

            def log_message(self, *args):
                pass

        servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=servidor.serve_forever, daemon=True)
        thread.start()
        try:
            with coletor.sessao_http() as sessao, patch("urllib3.util.retry.time.sleep") as espera:
                sessao.trust_env = False
                retorno = sessao.get(f"http://127.0.0.1:{servidor.server_port}/contratos", timeout=2)
                pausas = [c.args[0] for c in espera.call_args_list]
            return retorno.status_code, recebidas, pausas
        finally:
            servidor.shutdown()
            servidor.server_close()
            thread.join(timeout=2)

    def test_429_retry_after_e_503_antes_do_sucesso(self):
        status, recebidas, pausas = self.requisita([429, 503, 200])
        self.assertEqual(status, 200)
        self.assertEqual(len(recebidas), 3)
        self.assertEqual(pausas, [2, 4])

    def test_nao_repete_400(self):
        status, recebidas, _ = self.requisita([400])
        self.assertEqual((status, len(recebidas)), (400, 1))

    def test_limita_a_oito_retentativas_e_retorna_ultima_falha(self):
        status, recebidas, _ = self.requisita([503])
        self.assertEqual((status, len(recebidas)), (503, 9))


if __name__ == "__main__":
    unittest.main()

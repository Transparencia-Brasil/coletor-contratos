"""Testes do coletor de contratos, sem acesso ao repositório público."""

import argparse
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

from src.coletores import coleta_contratos as coletor

RAIZ = Path(__file__).resolve().parents[1]
CORPO = '﻿id,objeto\r\n1,"Aquisição de café; ""especial"""\r\n'.encode("utf-8")
LAST_MODIFIED = "Fri, 10 Jul 2026 06:07:00 GMT"
MOMENTO = datetime(2026, 10, 28, 10, 29, 15)
ANUAL = coletor.Periodo("anual", "2026")
MENSAL = coletor.Periodo("mensal", "2026", "07")
DIARIO = coletor.Periodo("diario", "2026", "07", "09")


def resposta(status=200, corpo=CORPO, cabecalhos=None, content_length=None):
    """Descreve uma resposta do servidor local.

    content_length=None declara o tamanho real do corpo; False omite o
    cabeçalho; um inteiro declara esse valor, mesmo que diferente do corpo.
    """
    return {"status": status, "corpo": corpo, "cabecalhos": cabecalhos or {},
            "content_length": content_length}


class ServidorLocal:
    """Servidor HTTP em thread que responde, em ordem, a respostas enfileiradas.

    Depois que a fila acaba, a última resposta é repetida. Usa HTTP/1.0, então
    cada resposta fecha a conexão, o que permite corpos sem Content-Length e
    corpos menores que o tamanho declarado.
    """

    def __init__(self):
        self.respostas = [resposta()]
        self.caminhos = []
        self.cabecalhos_recebidos = []
        servidor = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                servidor.caminhos.append(self.path)
                servidor.cabecalhos_recebidos.append(dict(self.headers))
                atual = servidor.respostas.pop(0) if len(servidor.respostas) > 1 else servidor.respostas[0]
                self.send_response(atual["status"])
                for nome, valor in atual["cabecalhos"].items():
                    self.send_header(nome, valor)
                if atual["content_length"] is None:
                    self.send_header("Content-Length", str(len(atual["corpo"])))
                elif atual["content_length"] is not False:
                    self.send_header("Content-Length", str(atual["content_length"]))
                self.end_headers()
                self.wfile.write(atual["corpo"])

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/base"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def fecha(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


class ComServidor(unittest.TestCase):
    """Base para testes que usam o servidor local e uma pasta temporária."""

    def setUp(self):
        self.servidor = ServidorLocal()
        self.addCleanup(self.servidor.fecha)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pasta = Path(tmp.name)
        self.destino = self.pasta / coletor.nome_arquivo(ANUAL)
        self.part = self.destino.with_name(self.destino.name + ".part")
        self.url = coletor.url_arquivo(coletor.Configuracao(self.servidor.base, 5.0), ANUAL)


class TestConfiguracao(unittest.TestCase):
    def ambiente(self, valores=None):
        """Isola o ambiente do processo e impede a leitura do .env local."""
        return (
            patch.dict(os.environ, valores or {}, clear=True),
            patch.object(coletor, "load_dotenv"),
        )

    def configuracao_com(self, valores):
        ambiente, dotenv = self.ambiente(valores)
        with ambiente, dotenv:
            return coletor.configuracao()

    def test_url_e_nome_montados_pelo_periodo(self):
        config = coletor.Configuracao("https://exemplo.gov.br/base", 60.0)
        for periodo, url in (
            (coletor.Periodo("anual", "2024"),
             "https://exemplo.gov.br/base/anual/2024/"
             "comprasnet-contratos-anual-contratos-2024.csv"),
            (MENSAL,
             "https://exemplo.gov.br/base/mensal/2026/07/"
             "comprasnet-contratos-mensal-contratos-2026-07.csv"),
            (DIARIO,
             "https://exemplo.gov.br/base/diario/2026/07/09/"
             "comprasnet-contratos-diario-contratos-2026-07-09.csv"),
        ):
            with self.subTest(periodo=periodo):
                self.assertEqual(coletor.url_arquivo(config, periodo), url)
                self.assertEqual(coletor.nome_arquivo(periodo), url.rsplit("/", 1)[1])

    def test_padroes(self):
        config = self.configuracao_com({})
        self.assertEqual(
            config.url_base,
            "https://repositorio.dados.gov.br/seges/comprasnet_contratos",
        )
        self.assertEqual(config.timeout, 60.0)

    def test_ambiente_tem_precedencia(self):
        config = self.configuracao_com({
            "REPOSITORIO_BASE_URL": "http://127.0.0.1:8000/base",
            "DOWNLOAD_TIMEOUT_SECONDS": "5",
        })
        self.assertEqual(config.url_base, "http://127.0.0.1:8000/base")
        self.assertEqual(config.timeout, 5.0)

    def test_barra_final_removida(self):
        config = self.configuracao_com({"REPOSITORIO_BASE_URL": "https://x.gov.br/base/"})
        self.assertEqual(config.url_base, "https://x.gov.br/base")

    def test_configuracao_invalida(self):
        for valores in (
            {"REPOSITORIO_BASE_URL": "https://x.gov.br/base?a=1"},
            {"REPOSITORIO_BASE_URL": "https://x.gov.br/base#parte"},
            {"REPOSITORIO_BASE_URL": "ftp://x.gov.br"},
            {"REPOSITORIO_BASE_URL": "https://"},
            {"DOWNLOAD_TIMEOUT_SECONDS": "0"},
            {"DOWNLOAD_TIMEOUT_SECONDS": "-1"},
            {"DOWNLOAD_TIMEOUT_SECONDS": "inf"},
            {"DOWNLOAD_TIMEOUT_SECONDS": "nan"},
            {"DOWNLOAD_TIMEOUT_SECONDS": "abc"},
        ):
            with self.subTest(valores=valores), self.assertRaises(ValueError):
                self.configuracao_com(valores)

    def test_ano_valido(self):
        self.assertEqual(coletor.valida_ano("2026"), "2026")

    def test_ano_invalido(self):
        for texto in ("26", "abcd", "20266", " 2026", "2026 ", "", "２０２６"):
            with self.subTest(texto=texto), self.assertRaises(argparse.ArgumentTypeError):
                coletor.valida_ano(texto)

    def test_mes_e_dia_validos_com_zero_a_esquerda(self):
        for texto, esperado in (("7", "07"), ("07", "07"), ("1", "01"), ("12", "12")):
            with self.subTest(texto=texto):
                self.assertEqual(coletor.valida_mes(texto), esperado)
        for texto, esperado in (("9", "09"), ("09", "09"), ("31", "31")):
            with self.subTest(texto=texto):
                self.assertEqual(coletor.valida_dia(texto), esperado)

    def test_mes_invalido(self):
        for texto in ("0", "00", "13", "007", "jul", "", " 7", "７"):
            with self.subTest(texto=texto), self.assertRaises(argparse.ArgumentTypeError):
                coletor.valida_mes(texto)

    def test_dia_invalido(self):
        for texto in ("0", "00", "32", "009", "x", "", "９"):
            with self.subTest(texto=texto), self.assertRaises(argparse.ArgumentTypeError):
                coletor.valida_dia(texto)

    def test_le_argumentos_por_tipo(self):
        for argv, esperado in (
            (["--coleta", "anual", "--ano", "2026"], ANUAL),
            (["--coleta", "mensal", "--ano", "2026", "--mes", "7"], MENSAL),
            (["--coleta", "diario", "--ano", "2026", "--mes", "07", "--dia", "9"], DIARIO),
            (["--coleta", "diario", "--ano", "2024", "--mes", "02", "--dia", "29"],
             coletor.Periodo("diario", "2024", "02", "29")),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(coletor.le_argumentos(argv), (esperado, None))

    def test_le_argumentos_com_saida(self):
        argv = ["--coleta", "anual", "--ano", "2026", "--saida", "pasta/contratos.csv"]
        self.assertEqual(coletor.le_argumentos(argv), (ANUAL, Path("pasta/contratos.csv")))

    def test_le_argumentos_rejeita_argumentos_incoerentes(self):
        for argv in (
            ["--ano", "2026"],
            ["--coleta", "semanal", "--ano", "2026"],
            ["--coleta", "diário", "--ano", "2026", "--mes", "07", "--dia", "09"],
            ["--coleta", "anual"],
            ["--coleta", "anual", "--ano", "2026", "--mes", "07"],
            ["--coleta", "anual", "--ano", "2026", "--dia", "09"],
            ["--coleta", "mensal", "--ano", "2026"],
            ["--coleta", "mensal", "--mes", "07"],
            ["--coleta", "mensal", "--ano", "2026", "--mes", "07", "--dia", "09"],
            ["--coleta", "diario", "--ano", "2026", "--mes", "07"],
            ["--coleta", "diario", "--ano", "2026", "--dia", "09"],
            ["--coleta", "diario", "--ano", "2026", "--mes", "02", "--dia", "30"],
            ["--coleta", "diario", "--ano", "2025", "--mes", "02", "--dia", "29"],
            ["--coleta", "diario", "--ano", "2026", "--mes", "04", "--dia", "31"],
            ["--coleta", "anual", "--ano", "2026", "--sobrescrever"],
            ["--coleta", "anual", "--ano", "2026", "--saida"],
        ):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit) as erro:
                coletor.le_argumentos(argv)
            self.assertEqual(erro.exception.code, 2)

    def test_destino_por_periodo_com_data_e_hora_do_download(self):
        momento = datetime(2026, 10, 28, 9, 5, 7)
        with patch.object(coletor, "PASTA_SAIDA", Path("saida")):
            for periodo, esperado in (
                (coletor.Periodo("anual", "2021"),
                 Path("saida", "anual", "2021", "20261028_090507_contratos_anual_2021.csv")),
                (MENSAL,
                 Path("saida", "mensal", "2026", "07", "20261028_090507_contratos_mensal_2026-07.csv")),
                (DIARIO,
                 Path("saida", "diario", "2026", "07", "09",
                      "20261028_090507_contratos_diario_2026-07-09.csv")),
            ):
                with self.subTest(periodo=periodo):
                    self.assertEqual(coletor.destino_arquivo(periodo, momento), esperado)


class TestDownload(ComServidor):
    def baixa(self, destino=None):
        return coletor.baixa_arquivo(coletor.sessao_http(), self.url, destino or self.destino, 5.0)

    def test_conteudo_identico_byte_a_byte(self):
        self.servidor.respostas = [resposta(cabecalhos={"Last-Modified": LAST_MODIFIED})]
        download = self.baixa()
        self.assertEqual(self.destino.read_bytes(), CORPO)
        self.assertEqual(download, coletor.Download(len(CORPO), LAST_MODIFIED, len(CORPO)))
        self.assertFalse(self.part.exists())
        self.assertEqual(self.servidor.caminhos,
                         ["/base/anual/2026/comprasnet-contratos-anual-contratos-2026.csv"])

    def test_pede_conteudo_sem_compressao(self):
        # Com gzip, o requests descomprimiria o corpo e o arquivo deixaria de ser o publicado.
        self.baixa()
        self.assertEqual(self.servidor.cabecalhos_recebidos[0].get("Accept-Encoding"), "identity")

    def test_corpo_maior_que_um_bloco(self):
        corpo = os.urandom(coletor.TAMANHO_BLOCO * 2 + coletor.TAMANHO_BLOCO // 2)
        self.servidor.respostas = [resposta(corpo=corpo)]
        self.assertEqual(self.baixa().bytes_gravados, len(corpo))
        self.assertEqual(self.destino.read_bytes(), corpo)

    def test_cria_pasta_destino(self):
        destino = self.pasta / "novo" / "sub" / coletor.nome_arquivo(ANUAL)
        self.baixa(destino)
        self.assertEqual(destino.read_bytes(), CORPO)

    def test_sem_content_length(self):
        self.servidor.respostas = [resposta(content_length=False)]
        download = self.baixa()
        self.assertEqual(self.destino.read_bytes(), CORPO)
        self.assertIsNone(download.content_length)
        self.assertIsNone(download.last_modified)

    def test_404(self):
        self.servidor.respostas = [resposta(status=404, corpo=b"nao encontrado")]
        with self.assertRaises(coletor.PeriodoNaoPublicado):
            self.baixa()
        self.assertFalse(self.destino.exists())
        self.assertFalse(self.part.exists())

    def test_status_nao_200(self):
        self.servidor.respostas = [resposta(status=403, corpo=b"proibido")]
        with self.assertRaises(requests.HTTPError) as erro:
            self.baixa()
        self.assertEqual(erro.exception.response.status_code, 403)
        self.assertFalse(self.destino.exists())

    def test_truncado(self):
        self.servidor.respostas = [resposta(content_length=len(CORPO) + 100)]
        with self.assertRaises((coletor.DownloadIncompleto, requests.RequestException)):
            self.baixa()
        self.assertFalse(self.destino.exists())
        self.assertFalse(self.part.exists())

    def test_truncado_preserva_arquivo_anterior(self):
        self.destino.write_bytes(b"antigo")
        self.servidor.respostas = [resposta(content_length=len(CORPO) + 100)]
        with self.assertRaises((coletor.DownloadIncompleto, requests.RequestException)):
            self.baixa()
        self.assertEqual(self.destino.read_bytes(), b"antigo")
        self.assertFalse(self.part.exists())

    def test_tamanho_diferente_do_declarado(self):
        # A conferência própria vale mesmo se o cliente HTTP não acusar o corte.
        original = requests.Response.iter_content

        def corta(resposta_http, *args, **kwargs):
            yield next(original(resposta_http, *args, **kwargs))[:-1]

        with patch.object(requests.Response, "iter_content", corta):
            with self.assertRaises(coletor.DownloadIncompleto):
                self.baixa()
        self.assertFalse(self.destino.exists())
        self.assertFalse(self.part.exists())

    def test_retentativa_503(self):
        self.servidor.respostas = [
            resposta(status=503, corpo=b"indisponivel", cabecalhos={"Retry-After": "0"}),
            resposta(),
        ]
        self.baixa()
        self.assertEqual(self.destino.read_bytes(), CORPO)
        self.assertEqual(len(self.servidor.caminhos), 2)

    def test_interrupcao_remove_part(self):
        self.destino.write_bytes(b"antigo")
        original = requests.Response.iter_content

        def interrompe(resposta_http, *args, **kwargs):
            yield next(original(resposta_http, *args, **kwargs))
            raise KeyboardInterrupt

        with patch.object(requests.Response, "iter_content", interrompe):
            with self.assertRaises(KeyboardInterrupt):
                self.baixa()
        self.assertEqual(self.destino.read_bytes(), b"antigo")
        self.assertFalse(self.part.exists())


class TestMain(ComServidor):
    def setUp(self):
        super().setUp()
        for contexto in (
            patch.dict(os.environ, {"REPOSITORIO_BASE_URL": self.servidor.base,
                                    "DOWNLOAD_TIMEOUT_SECONDS": "5"}),
            patch.object(coletor, "PASTA_SAIDA", self.pasta),
            patch.object(coletor, "datetime", wraps=datetime),
        ):
            contexto.start()
            self.addCleanup(contexto.stop)
        coletor.datetime.now.return_value = MOMENTO
        # Caminho esperado escrito por extenso, sem usar destino_arquivo().
        self.destino = self.pasta / "anual" / "2026" / "20261028_102915_contratos_anual_2026.csv"
        self.part = self.destino.with_name(self.destino.name + ".part")

    def executa(self, *argumentos, nivel="INFO"):
        """Roda main() e devolve o código de saída e as mensagens registradas.

        Sem argumentos, pede a coleta anual de 2026.
        """
        with self.assertLogs("coleta_contratos", level=nivel) as log:
            codigo = coletor.main(list(argumentos) or ["--coleta", "anual", "--ano", "2026"])
        return codigo, "\n".join(log.output)

    def test_main_sucesso(self):
        self.servidor.respostas = [resposta(cabecalhos={"Last-Modified": LAST_MODIFIED})]
        codigo, log = self.executa()
        self.assertEqual(codigo, 0)
        self.assertEqual(self.destino.read_bytes(), CORPO)
        self.assertEqual(self.servidor.caminhos,
                         ["/base/anual/2026/comprasnet-contratos-anual-contratos-2026.csv"])
        self.assertIn(LAST_MODIFIED, log)
        self.assertIn(str(len(CORPO)), log)
        self.assertIn(str(self.destino), log)

    def test_main_sucesso_mensal_e_diario(self):
        for argv, caminho, destino in (
            (["--coleta", "mensal", "--ano", "2026", "--mes", "7"],
             "/base/mensal/2026/07/comprasnet-contratos-mensal-contratos-2026-07.csv",
             self.pasta / "mensal" / "2026" / "07" / "20261028_102915_contratos_mensal_2026-07.csv"),
            (["--coleta", "diario", "--ano", "2026", "--mes", "7", "--dia", "9"],
             "/base/diario/2026/07/09/comprasnet-contratos-diario-contratos-2026-07-09.csv",
             self.pasta / "diario" / "2026" / "07" / "09"
             / "20261028_102915_contratos_diario_2026-07-09.csv"),
        ):
            with self.subTest(argv=argv):
                self.servidor.caminhos.clear()
                codigo, log = self.executa(*argv)
                self.assertEqual(codigo, 0)
                self.assertEqual(self.servidor.caminhos, [caminho])
                self.assertEqual(destino.read_bytes(), CORPO)
                self.assertIn(str(destino), log)

    def test_main_sucesso_sem_cabecalhos_opcionais(self):
        self.servidor.respostas = [resposta(content_length=False)]
        codigo, log = self.executa()
        self.assertEqual(codigo, 0)
        self.assertIn("não informado", log)
        self.assertIn("tamanho não declarado", log)

    def test_main_nome_existente_recusado(self):
        self.destino.parent.mkdir(parents=True)
        self.destino.write_bytes(b"antigo")
        codigo, log = self.executa()
        self.assertEqual(codigo, 2)
        self.assertEqual(self.destino.read_bytes(), b"antigo")
        self.assertEqual(self.servidor.caminhos, [])
        self.assertIn(str(self.destino), log)

    def test_main_saida_absoluta_substitui_destino_automatico(self):
        saida = self.pasta / "outro" / "sub" / "contratos.csv"
        codigo, log = self.executa("--coleta", "anual", "--ano", "2026", "--saida", str(saida))
        self.assertEqual(codigo, 0)
        self.assertEqual(saida.read_bytes(), CORPO)
        self.assertFalse(saida.with_name(saida.name + ".part").exists())
        self.assertFalse((self.pasta / "anual").exists())
        self.assertIn(str(saida), log)

    def test_main_saida_relativa_ao_diretorio_de_execucao(self):
        with tempfile.TemporaryDirectory() as tmp:
            diretorio_anterior = os.getcwd()
            os.chdir(tmp)
            try:
                codigo, log = self.executa("--coleta", "mensal", "--ano", "2026", "--mes", "7",
                                           "--saida", "dados/mensal.csv")
            finally:
                os.chdir(diretorio_anterior)
            saida = Path(tmp).resolve() / "dados" / "mensal.csv"
            self.assertEqual(codigo, 0)
            self.assertEqual(saida.read_bytes(), CORPO)
            self.assertIn(str(saida), log)

    def test_main_saida_existente_recusada(self):
        saida = self.pasta / "contratos.csv"
        saida.write_bytes(b"antigo")
        codigo, log = self.executa("--coleta", "anual", "--ano", "2026", "--saida", str(saida))
        self.assertEqual(codigo, 2)
        self.assertEqual(saida.read_bytes(), b"antigo")
        self.assertEqual(self.servidor.caminhos, [])
        self.assertIn(str(saida), log)

    def test_main_saida_que_e_pasta_recusada(self):
        codigo, log = self.executa("--coleta", "anual", "--ano", "2026", "--saida", str(self.pasta))
        self.assertEqual(codigo, 2)
        self.assertEqual(self.servidor.caminhos, [])
        self.assertIn(str(self.pasta), log)

    def test_main_downloads_em_momentos_diferentes_geram_dois_arquivos(self):
        coletor.datetime.now.side_effect = [MOMENTO, datetime(2026, 10, 28, 10, 29, 16)]
        self.assertEqual(self.executa()[0], 0)
        self.assertEqual(self.executa()[0], 0)
        self.assertEqual(
            sorted(p.name for p in (self.pasta / "anual" / "2026").iterdir()),
            ["20261028_102915_contratos_anual_2026.csv",
             "20261028_102916_contratos_anual_2026.csv"],
        )

    def test_main_part_orfao_nao_bloqueia(self):
        self.part.parent.mkdir(parents=True)
        self.part.write_bytes(b"lixo")
        codigo, _ = self.executa()
        self.assertEqual(codigo, 0)
        self.assertEqual(self.destino.read_bytes(), CORPO)
        self.assertFalse(self.part.exists())

    def test_main_404(self):
        self.servidor.respostas = [resposta(status=404, corpo=b"")]
        codigo, log = self.executa()
        self.assertEqual(codigo, 2)
        self.assertIn("Período anual 2026 não publicado no repositório", log)
        self.assertFalse(self.destino.exists())

    def test_main_404_mensal(self):
        self.servidor.respostas = [resposta(status=404, corpo=b"")]
        codigo, log = self.executa("--coleta", "mensal", "--ano", "2025", "--mes", "11")
        self.assertEqual(codigo, 2)
        self.assertIn("Período mensal 2025-11 não publicado no repositório", log)

    def test_main_403(self):
        self.servidor.respostas = [resposta(status=403, corpo=b"")]
        codigo, log = self.executa()
        self.assertEqual(codigo, 1)
        self.assertIn("403", log)
        self.assertFalse(self.destino.exists())

    def test_main_truncado(self):
        self.servidor.respostas = [resposta(content_length=len(CORPO) + 100)]
        codigo, _ = self.executa()
        self.assertEqual(codigo, 1)
        self.assertFalse(self.destino.exists())
        self.assertFalse(self.part.exists())

    def test_main_argumentos_invalidos_nao_acessam_a_fonte(self):
        for argv in (
            ["--coleta", "anual", "--ano", "26"],
            ["--ano", "2026"],
            ["--coleta", "mensal", "--ano", "2026"],
            ["--coleta", "diario", "--ano", "2026", "--mes", "02", "--dia", "30"],
        ):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit) as erro:
                coletor.main(argv)
            self.assertEqual(erro.exception.code, 2)
        self.assertEqual(self.servidor.caminhos, [])

    def test_main_configuracao_invalida(self):
        with patch.dict(os.environ, {"REPOSITORIO_BASE_URL": "https://x.gov.br/base?a=1"}):
            codigo, log = self.executa()
        self.assertEqual(codigo, 2)
        self.assertIn("REPOSITORIO_BASE_URL", log)


def bash_compativel():
    """Retorna um bash que aceite caminhos do Windows, ou None.

    No Windows, o primeiro bash do PATH costuma ser o do WSL, que não entende
    caminhos como C:/... e por isso não serve para testar o wrapper; usa-se o
    do Git for Windows.
    """
    if os.name != "nt":
        return shutil.which("bash")
    for pasta in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if pasta and (Path(pasta) / "Git" / "bin" / "bash.exe").exists():
            return str(Path(pasta) / "Git" / "bin" / "bash.exe")
    caminho = shutil.which("bash")
    return caminho if caminho and "\\git\\" in caminho.lower() else None


class TestWrapper(unittest.TestCase):
    @unittest.skipUnless(bash_compativel(), "bash compatível indisponível")
    def test_wrapper_de_outro_diretorio(self):
        with tempfile.TemporaryDirectory() as tmp:
            processo = subprocess.run(
                [bash_compativel(), (RAIZ / "src" / "coletores" / "run-coleta-contratos.sh").as_posix(),
                 "--coleta", "anual", "--ano", "26"],
                cwd=tmp, capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(processo.returncode, 2, processo.stderr)
        self.assertIn("--ano", processo.stderr)

    @unittest.skipUnless(bash_compativel(), "bash compatível indisponível")
    def test_wrapper_saida_relativa_a_quem_chama(self):
        # Simula outro repositório chamando o wrapper com um caminho próprio.
        servidor = ServidorLocal()
        self.addCleanup(servidor.fecha)
        ambiente = {**os.environ, "REPOSITORIO_BASE_URL": servidor.base, "DOWNLOAD_TIMEOUT_SECONDS": "5"}
        with tempfile.TemporaryDirectory() as tmp:
            processo = subprocess.run(
                [bash_compativel(), (RAIZ / "src" / "coletores" / "run-coleta-contratos.sh").as_posix(),
                 "--coleta", "diario", "--ano", "2026", "--mes", "7", "--dia", "9",
                 "--saida", "outputs/diario.csv"],
                cwd=tmp, env=ambiente, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(processo.returncode, 0, processo.stdout + processo.stderr)
            self.assertEqual((Path(tmp) / "outputs" / "diario.csv").read_bytes(), CORPO)
        self.assertEqual(servidor.caminhos,
                         ["/base/diario/2026/07/09/comprasnet-contratos-diario-contratos-2026-07-09.csv"])


if __name__ == "__main__":
    unittest.main()

r"""Baixa o arquivo anual, mensal ou diário de contratos do Contratos.gov.br.

Uso, a partir de qualquer diretório:
    python src/coletores/coleta_contratos.py --coleta anual --ano 2026
    python src/coletores/coleta_contratos.py --coleta mensal --ano 2026 --mes 07
    python src/coletores/coleta_contratos.py --coleta diario --ano 2026 --mes 07 --dia 09

O tipo de coleta define os parâmetros exigidos: anual pede só o ano; mensal,
ano e mês; diário, ano, mês e dia. O CSV é obtido no repositório de dados
abertos da SEGES e salvo byte a byte como foi recebido, em
outputs/contratos/{coleta}/, numa subpasta por período e com a data e a hora
do download no nome. Nenhuma linha é filtrada, convertida ou reescrita.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RAIZ_REPO = Path(__file__).resolve().parents[2]
PASTA_SAIDA = RAIZ_REPO / "outputs" / "contratos"
BASE_PADRAO = "https://repositorio.dados.gov.br/seges/comprasnet_contratos"
TIMEOUT_PADRAO = "60"
TAMANHO_BLOCO = 1024 * 1024
# [0-9] em vez de \d, que também aceitaria dígitos Unicode como "２０２６".
PADRAO_ANO = re.compile(r"[0-9]{4}")
PADRAO_MES_DIA = re.compile(r"[0-9]{1,2}")
# Parâmetros de período exigidos por tipo de coleta; os nomes das pastas da fonte.
PARAMETROS_COLETA = {
    "anual": ("ano",),
    "mensal": ("ano", "mes"),
    "diario": ("ano", "mes", "dia"),
}

log = logging.getLogger("coleta_contratos")


@dataclass(frozen=True)
class Configuracao:
    """Parâmetros de acesso ao repositório.

    Attributes:
        url_base: URL da pasta comprasnet_contratos, sem barra final.
        timeout: Segundos de espera, por tentativa, para conectar e entre
            leituras sucessivas da resposta.
    """

    url_base: str
    timeout: float


@dataclass(frozen=True)
class Periodo:
    """Período publicado pela fonte que será coletado.

    Attributes:
        coleta: Tipo de coleta, igual ao nome da pasta na fonte: anual,
            mensal ou diario.
        ano: Ano com quatro dígitos.
        mes: Mês com dois dígitos, nas coletas mensal e diária.
        dia: Dia com dois dígitos, na coleta diária.
    """

    coleta: str
    ano: str
    mes: str | None = None
    dia: str | None = None

    @property
    def partes(self) -> tuple[str, ...]:
        """Ano, mês e dia preenchidos, na ordem das subpastas da fonte."""
        return tuple(parte for parte in (self.ano, self.mes, self.dia) if parte is not None)

    @property
    def rotulo(self) -> str:
        """Período como aparece nos nomes dos arquivos: 2026, 2026-07 ou 2026-07-09."""
        return "-".join(self.partes)


@dataclass(frozen=True)
class Download:
    """Resumo de um download concluído.

    Attributes:
        bytes_gravados: Tamanho do arquivo salvo.
        last_modified: Cabeçalho Last-Modified da fonte, se enviado.
        content_length: Tamanho declarado pela fonte, se enviado.
    """

    bytes_gravados: int
    last_modified: str | None
    content_length: int | None


class PeriodoNaoPublicado(Exception):
    """A fonte respondeu 404 para o arquivo do período pedido."""


class DownloadIncompleto(Exception):
    """O total recebido difere do Content-Length declarado pela fonte."""


def configuracao() -> Configuracao:
    """Lê a configuração do ambiente e do .env da raiz do repositório.

    Variáveis já definidas no ambiente têm precedência sobre o .env.

    Returns:
        Configuração validada.

    Raises:
        ValueError: Se a URL base não for HTTP(S) com host, ou tiver query ou
            fragmento, ou se o timeout não for um número positivo e finito.
    """
    load_dotenv(RAIZ_REPO / ".env", override=False)
    url_base = os.getenv("REPOSITORIO_BASE_URL", BASE_PADRAO).rstrip("/")
    url = urlsplit(url_base)
    if url.scheme not in ("https", "http") or not url.netloc or url.query or url.fragment:
        raise ValueError("REPOSITORIO_BASE_URL deve ser uma URL HTTP(S) sem query ou fragmento")
    try:
        timeout = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", TIMEOUT_PADRAO))
    except ValueError as erro:
        raise ValueError("DOWNLOAD_TIMEOUT_SECONDS deve ser um número") from erro
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("DOWNLOAD_TIMEOUT_SECONDS deve ser positivo e finito")
    return Configuracao(url_base, timeout)


def valida_ano(texto: str) -> str:
    """Aceita somente quatro dígitos ASCII; usado como type do argparse.

    A existência do ano no repositório não é verificada aqui: quem decide é a
    fonte, que responde 404 para anos não publicados.

    Args:
        texto: Valor recebido em --ano.

    Returns:
        O próprio texto, sem alterações.

    Raises:
        argparse.ArgumentTypeError: Se o texto não tiver exatamente 4 dígitos.
    """
    if not PADRAO_ANO.fullmatch(texto):
        raise argparse.ArgumentTypeError(f"ano inválido: {texto!r}; use quatro dígitos, como 2026")
    return texto


def valida_mes_dia(texto: str, maximo: int, nome: str) -> str:
    """Aceita de um a dois dígitos ASCII entre 1 e maximo e completa com zero à esquerda."""
    if not PADRAO_MES_DIA.fullmatch(texto) or not 1 <= int(texto) <= maximo:
        raise argparse.ArgumentTypeError(f"{nome} inválido: {texto!r}; use de 1 a {maximo}")
    return texto.zfill(2)


def valida_mes(texto: str) -> str:
    """Valida --mes (1 a 12) e devolve com dois dígitos; usado como type do argparse."""
    return valida_mes_dia(texto, 12, "mês")


def valida_dia(texto: str) -> str:
    """Valida --dia (1 a 31) e devolve com dois dígitos; usado como type do argparse.

    Se o dia existe no mês informado é conferido em le_periodo().
    """
    return valida_mes_dia(texto, 31, "dia")


def nome_arquivo(periodo: Periodo) -> str:
    """Retorna o nome do CSV de contratos do período, igual ao publicado na fonte."""
    return f"comprasnet-contratos-{periodo.coleta}-contratos-{periodo.rotulo}.csv"


def url_arquivo(config: Configuracao, periodo: Periodo) -> str:
    """Monta a URL do CSV de contratos do período.

    Exemplo: {url_base}/mensal/2026/07/comprasnet-contratos-mensal-contratos-2026-07.csv.
    """
    pastas = "/".join(periodo.partes)
    return f"{config.url_base}/{periodo.coleta}/{pastas}/{nome_arquivo(periodo)}"


def destino_arquivo(periodo: Periodo, momento: datetime) -> Path:
    """Monta o caminho local do arquivo de um download.

    As subpastas repetem as da fonte (tipo de coleta, ano, mês e dia), e o nome
    começa pela data e hora do download no formato AAAAMMDD_HHMMSS, para que a
    ordem alfabética seja a cronológica. Exemplo:
    outputs/contratos/mensal/2026/07/20261028_102915_contratos_mensal_2026-07.csv.

    Args:
        periodo: Período coletado.
        momento: Data e hora do download, no horário local da máquina.

    Returns:
        Caminho do arquivo dentro de PASTA_SAIDA.
    """
    nome = f"{momento:%Y%m%d_%H%M%S}_contratos_{periodo.coleta}_{periodo.rotulo}.csv"
    return PASTA_SAIDA.joinpath(periodo.coleta, *periodo.partes, nome)


def sessao_http() -> requests.Session:
    """Cria uma sessão que repete GETs com falhas transitórias.

    Repete até 8 vezes falhas de conexão e respostas 429, 500, 502, 503 e 504,
    com espera crescente de fator 2 e respeito ao cabeçalho Retry-After. Esgotadas
    as tentativas, a última resposta é devolvida para quem chamou tratar o status.
    """
    tentativas = Retry(
        total=8,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    sessao = requests.Session()
    adaptador = HTTPAdapter(max_retries=tentativas)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    sessao.headers["User-Agent"] = "Transparencia-Brasil/coletor-contratos"
    # Sem compressão: o requests descomprimiria o corpo, e o arquivo salvo deixaria
    # de ser o publicado e de conferir com o Content-Length.
    sessao.headers["Accept-Encoding"] = "identity"
    return sessao


def baixa_arquivo(sessao: requests.Session, url: str, destino: Path, timeout: float) -> Download:
    """Baixa a URL em streaming e publica o arquivo completo de forma atômica.

    O corpo é gravado em blocos num arquivo .part ao lado do destino e só
    substitui o destino depois de recebido por inteiro. Em qualquer falha ou
    interrupção, o .part é removido e um destino anterior permanece intacto.

    Args:
        sessao: Sessão HTTP, normalmente criada por sessao_http().
        url: Endereço do CSV.
        destino: Caminho final do arquivo; a pasta é criada se não existir.
        timeout: Segundos para conectar e entre leituras sucessivas da resposta.

    Returns:
        Resumo do download.

    Raises:
        PeriodoNaoPublicado: Se a fonte responder 404.
        requests.HTTPError: Para outro status diferente de 200.
        DownloadIncompleto: Se o total gravado diferir do Content-Length.
        requests.RequestException: Para falhas de conexão ou de leitura.
        OSError: Para falhas ao gravar o arquivo.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    part = destino.with_name(destino.name + ".part")
    try:
        with sessao.get(url, stream=True, timeout=(timeout, timeout)) as resposta:
            if resposta.status_code == 404:
                raise PeriodoNaoPublicado(url)
            if resposta.status_code != 200:
                raise requests.HTTPError(f"HTTP {resposta.status_code} em {url}", response=resposta)
            cabecalho_tamanho = resposta.headers.get("Content-Length")
            content_length = int(cabecalho_tamanho) if cabecalho_tamanho is not None else None
            gravados = 0
            with part.open("wb") as saida:
                for bloco in resposta.iter_content(TAMANHO_BLOCO):
                    saida.write(bloco)
                    gravados += len(bloco)
            if content_length is not None and gravados != content_length:
                raise DownloadIncompleto(
                    f"recebidos {gravados} de {content_length} bytes declarados em {url}"
                )
            last_modified = resposta.headers.get("Last-Modified")
        part.replace(destino)
    finally:
        part.unlink(missing_ok=True)
    return Download(gravados, last_modified, content_length)


def parser_argumentos() -> argparse.ArgumentParser:
    """Cria o parser dos argumentos de linha de comando."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--coleta", required=True, choices=tuple(PARAMETROS_COLETA),
        help="Tipo de coleta: anual (pede --ano), mensal (--ano e --mes) "
             "ou diario (--ano, --mes e --dia).",
    )
    parser.add_argument("--ano", type=valida_ano, help="Ano com quatro dígitos (ex.: 2026).")
    parser.add_argument("--mes", type=valida_mes, help="Mês de 1 a 12 (ex.: 7 ou 07).")
    parser.add_argument("--dia", type=valida_dia, help="Dia de 1 a 31 (ex.: 9 ou 09).")
    return parser


def le_periodo(argv: list[str] | None = None) -> Periodo:
    """Lê os argumentos e confere se combinam com o tipo de coleta.

    Cada tipo exige exatamente os parâmetros de PARAMETROS_COLETA: faltar um
    ou sobrar outro (como --mes numa coleta anual) é erro. Na coleta diária, a
    data também precisa existir no calendário.

    Args:
        argv: Argumentos de linha de comando; None usa sys.argv.

    Returns:
        Período pedido.

    Raises:
        SystemExit: Com código 2, pelo argparse, para argumentos inválidos.
    """
    parser = parser_argumentos()
    args = parser.parse_args(argv)
    exigidos = PARAMETROS_COLETA[args.coleta]
    faltando = [f"--{nome}" for nome in exigidos if getattr(args, nome) is None]
    if faltando:
        parser.error(f"a coleta {args.coleta} exige {', '.join(faltando)}")
    sobrando = [
        f"--{nome}" for nome in ("ano", "mes", "dia")
        if nome not in exigidos and getattr(args, nome) is not None
    ]
    if sobrando:
        parser.error(f"a coleta {args.coleta} não aceita {', '.join(sobrando)}")
    if args.coleta == "diario":
        try:
            date(int(args.ano), int(args.mes), int(args.dia))
        except ValueError:
            parser.error(f"data inexistente: {args.ano}-{args.mes}-{args.dia}")
    return Periodo(args.coleta, args.ano, args.mes, args.dia)


def main(argv: list[str] | None = None) -> int:
    """Executa a coleta do período pedido e devolve o código de saída.

    Args:
        argv: Argumentos de linha de comando; None usa sys.argv.

    Returns:
        0 quando o arquivo é salvo; 1 para falha de rede, HTTP diferente de
        404, download incompleto ou erro de escrita; 2 para configuração
        inválida, período não publicado ou arquivo com o mesmo nome já
        existente (download no mesmo segundo). Argumentos inválidos encerram
        pelo argparse, também com código 2.
    """
    periodo = le_periodo(argv)
    try:
        config = configuracao()
    except ValueError as erro:
        log.error("Configuração inválida: %s", erro)
        return 2

    url = url_arquivo(config, periodo)
    destino = destino_arquivo(periodo, datetime.now())
    if destino.exists():
        log.error("O arquivo %s já existe; aguarde um segundo e execute novamente", destino)
        return 2

    log.info("Baixando %s", url)
    inicio = time.monotonic()
    try:
        with sessao_http() as sessao:
            download = baixa_arquivo(sessao, url, destino, config.timeout)
    except PeriodoNaoPublicado:
        log.error("Período %s %s não publicado no repositório (%s)", periodo.coleta, periodo.rotulo, url)
        return 2
    except (requests.RequestException, DownloadIncompleto, OSError) as erro:
        log.error("Falha no download: %s", erro)
        return 1

    log.info(
        "Arquivo salvo em %s: %d bytes (Content-Length: %s), Last-Modified: %s, %.1f s",
        destino,
        download.bytes_gravados,
        download.content_length if download.content_length is not None else "tamanho não declarado",
        download.last_modified or "não informado",
        time.monotonic() - inicio,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    sys.exit(main())

r"""Coleta respostas originais do endpoint de contratos por identificador.

Uso, a partir da raiz do repositório:
    python src/ETL/coletores/coleta_contratos.py --tipo idCompra \
        --arquivo-ids inputs/ids.txt --alias minha-coleta

Os JSONs são gravados sem reserialização, um por consulta. Os CSVs contêm
somente informações operacionais; nenhum contrato é transformado ou filtrado.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import sys
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RAIZ_REPO = Path(__file__).resolve().parents[3]
PASTA_OUTPUTS = RAIZ_REPO / "src" / "ETL" / "outputs"
ENDPOINT = "/modulo-contratos/1.1_consultarContratos_Id"
TIPOS = ("idCompra", "numeroControlePncpContrato")
COLUNAS_MONITORAMENTO = (
    "consulta", "linha_entrada", "tipo", "codigo", "url", "http_status",
    "inicio", "fim", "duracao_segundos", "status", "arquivo",
)
COLUNAS_ERROS = (
    "consulta", "linha_entrada", "tipo", "codigo", "http_status", "erro",
)


@dataclass(frozen=True)
class Configuracao:
    url_base: str
    timeout: float
    intervalo: float

    @property
    def endpoint(self) -> str:
        return self.url_base + ENDPOINT


def configuracao() -> Configuracao:
    """Lê as configurações do ambiente, sem alterar os valores dos dados."""
    url_base = os.getenv(
        "COMPRAS_API_BASE_URL", "https://dadosabertos.compras.gov.br"
    ).rstrip("/")
    url = urlsplit(url_base)
    if url.scheme not in ("https", "http") or not url.netloc or url.query or url.fragment:
        raise ValueError("COMPRAS_API_BASE_URL deve ser uma URL HTTP(S) sem query ou fragmento")
    try:
        timeout = float(os.getenv("API_TIMEOUT_SECONDS", "30"))
        intervalo = float(os.getenv("API_INTERVAL_SECONDS", "1"))
    except ValueError as erro:
        raise ValueError("Timeout e intervalo devem ser números") from erro
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("API_TIMEOUT_SECONDS deve ser positivo e finito")
    if not math.isfinite(intervalo) or intervalo < 0:
        raise ValueError("API_INTERVAL_SECONDS deve ser não negativo e finito")
    return Configuracao(url_base, timeout, intervalo)


def sessao_http() -> requests.Session:
    """Repete somente GETs com falhas transitórias, respeitando Retry-After."""
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
    return sessao


def le_ids(arquivo: Path, limite: int | None) -> list[tuple[int, str]]:
    """Lê um código por linha, preservando a ordem e as repetições."""
    ids = []
    with arquivo.open(encoding="utf-8-sig") as entrada:
        for numero_linha, linha in enumerate(entrada, start=1):
            codigo = linha.strip()
            if codigo:
                ids.append((numero_linha, codigo))
                if limite is not None and len(ids) >= limite:
                    break
    if not ids:
        raise ValueError("O arquivo de IDs não contém identificadores")
    return ids


def grava_json(destino: Path, corpo: bytes) -> None:
    """Publica um arquivo completo por substituição atômica, sem reserializar."""
    temporario = destino.with_suffix(".json.part")
    try:
        with temporario.open("wb") as saida:
            saida.write(corpo)
        temporario.replace(destino)
    finally:
        temporario.unlink(missing_ok=True)


def rejeita_constante_json(valor: str) -> None:
    """Rejeita NaN e Infinity, que o parser do Python aceita por extensão."""
    raise ValueError(f"Constante inválida em JSON: {valor}")


@contextmanager
def log_da_coleta(pasta: Path) -> Iterator[logging.Logger]:
    logger = logging.getLogger("coleta_contratos")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formato = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.FileHandler(pasta / "coleta.log", encoding="utf-8")]
    handlers.append(logging.StreamHandler(sys.stdout))
    try:
        for handler in handlers:
            handler.setFormatter(formato)
            logger.addHandler(handler)
        yield logger
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


def parser_argumentos() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tipo", required=True, choices=TIPOS, help="Tipo dos identificadores do arquivo.")
    parser.add_argument("--arquivo-ids", required=True, type=Path, help="TXT UTF-8 com um identificador por linha.")
    parser.add_argument("--alias", required=True, help="Nova subpasta de saída em src/ETL/outputs/.")
    parser.add_argument("--limite", type=int, help="Consulta somente os primeiros N IDs não vazios.")
    return parser


def executa_coleta(
    tipo: str,
    ids: list[tuple[int, str]],
    config: Configuracao,
    pasta: Path,
    log: logging.Logger,
) -> int:
    falhou = False
    with (
        (pasta / "monitoramento.csv").open("w", encoding="utf-8", newline="") as monitoramento,
        (pasta / "erros.csv").open("w", encoding="utf-8", newline="") as erros,
        closing(sessao_http()) as sessao,
    ):
        monitor = csv.DictWriter(monitoramento, fieldnames=COLUNAS_MONITORAMENTO)
        registro_erros = csv.DictWriter(erros, fieldnames=COLUNAS_ERROS)
        monitor.writeheader()
        registro_erros.writeheader()
        for consulta, (linha, codigo) in enumerate(ids, start=1):
            if consulta > 1 and config.intervalo:
                time.sleep(config.intervalo)
            inicio = time.monotonic()
            parametros = {"tipo": tipo, "codigo": codigo}
            registro = {
                "consulta": consulta,
                "linha_entrada": linha,
                "tipo": tipo,
                "codigo": codigo,
                "url": requests.Request("GET", config.endpoint, params=parametros).prepare().url,
                "http_status": "",
                "inicio": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "status": "falha",
                "arquivo": "",
            }
            log.info("Consulta %d/%d: %s=%s", consulta, len(ids), tipo, codigo)
            try:
                with sessao.get(config.endpoint, params=parametros, timeout=config.timeout) as resposta:
                    registro["http_status"] = resposta.status_code
                    if resposta.status_code == 204:
                        registro["status"] = "sem_dados"
                    else:
                        resposta.raise_for_status()
                        if resposta.status_code != 200:
                            raise ValueError(f"Status HTTP inesperado: {resposta.status_code}")
                        corpo = resposta.content
                        dados = json.loads(corpo, parse_constant=rejeita_constante_json)
                        nome = f"{consulta:06d}.json"
                        grava_json(pasta / nome, corpo)
                        registro["arquivo"] = nome
                        registro["status"] = (
                            "sem_dados" if isinstance(dados, dict) and dados.get("resultado") == [] else "ok"
                        )
            except (requests.RequestException, ValueError, UnicodeError, OSError) as erro:
                falhou = True
                registro["status"] = "falha"
                detalhe = f"{type(erro).__name__}: {erro}"
                registro_erros.writerow({
                    **{chave: registro[chave] for chave in COLUNAS_ERROS if chave != "erro"},
                    "erro": detalhe,
                })
                erros.flush()
                log.error("Falha na consulta %d (%s): %s", consulta, codigo, detalhe)
            finally:
                registro["fim"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                registro["duracao_segundos"] = round(time.monotonic() - inicio, 3)
                monitor.writerow(registro)
                monitoramento.flush()
            log.info("Consulta %d: %s", consulta, registro["status"])
    log.info("Coleta finalizada: %d consultas; saída: %s", len(ids), pasta)
    return 1 if falhou else 0


def main(argv: list[str] | None = None) -> int:
    parser = parser_argumentos()
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.alias):
        parser.error("--alias deve começar com letra ou número e conter apenas letras ASCII, números, '.', '_' ou '-'")
    if args.limite is not None and args.limite <= 0:
        parser.error("--limite deve ser positivo")
    load_dotenv(RAIZ_REPO / ".env")
    try:
        config = configuracao()
        ids = le_ids(args.arquivo_ids, args.limite)
    except (OSError, ValueError, UnicodeError) as erro:
        parser.error(str(erro))
    pasta = PASTA_OUTPUTS / args.alias
    try:
        pasta.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error(f"A pasta de saída já existe: {pasta}. Escolha outro --alias.")
    except OSError as erro:
        print(f"Não foi possível criar a pasta de saída: {erro}", file=sys.stderr)
        return 1
    try:
        with log_da_coleta(pasta) as log:
            return executa_coleta(args.tipo, ids, config, pasta, log)
    except OSError as erro:
        print(f"Falha de escrita ou leitura durante a coleta: {erro}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

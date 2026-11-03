# Coletor de contratos anuais do Contratos.gov.br

Data: 2026-10-28. Status: implementado. Revisão 1 (2026-10-28): subpasta por ano, nome com data e hora do
download e remoção de `--sobrescrever`. Revisão 2 (2026-10-30): parâmetro `--coleta` com os tipos anual,
mensal e diário; ver a seção "Tipos de coleta".

## Objetivo

Baixar, para um ano informado, o arquivo anual de contratos publicado pela SEGES em
`https://repositorio.dados.gov.br/seges/comprasnet_contratos/` e salvá-lo exatamente
como veio. O foco analítico é o GDF, mas recorte, tratamento e cruzamento com o PNCP
ficam em outro repositório. Este coletor não filtra, não converte e não reescreve dados.

## Contexto verificado em 2026-10-28

- Os arquivos anuais seguem o padrão
  `anual/{ano}/comprasnet-contratos-anual-contratos-{ano}.csv` de 2021 a 2026.
  Um ano inexistente responde HTTP 404.
- Cada anual contém os contratos criados ou alterados naquele ano, não os assinados
  nele. O anual de 2026 tem 207.323 linhas, com assinaturas de 2019 a 2026, e nenhum
  `id` repetido dentro do arquivo. Um mesmo contrato pode aparecer em anos diferentes.
- Os anuais de 2025 (Last-Modified 06/08/2025) e de 2026 (10/07/2026) estão
  congelados e incompletos. Os de 2021 a 2024 foram fechados em 1º de janeiro do ano seguinte.
- Tamanhos: de 57 MB (2022) a 165 MB (2025). O servidor envia `Content-Length` e
  `Content-Type: application/octet-stream`.
- O GDF aparece como esfera "Estadual", com `orgao_nome` "DISTRITO FEDERAL" ou
  "FUNDO CONSTITUCIONAL DO DISTRITO FEDERAL" (cerca de 450 linhas em 2026). Registrado
  apenas como informação para o repositório de análise.

## Interface

```text
python src/coletores/coleta_contratos.py --ano 2026
bash src/coletores/run-coleta-contratos.sh --ano 2026
```

- `--ano`: obrigatório, quatro dígitos ASCII (`[0-9]{4}`; `\d` aceitaria dígitos Unicode). A existência do ano é decidida pela
  fonte (404), não por uma lista fixa no código.
- O wrapper Bash repassa os argumentos, preferindo o Python do `.venv` do repositório,
  e funciona de qualquer diretório.

## Tipos de coleta (revisão 2)

Pedido registrado no `fluxo-coletor-contratos.excalidraw`: antes dos parâmetros do período, informar o
tipo de coleta. Ficou decidido usar um comando único, com o tipo primeiro, e não duas chamadas separadas,
para não depender de estado guardado entre execuções.

```text
python src/coletores/coleta_contratos.py --coleta anual  --ano 2021
python src/coletores/coleta_contratos.py --coleta mensal --ano 2026 --mes 07
python src/coletores/coleta_contratos.py --coleta diario --ano 2026 --mes 07 --dia 09
```

- `--coleta`: obrigatório, `anual`, `mensal` ou `diario`, igual ao nome da pasta na fonte.
- Anual exige `--ano`; mensal, `--ano` e `--mes`; diário, `--ano`, `--mes` e `--dia`. Parâmetro faltando ou
  sobrando encerra com código 2 antes de acessar a fonte.
- `--mes` (1 a 12) e `--dia` (1 a 31) aceitam um ou dois dígitos ASCII e são completados com zero. No
  diário, a data precisa existir no calendário.
- Padrões na fonte, verificados em 2026-10-30:
  `mensal/{ano}/{mes}/comprasnet-contratos-mensal-contratos-{ano}-{mes}.csv` e
  `diario/{ano}/{mes}/{dia}/comprasnet-contratos-diario-contratos-{ano}-{mes}-{dia}.csv`. Período não
  publicado responde 404 (ex.: mensal 2025-11).
- Saída em `outputs/contratos/{coleta}/{ano}[/{mes}[/{dia}]]/{AAAAMMDD}_{HHMMSS}_contratos_{coleta}_{período}.csv`,
  com período `2026`, `2026-07` ou `2026-07-09`. A saída do anual não muda.
- Download, retentativas, `.part`, conferência do `Content-Length` e códigos de saída valem igualmente para
  os três tipos.

As seções abaixo descrevem a versão anual original; onde dizem "ano", leia "período".

## Configuração

Lida do ambiente ou de `.env` na raiz (o ambiente tem precedência), com `.env.example`
atualizado:

| Variável | Padrão | Uso |
| --- | --- | --- |
| `REPOSITORIO_BASE_URL` | `https://repositorio.dados.gov.br/seges/comprasnet_contratos` | Base das URLs; HTTP(S), sem query ou fragmento. |
| `DOWNLOAD_TIMEOUT_SECONDS` | `60` | Timeout positivo de conexão e de leitura entre blocos, por tentativa. |

As variáveis `COMPRAS_API_BASE_URL`, `API_TIMEOUT_SECONDS` e `API_INTERVAL_SECONDS` saem
do `.env.example`, pois pertenciam ao coletor por API.

## Saída

`outputs/contratos/anual/{ano}/{AAAAMMDD}_{HHMMSS}_contratos_anual_{ano}.csv`, na raiz do
repositório, independentemente do diretório de execução, por exemplo
`outputs/contratos/anual/2021/20261028_102915_contratos_anual_2021.csv`. Data e hora são as
do início do download, no horário local, em ordem ano, mês, dia para que a ordem
alfabética seja a cronológica. Cada execução gera um arquivo novo; nada é substituído.
`outputs/` entra no `.gitignore`. O conteúdo é gravado byte a byte como recebido, e o
pedido é feito com `Accept-Encoding: identity`.

## Fluxo

1. Valida argumentos e configuração; monta a URL e o destino.
2. Se já existe arquivo com o mesmo nome (mesmo segundo), encerra com código 2.
3. GET em streaming (`stream=True`), sessão com até 8 retentativas para falhas de conexão
   e HTTP 429, 500, 502, 503 e 504, backoff fator 2 e respeito a `Retry-After`.
4. HTTP 404: encerra com código 2 e mensagem de ano não publicado. Outro status diferente
   de 200: código 1.
5. Grava em blocos de 1 MiB em `{destino}.part` na mesma pasta.
6. Se houver `Content-Length` e o total gravado for diferente, descarta o `.part` e
   encerra com código 1.
7. Substitui o destino pelo `.part` de forma atômica (`Path.replace`). Em qualquer falha,
   o `.part` é removido.
8. Registra na saída padrão: URL, `Last-Modified` da fonte, bytes gravados, duração e caminho.

## Códigos de saída

| Código | Situação |
| --- | --- |
| 0 | Arquivo salvo. |
| 1 | Falha de rede ou HTTP (exceto 404), download truncado, erro de escrita. |
| 2 | Argumento ou configuração inválidos, ano não publicado (404), arquivo com o mesmo nome já existente. |


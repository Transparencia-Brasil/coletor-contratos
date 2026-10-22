# coletor-contratos

Coleta respostas originais da API de contratos do Compras.gov.br, por uma lista
de identificadores. Salva um JSON por consulta e arquivos operacionais de
monitoramento, erros e log. O conteúdo dos contratos não é transformado,
filtrado ou convertido para tabelas.

Criado a partir do [python-template-tb](https://github.com/Transparencia-Brasil/python-template-tb),
seguindo a organização de [coletor-contratacoes](https://github.com/Transparencia-Brasil/coletor-contratacoes/pull/1).

## Fonte e abrangência

- [Swagger — contratos por identificador](https://dadosabertos.compras.gov.br/swagger-ui/index.html#/09%20-%20CONTRATOS/consultarContratos_Id).
- [Especificação OpenAPI](https://dadosabertos.compras.gov.br/v3/api-docs).
- `GET https://dadosabertos.compras.gov.br/modulo-contratos/1.1_consultarContratos_Id`.
- Parâmetros obrigatórios: `tipo` (`idCompra` ou `numeroControlePncpContrato`) e `codigo`.

O endpoint não exige autenticação e não expõe parâmetros de paginação. Cada ID
gera uma consulta, e o coletor guarda a resposta completa recebida, incluindo
`resultado`, `totalRegistros`, `totalPaginas`, `paginasRestantes` e eventuais
novos campos. Ele não garante a completude da base da API nem procura contratos
adicionais para uma compra.

Os IDs devem ser fornecidos por quem executa a coleta. O programa não descobre
IDs, não consulta itens, não restringe UF ou esfera e não depende de outro
repositório para funcionar. Um mesmo tipo deve ser usado para todos os IDs de
uma execução.

## Instalação

Requer Python **3.10 ou superior**. Na raiz do repositório:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

No Linux/macOS, ative o ambiente com `source .venv/bin/activate`. Use `python3`
no lugar de `python` se necessário.

## Entrada e execução

Crie um TXT UTF-8 com um ID por linha, por exemplo `inputs/ids-compras.txt`:

```text
15200505900212025
```

Linhas vazias são ignoradas; espaços nas extremidades da linha são removidos.
Os códigos permanecem como texto: zeros à esquerda, ordem e repetições são
preservados. UTF-8 com BOM também é aceito. O diretório `inputs/` é ignorado
pelo Git.

```powershell
python src/ETL/coletores/coleta_contratos.py --tipo idCompra --arquivo-ids inputs/ids-compras.txt --alias compras-exemplo
```

Para consultar por número de controle do contrato no PNCP, crie outro arquivo,
por exemplo `inputs/ids-pncp.txt`:

```text
00394445000101-2-000005/2026
```

```powershell
python src/ETL/coletores/coleta_contratos.py --tipo numeroControlePncpContrato --arquivo-ids inputs/ids-pncp.txt --alias pncp-exemplo
```

`--tipo`, `--arquivo-ids` e `--alias` são obrigatórios. `--limite N` consulta
apenas os primeiros N IDs não vazios, útil para uma verificação pequena.
O alias deve começar com letra ASCII ou número e conter somente letras ASCII,
números, ponto, sublinhado ou hífen. Uma pasta de saída existente é recusada;
use um novo alias para repetir uma coleta.

O caminho do arquivo de entrada é relativo à pasta corrente, ou pode ser
absoluto. As saídas ficam sempre no repositório, independentemente da pasta de
onde o programa é chamado. Para executar por Bash, use os mesmos argumentos:

```bash
bash src/ETL/coletores/run-coleta-contratos.sh --tipo idCompra --arquivo-ids inputs/ids-compras.txt --alias compras-bash
```

O wrapper funciona também quando chamado por seu caminho absoluto e utiliza
o Python do `.venv` do repositório quando disponível.

## Saídas

Cada execução cria `src/ETL/outputs/<alias>/`, ignorado pelo Git:

| Arquivo | Conteúdo |
| --- | --- |
| `000001.json`, `000002.json`, ... | Corpo original de cada resposta HTTP 200 com JSON válido, sem reserialização. |
| `monitoramento.csv` | Uma linha por consulta: sequência, linha da entrada, tipo, código, URL, status HTTP, início/fim em UTC, duração, resultado e arquivo. |
| `erros.csv` | Consultas que falharam, com identificação, status HTTP quando disponível e mensagem. |
| `coleta.log` | Progresso e falhas da execução. |

A numeração dos JSONs corresponde à sequência de consultas, não ao código do
contrato, evitando problemas com barras dos números PNCP e preservando consultas
repetidas. Falhas ou HTTP 204 podem deixar lacunas nessa sequência.

Uma resposta HTTP 200 com `resultado: []` é preservada e registrada como
`sem_dados`. HTTP 204 também é `sem_dados`, mas não gera JSON. Os demais sucessos
são `ok`; erros HTTP, falhas de conexão, JSON inválido e falhas de escrita são
registrados como `falha`. Uma falha individual não impede os próximos IDs.
A gravação usa um arquivo temporário e substituição atômica, evitando publicar
JSON parcial.

O código de saída é **0** quando todas as consultas terminam em `ok` ou
`sem_dados`, **1** quando a coleta tem alguma falha e **2** para argumentos,
configuração ou entrada inválidos, inclusive alias já existente. Interrupções
podem deixar uma execução incompleta; os arquivos já concluídos são preservados.
Não há retomada automática: execute novamente com outro alias.

## Configuração HTTP

As variáveis podem ser definidas no ambiente ou em `.env` na raiz do
repositório, usando `.env.example` como referência. O ambiente tem precedência.
O `.env` não é versionado.

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `COMPRAS_API_BASE_URL` | `https://dadosabertos.compras.gov.br` | URL base da API, sem query ou fragmento. |
| `API_TIMEOUT_SECONDS` | `30` | Timeout positivo de conexão/leitura por tentativa, em segundos. |
| `API_INTERVAL_SECONDS` | `1` | Espera entre consultas de IDs; aceita zero. |

GETs têm até oito novas tentativas para falhas transitórias de transporte e
HTTP **429, 500, 502, 503 e 504**, com espera crescente de fator 2 e respeito
ao cabeçalho `Retry-After`. O timeout é por tentativa, não para toda a execução.
Outros erros HTTP não são repetidos. Falhas ao ler o corpo após o recebimento
dos cabeçalhos podem ser registradas diretamente pelo `requests`, sem nova
tentativa automática.

## Testes

```powershell
python -m unittest discover -s tests -v
```

Os testes usam respostas simuladas e um servidor HTTP local para validar as
retentativas. Não consultam a API pública. Cobrem preservação dos bytes e
decimais, ambos os tipos de ID, entradas, respostas vazias, falhas individuais,
proteção de saídas e gravação atômica.

# coletor-contratos

Baixa o arquivo anual, mensal ou diário de contratos do Contratos.gov.br
publicado pela SEGES em dados abertos, para o período informado, e o salva
exatamente como foi recebido.
O conteúdo não é filtrado, convertido nem reescrito: tratamento, recortes (como
o do GDF) e cruzamento com as compras do PNCP ficam em outro repositório.

Criado a partir do [python-template-tb](https://github.com/Transparencia-Brasil/python-template-tb).

## Fonte e abrangência

- Repositório: <https://repositorio.dados.gov.br/seges/comprasnet_contratos/>.
- Dicionário de dados: `dicionário-de-dados.odt`, na mesma pasta.
- Arquivos baixados, conforme o tipo de coleta:

| Coleta | Arquivo na fonte |
| --- | --- |
| `anual` | `anual/{ano}/comprasnet-contratos-anual-contratos-{ano}.csv` |
| `mensal` | `mensal/{ano}/{mes}/comprasnet-contratos-mensal-contratos-{ano}-{mes}.csv` |
| `diario` | `diario/{ano}/{mes}/{dia}/comprasnet-contratos-diario-contratos-{ano}-{mes}-{dia}.csv` |

O anual é a carga completa do ano. Segundo o dicionário de dados, o mensal e o
diário trazem só os registros novos ou alterados no mês ou no dia.

Pontos verificados na fonte em 28/10/2026:

- **O ano é o da atualização do registro, não o da assinatura.** Cada arquivo
  anual traz os contratos criados ou alterados naquele ano. O de 2026 tem
  contratos assinados desde 2019. Não há `id` repetido dentro de um arquivo, mas
  o mesmo contrato pode aparecer em anos diferentes.
- **Há arquivos anuais de 2021 a 2026, e mensais e diários desde 10/2021.**
  Os mensais e diários têm lacunas (faltam, por exemplo, 11/2022 e set a
  dez/2025 no mensal). Um período não publicado responde HTTP 404.
- **Os anuais de 2025 e 2026 estão incompletos.** A publicação parou em
  06/08/2025 e em 10/07/2026, respectivamente. Os de 2021 a 2024 foram fechados
  no início do ano seguinte. A data de cada arquivo aparece no log como
  `Last-Modified`.
- **Os arquivos têm de 57 MB a 165 MB.** Usam UTF-8 e vírgula como separador.
- **Só aparecem órgãos que usam o Contratos.gov.br.** Órgãos que não usam o
  sistema, como a maior parte do GDF, não aparecem.

As demais tabelas (itens, históricos, empenhos etc.), os arquivos `-latest.csv`
e a retomada de downloads interrompidos não fazem parte deste coletor.

## Instalação

Requer Python **3.10 ou superior**. Na raiz do repositório:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

No Linux/macOS, ative o ambiente com `source .venv/bin/activate`. Use `python3`
no lugar de `python` se necessário.

## Execução

O comando recebe primeiro o tipo de coleta, em `--coleta`, e depois os
parâmetros que esse tipo exige:

```powershell
python src/coletores/coleta_contratos.py --coleta anual --ano 2026
python src/coletores/coleta_contratos.py --coleta mensal --ano 2026 --mes 07
python src/coletores/coleta_contratos.py --coleta diario --ano 2026 --mes 07 --dia 09
```

| Coleta | Parâmetros exigidos |
| --- | --- |
| `anual` | `--ano` |
| `mensal` | `--ano` e `--mes` |
| `diario` | `--ano`, `--mes` e `--dia` |

`--coleta` é obrigatório e aceita `anual`, `mensal` ou `diario` (sem acento,
como a pasta da fonte). `--ano` tem quatro dígitos; `--mes` vai de 1 a 12 e
`--dia` de 1 a 31, com ou sem zero à esquerda (`7` e `07` são equivalentes). A
coleta é recusada, antes de acessar a fonte, se faltar um parâmetro exigido, se
sobrar um que o tipo não usa (como `--mes` numa coleta anual) ou se a data
diária não existir no calendário (como 30/02). Se o período foi publicado, quem
decide é a fonte.

Para executar pelo Bash, de qualquer diretório, use os mesmos argumentos:

```bash
bash src/coletores/run-coleta-contratos.sh --coleta mensal --ano 2026 --mes 07
```

O wrapper usa o Python do `.venv` do repositório quando ele existe; caso
contrário, usa `python3` ou `python` do sistema. Funciona no Git Bash e no WSL:
no WSL, com o `.venv` criado no Windows, o caminho do script é convertido com
`wslpath` para que o `python.exe` consiga abri-lo. Nesse caso, prefira
`--saida` relativo, porque o Python do Windows não entende caminhos `/mnt/c/...`.

## Saída

Cada download gera um arquivo novo em `outputs/contratos/`, na raiz do
repositório, qualquer que seja o diretório de execução, numa subpasta que repete
a da fonte: tipo de coleta, ano, mês e dia. As pastas são criadas se não
existirem e são ignoradas pelo Git.

```text
outputs/contratos/anual/{ano}/{AAAAMMDD}_{HHMMSS}_contratos_anual_{ano}.csv
outputs/contratos/mensal/{ano}/{mes}/{AAAAMMDD}_{HHMMSS}_contratos_mensal_{ano}-{mes}.csv
outputs/contratos/diario/{ano}/{mes}/{dia}/{AAAAMMDD}_{HHMMSS}_contratos_diario_{ano}-{mes}-{dia}.csv

outputs/contratos/anual/2021/20261028_102915_contratos_anual_2021.csv
outputs/contratos/mensal/2026/07/20261028_102915_contratos_mensal_2026-07.csv
outputs/contratos/diario/2026/07/09/20261028_102915_contratos_diario_2026-07-09.csv
```

A data e a hora são as do início do download, no horário local da máquina, e
vêm no formato ano, mês, dia, para que a ordem alfabética dos arquivos seja a
cronológica. Elas indicam quando o arquivo foi baixado, não a versão dos dados:
a data de publicação na fonte aparece no log como `Last-Modified`.

Downloads anteriores nunca são substituídos. Como cada execução baixa o arquivo
inteiro de novo (no anual, de 57 MB a 165 MB), as cópias se acumulam na pasta do
período até serem removidas manualmente. Se já existir um arquivo com o mesmo nome (duas
execuções no mesmo segundo), a coleta é recusada.

### Escolher o arquivo de saída

O parâmetro opcional `--saida` substitui o caminho automático pelo caminho
completo do CSV, o que permite a outro programa saber de antemão onde o
arquivo vai ficar:

```bash
bash /caminho/para/coletor-contratos/src/coletores/run-coleta-contratos.sh \
  --coleta mensal --ano 2026 --mes 07 \
  --saida tasks/coleta-contratos/outputs/mensal/2026/07/contratos_mensal_2026-07.csv
```

Um caminho relativo é resolvido a partir do diretório de onde o comando é
executado, e não da raiz deste repositório. As pastas são criadas se não
existirem. A regra de não substituir vale também aqui: se o caminho já existir,
seja arquivo ou pasta, a coleta é recusada com código 2, sem acessar a fonte.

Quem chama o coletor de outro repositório usa o `.venv` e o `.env` deste
repositório; variáveis definidas no ambiente de quem chama têm precedência
sobre o `.env`.

O download é feito em streaming, em blocos de 1 MiB, para um arquivo `.part` na
mesma pasta. O nome final só é usado depois que o arquivo chega inteiro e o total
recebido confere com o `Content-Length` informado pela fonte. Em caso de falha
ou interrupção, o `.part` é removido.

O log vai para a saída padrão, inclusive os erros, e registra apenas o início e o
fim da coleta, sem progresso durante o download. Ao final, mostra a URL, o caminho
salvo, os bytes gravados, o `Content-Length`, o `Last-Modified` da fonte e a
duração. O arquivo é pedido sem compressão (`Accept-Encoding: identity`), para
que o conteúdo salvo seja exatamente o publicado.

## Códigos de saída

| Código | Situação |
| --- | --- |
| **0** | Arquivo salvo. |
| **1** | Falha de rede, HTTP diferente de 200 e 404, download incompleto ou erro de escrita. |
| **2** | Argumento ou configuração inválidos, período não publicado (404) ou destino já existente (caminho de `--saida` ocupado ou arquivo com o mesmo nome). |

## Configuração

As variáveis podem ser definidas no ambiente ou num `.env` na raiz do
repositório, usando `.env.example` como referência. O ambiente tem precedência.
O `.env` não é versionado.

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `REPOSITORIO_BASE_URL` | `https://repositorio.dados.gov.br/seges/comprasnet_contratos` | URL base da pasta de contratos, HTTP(S), sem query ou fragmento. |
| `DOWNLOAD_TIMEOUT_SECONDS` | `60` | Timeout positivo, por tentativa, para conectar e entre leituras sucessivas da resposta. |

Falhas de conexão e respostas HTTP **429, 500, 502, 503 e 504** são repetidas
até oito vezes, com espera crescente de fator 2 e respeito ao cabeçalho
`Retry-After`. Outros erros HTTP não são repetidos. Com a fonte fora do ar, as
retentativas somam cerca de 6 minutos de espera sem mensagens no log antes de
a coleta terminar com código 1.

## Testes

```powershell
python -m unittest discover -s tests -v
```

Os testes usam um servidor HTTP local e não acessam o repositório público.
Cobrem a preservação byte a byte do arquivo, a montagem da URL e do destino nos
três tipos de coleta, arquivos maiores que um bloco, ausência de `Content-Length`,
404 e outros status, downloads truncados, retentativa após 503, interrupção, nome
com data e hora, recusa de nome já existente, validação dos argumentos de cada
tipo de coleta (parâmetros faltando ou sobrando, mês, dia e datas inexistentes),
`--saida` com caminho absoluto, relativo e já ocupado, validação da configuração
e o wrapper Bash, inclusive chamado de outro diretório com `--saida` relativo.

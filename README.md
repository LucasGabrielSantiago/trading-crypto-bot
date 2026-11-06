# Bot de trading (MVP em testnet)

Bot spot para pares em USDT em Python com `ccxt`. Este MVP só opera na Spot Testnet da Binance, com
saldo fictício: não existe configuração para modo real, e o bot para se as rotas não forem da testnet.

A estratégia de médias móveis é um exemplo para testar o sistema, não uma recomendação de investimento.

## Jeito mais fácil

Instale o Python 3.10 ou mais novo. Depois, dê dois cliques em `Iniciar.bat` no Windows ou rode
`./iniciar.sh` no Linux ou Mac. O bot instala o que falta e abre a interface no navegador:

1. **Conectar:** cole as chaves da testnet.
2. **Estratégia e risco:** ajuste os controles, veja no gráfico e teste.
3. **Painel:** acompanhe, pause, desligue ou use "Parar e vender".

Fechar a janela do bot o desliga, mas uma posição aberta continua com o OCO na exchange. A interface
só aceita conexões deste computador. As chaves ficam no `.env`, e os ajustes em `settings.json`,
que tem prioridade sobre o `.env`.

Ao passar o projeto para alguém, envie sem `.env`, `settings.json`, `bot.db` e `.venv`. Cada pessoa
usa as próprias chaves de teste.

## Rodar com Docker

Dê dois cliques em `Iniciar-Docker.bat`. Ele instala o Docker Desktop com sua confirmação, abre o Docker
se estiver fechado, cria o `.env` com uma senha aleatória, sobe os contêineres e abre a interface.
Depois de instalar o Docker pela primeira vez, pode ser preciso reiniciar o Windows.

Pelo terminal, com o Docker aberto:

```bash
docker compose up -d --build
```

Depois, abra http://127.0.0.1:8766. Sem Docker, a interface usa a porta 8765. O app e o PostgreSQL ficam ligados em segundo plano e voltam
sozinhos ao reiniciar o Docker. O bot religa se estava ligado, com a mesma configuração. Se você o
desligou ou se todos os pares pararam, ele continua desligado.

```bash
docker compose logs -f bot      # registros
docker compose down             # desliga e mantém os dados
docker compose down -v          # apaga banco, posições e configurações
```

Antes do primeiro início, defina `POSTGRES_PASSWORD` no `.env`, só com letras e números. Depois de criar o
banco, trocar esse valor não muda a senha dele.

As portas só aceitam conexões deste computador. Para abrir o banco em outra ferramenta, use
`127.0.0.1:5433`, banco `trading`, usuário `bot` e a senha `POSTGRES_PASSWORD`.
Faça cópia de segurança com:

```bash
docker compose exec postgres pg_dump -U bot trading > backup-trading.sql
```

## Histórico

Cada operação fechada fica em `trades`, com datas, preços, taxas, resultado, motivo da saída e os
parâmetros usados. Cada teste fica em `validation_runs`. O registro é gravado primeiro no arquivo do
par e enviado ao banco, então uma queda do banco não perde a operação nem para o bot. Fora do Docker,
o histórico fica em `trades.db`.

## Instalação pelo terminal

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # Windows: copy .env.example .env
```

Gere as chaves em https://testnet.binance.vision e preencha o `.env`.

## Uso

```bash
pytest                                # testes
python main.py ui                     # abre a interface
python main.py validate               # valida a estratégia em períodos separados
python main.py backtest               # backtest com candles reais, sem chave
python main.py backtest --synthetic   # dados simulados, sem internet
python main.py backtest --csv dados.csv
python main.py check                  # valida chaves, saldo e preço na testnet
python main.py run                    # inicia o bot com o painel
python main.py run --no-panel         # inicia com logs no terminal
```

Com o painel, os logs ficam em `bot.log`. O CSV precisa das colunas `timestamp` (em ms), `open`,
`high`, `low`, `close` e `volume`.

Para uma parada de emergência, crie um arquivo chamado `STOP` na pasta do bot. Ele cancela o OCO,
vende a posição e encerra. Apague o arquivo antes de iniciar de novo. Com Ctrl+C, o bot fecha, mas a
posição e o OCO continuam na exchange.

## Vários pares

Escolha até 8 pares em USDT na interface ou em `SYMBOLS` no `.env`. O capital é dividido igualmente,
e cada par tem bot, limites e arquivo de dados próprios (`bot_BTCUSDT.db`, por exemplo). Se um par
parar, os outros continuam. Pares sem USDT, como ETH/BTC, ainda não são aceitos.

## Como funciona

- **Estratégia `ranges`:** a resistência é a maior máxima e o suporte é a menor mínima dos últimos
  `LOOKBACK` candles fechados. O bot compra perto do suporte, depois de um candle de alta, quando o
  alvo paga pelo menos `MIN_REWARD_RISK` vezes o risco. Não compra se o suporte foi rompido.
- **Saída com OCO:** depois da compra, envia à exchange um alvo perto da resistência e um stop
  abaixo do suporte. Se o preço passar direto pelo stop limitado, o bot vende a mercado.
- **Sem proteção, sem posição:** se o OCO for recusado, o bot vende a posição e para.
- **Tamanho:** arrisca 1% do capital até o stop, usando no máximo 25% do capital por posição.
- **Capital:** `CAPITAL_USDT` limita o total do bot. Ele só vende o que ele mesmo comprou.
- **Perda diária:** ao perder 3% no dia (UTC), vende a posição e para.
- **Recuperação:** ordens e posição ficam em `bot.db`. Se a resposta de uma ordem ou de um OCO se
  perder, o bot para e confere na exchange ao reiniciar.
- **Backtest:** executa o sinal na abertura seguinte, com taxa de 0,1% e slippage de 0,05%. Se o stop e o
  alvo cabem no mesmo candle, conta o stop.
- **Validação:** escolhe os parâmetros nos primeiros 70% dos dados e confirma nos 30% finais. Para
  ser aprovada, a estratégia precisa de 20 operações, lucro e resultado melhor que segurar BTC com a
  mesma exposição média. A aprovação não garante lucro futuro.

Para usar as médias móveis, defina `STRATEGY=sma` no `.env`.

## Limitações

- O stop OCO é limitado. Com o bot desligado, uma queda muito rápida pode passar por ele sem executar.
- Os limites são por par. Se várias criptos caírem juntas, as perdas se somam até o limite total.
- Os preços e a liquidez da testnet não representam o mercado real, e ela pode sair do ar.
- A testnet é zerada cerca de uma vez por mês, sem aviso. Se o bot parar avisando que o OCO sumiu ou
  que o saldo não cobre a posição, confira a testnet e apague `bot.db` antes de ligar de novo.
- Taxas pagas em BNB não entram no lucro calculado pelo bot.
- Um sinal é perdido se o bot cair entre registrar o candle e enviar a ordem. É uma escolha para
  evitar ordem duplicada.
- Depois de parar pelo limite diário, um reinício no mesmo dia (UTC) para de novo. No dia seguinte, volta a operar.

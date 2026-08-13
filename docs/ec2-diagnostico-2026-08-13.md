# Diagnostico read-only da EC2 - 2026-08-13

Janela analisada: de 2026-08-13 00:53 UTC ate 12:25 UTC. Esta analise nao
reiniciou containers, removeu dados, alterou cron ou mudou configuracoes do host.

## Resumo executivo

O OneLog teve uma falha operacional real durante a madrugada, mas nao foi a
fonte dos picos de CPU vistos na coleta ao vivo. A solicitacao que excedeu o
limite do worker permaneceu em `inflight` e so voltou a ser processada quando
um deploy do OneLog reiniciou o worker. Esse e um defeito de recuperacao de
tarefa, corrigido neste conjunto de alteracoes.

No momento da coleta, o worker do OneLog consumia 0,09% de CPU e 97,51 MiB de
memoria; a API consumia 0,01% e 117,4 MiB. Nao houve evento de OOM do kernel na
janela analisada. O OneLog nao explica, sozinho, a carga do host.

## Estado do host na coleta

| Indicador | Valor observado |
| --- | --- |
| Host | `ip-172-31-44-220`, 4 vCPU, 15,8 GiB RAM |
| Load average | 9,59 / 8,65 / 7,55 |
| Memoria disponivel | 8,8 GiB |
| Swap em uso | 3,1 GiB de 4 GiB |
| Disco raiz | 94 GiB de 116 GiB, 82% |
| Containers ativos | 57 |
| Zumbis | 64 |
| Imagens Docker recuperaveis | 21,86 GiB |

O `sar` entre 00:50 e 12:10 UTC mostrou media de CPU ocupada de 42,69%, com
amostras de dez em dez minutos. Essa serie nao captura picos de segundos, mas
nao mostra saturacao sustentada causada pelo OneLog.

## Principais consumidores na coleta

Os maiores consumidores instantaneos nao pertenciam ao projeto OneLog:

| Container/processo | CPU observada | Observacao |
| --- | --- | --- |
| `monitor-j44...` | 76,20% | Projeto `j44cco0kws0s4848osssws8k` |
| `db-j44...` | 44,35% | PostgreSQL do mesmo projeto |
| `coletor-j44...` | 33,25% | Mesmo projeto |
| `coolify-redis` | 19,95% | Infraestrutura Coolify |
| `api-u800...` | 10,73% | Projeto `u800go80wk8kccg4sswowg08`, com quatro processos Python de aproximadamente 290-314 MiB cada |
| Chrome do OneSID | cerca de 21-23% por processo | Navegador em `/tmp/onesid-rpa-chrome-*` |
| OneLog worker | 0,09% | 97,51 MiB, um worker ativo |
| OneLog API | 0,01% | 117,4 MiB |

Os picos precisam ser investigados prioritariamente nos projetos `j44...`,
`u800...`, OneSID e na infraestrutura compartilhada. Nenhuma acao nesses
projetos foi tomada nesta intervencao.

## Linha do tempo do OneLog

1. Em tentativas anteriores, desafios Cloudflare deixaram o browser preso ate
o limite absoluto de 480 segundos.
2. O supervisor encerrou o processo para proteger a frota, aplicou cooldown e
reiniciou o worker.
3. A tarefa ja retirada do Redis ficou em `inflight`; o caminho de timeout nao
a devolvia nem a agendava. Ela so voltou no boot seguinte, quando o worker
executou `recover_all_inflight_tasks()`.
4. O deploy das 11:32 UTC recuperou uma tarefa em voo. O worker voltou a
concluir logins, com sucessos registrados as 11:39, 12:03, 12:28 e 12:51 UTC.

O observador de `/home/ubuntu/scripts/onelog-watchdog-observe.sh` esta em modo
passivo, executado pelo cron a cada cinco minutos. O script destrutivo antigo
permanece comentado e nao reiniciou containers.

## Riscos do OneLog identificados

- O volume `shared_data` ocupa 3,8 GiB e contem 90.384 PNGs historicos.
- Os logs compartilhados eram append-only e o painel lia o arquivo completo
  para cada atualizacao de logs.
- O worker nao possuia limite de PIDs explicito, apesar de usar `init: true`.
- Extensoes legadas consultam `status` a cada dois segundos durante espera.

## Acoes aplicadas no repositorio

- Fila persistente de retries adiados para tarefas interrompidas ou falhas
  temporarias de Cloudflare.
- Recuperacao no caminho do timeout do supervisor, sem depender de deploy.
- Limite padrao de duas armadilhas Cloudflare por tarefa e retry de cinco
  minutos, evitando abrir Chrome repetidamente sem perspectiva de sucesso.
- Rotacao de logs, leitor ao vivo por tail limitado e amostragem de eventos de
  status; contadores diarios continuam exatos.
- Limite de 256 PIDs para o worker e compatibilidade do observador com retries
  adiados.

## Acoes recomendadas para a equipe de infraestrutura

Estas acoes nao foram executadas:

1. Confirmar o proprietario e o comportamento do projeto `j44...`; o conjunto
   monitor/coletor/PostgreSQL foi o maior consumidor na amostra ao vivo.
2. Revisar o projeto `u800...`, que executa Uvicorn com quatro filhos Python e
   consome cerca de 1,2 GiB somente nesses filhos.
3. Investigar o Chrome ativo do OneSID e definir limite de concorrencia e
   ciclo de encerramento.
4. Planejar limpeza controlada de imagens Docker recuperaveis e retenção de
   snapshots do OneLog. Fazer backup ou validar retenção antes de remover dados.
5. Após reduzir a pressão, avaliar reduzir o swap ocupado de forma planejada;
   nao executar `swapoff` em host sob carga.
6. Adicionar alertas por PSI de CPU, memoria e I/O, além de CPU por container.

## Validacao posterior ao deploy do OneLog

1. Confirmar `delayed_retries` no dashboard/API e que uma falha temporaria
   apareca como retry, nunca como fila perdida.
2. Confirmar log `Tarefa preservada para retry` em falha Cloudflare e
   `tarefa(s) de retry devolvida(s)` quando vencer o atraso.
3. Confirmar que `docker stats` do OneLog permanece abaixo dos limites e que
   PIDs do worker ficam abaixo de 256.
4. Acompanhar por 24 horas o tamanho do volume compartilhado e a taxa de
   crescimento dos logs rotacionados.

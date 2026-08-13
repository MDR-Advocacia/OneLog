# Capacidade da EC2 e governanca de recursos

Data da coleta: 2026-08-13, aproximadamente 13:20 a 13:40 UTC.

Escopo: leitura do host, Docker, Coolify, logs do kernel e bancos de dados do
Coolify. Nenhum container foi reiniciado, nenhuma configuracao foi alterada e
nenhum dado foi removido para produzir este documento.

## Decisao executiva

O host atual nao tem margem suficiente para a carga consolidada. Ele e uma AWS
`t3a.xlarge`, com 4 vCPU e 16 GiB de RAM, executando 57 containers, entre APIs,
bancos, filas, Coolify e ao menos tres familias de automacao com Chromium.

O OneLog nao era o maior consumidor na coleta. A versao de producao esta
contida em 1 vCPU e 1,5 GiB para o worker, e a API tem 0,5 vCPU e 400 MiB. No
instante observado, seu worker consumia 0,00% de CPU e cerca de 115 MiB. Picos
curtos de um Chrome durante login sao inevitaveis, mas o OneLog atual nao pode
explicar uma ocupacao global recorrente de 100% com os limites ja aplicados.

O problema e a soma de recursos sem quota. OneSid executa dois RPAs com Chrome,
Flow mantem quatro processos Uvicorn e tambem usa Playwright, Lake possui
Celery e PostgreSQL sem limite, e Atlas mantem outro ambiente de navegador.
Em 12/08 houve OOM global: o kernel matou um Chrome do OneSid. Isso confirma
pressao real de memoria compartilhada, nao apenas leitura imprecisa do Grafana.

Recomendacao: aplicar limites e controle de concorrencia por projeto agora e
aprovar aumento para uma instancia x86 nao burstable de 8 vCPU e 32 GiB, como
`m7a.2xlarge`, ou separar as automacoes em host dedicado. O upgrade isolado
alivia a situacao; quotas e filas controladas evitam que ela se repita.

## Ambiente confirmado

| Item | Observado |
| --- | --- |
| Host | `ip-172-31-44-220`, `i-03e9eef33989d0cda` |
| Zona | `us-east-2c` |
| Tipo | AWS `t3a.xlarge` |
| Capacidade | 4 vCPU, 15,8 GiB de RAM utilizavel |
| Plataforma | Ubuntu 24.04, Docker 29.1.3, cgroup v2 |
| Coolify | `4.0.0-beta.460` |
| Projeto / ambiente | `mdr-producao` / `production` |
| Containers ativos | 57 |
| Disco raiz | 95 GiB de 116 GiB usados (83%) |
| Imagens e cache recuperaveis | 21,6 GiB |
| Swap na coleta | 3,1 GiB de 4 GiB ocupados |
| Processos zumbis | 63 |

Uma `t3a.xlarge` tem baseline de 40% por vCPU, ou cerca de 1,6 vCPU de uso
sustentado sem creditos. A AWS documenta 4 vCPU, 16 GiB e 96 creditos/hora
para esse tipo: [T3a](https://aws.amazon.com/ec2/instance-types/t3/) e
[General purpose](https://docs.aws.amazon.com/ec2/latest/instancetypes/gp.html).
O perfil AWS do host nao tem permissao para consultar o modo de creditos ou as
metricas `CPUCreditBalance` e `CPUSurplus*`; essa permissao de leitura deve ser
concedida para fechar o acompanhamento financeiro e operacional.

## Pressao observada

### CPU

O `sar` entre 06:10 e 13:20 UTC mostrou media de 43,6% de CPU nao ociosa:
23,74% usuario, 12,68% `nice`, 3,60% sistema, 0,68% I/O wait e 2,89% steal.
Isso equivale a aproximadamente 1,74 vCPU em media, acima do baseline de 1,6
vCPU da familia T3a.

As amostras de 11:50, 12:00 e 12:10 UTC tiveram aproximadamente 77%, 70% e
74% de CPU nao ociosa. A fila de execucao chegou a 31, 34 e 37 processos, e o
load average atingiu 13,53. Durante a coleta ao vivo,
`/proc/pressure/cpu` marcou `some avg300=22,17`: tarefas aguardaram CPU em
cerca de um quinto do tempo nos cinco minutos anteriores.

O disco nao apresentou gargalo comparavel: media de 4,7% de uso e espera em
torno de 1,4 ms. Os picos sao essencialmente de CPU e memoria, nao de I/O.

### Memoria

Havia aproximadamente 7,5 GiB de memoria disponivel no instante final, mas o
swap permanecia em 3,1 GiB. Historicamente o swap ficou proximo de 100% por
parte significativa da madrugada/manha e somente caiu depois de reinicios e
deploys. Isso e evidencia de pressao anterior; nao significa que haja folga
segura apenas pela amostra atual.

Em 12/08 as 15:05 UTC o kernel registrou OOM global. O alocador era `postgres`
e a vitima selecionada foi `chrome` no cgroup `monitor` do OneSid. A conclusao
correta e que a memoria global se esgotou com varios projetos concorrendo. O
evento nao permite culpar isoladamente o PostgreSQL ou o Chrome.

### Disco

O disco nao causou os picos, mas 21 GiB livres e insuficiente como margem para
mais imagens, builds de Playwright/Chromium e crescimento de banco. Os volumes
de maior porte observados foram:

| Dado | Tamanho aproximado |
| --- | ---: |
| PostgreSQL do Lake | 12 GiB |
| Saida da API do Flow | 5,8 GiB |
| Dados compartilhados do OneLog | 3,8 GiB |
| Dados da API do Flow | 2,1 GiB |
| PostgreSQL do Flow | 2,0 GiB |
| Perfil Chrome do Atlas | 1,7 GiB |
| Logs do OneSid | 1,5 GiB |

O Coolify recomenda limpeza automatizada de imagens e build cache; volumes
precisam de retencao e backup antes de qualquer exclusao. Referencia:
[Automated cleanup](https://coolify.io/docs/knowledge-base/server/automated-cleanup).

## Inventario Coolify e revisoes

| Recurso | Repositorio | Branch | Revisao implantada relevante |
| --- | --- | --- | --- |
| OneLog, id 7 | `MDR-Advocacia/OneLog` | `main` | `01f45f1`, 13/08 13:11 UTC |
| Flow, id 14 | `MDR-Advocacia/TwoTask` | `main` | `66766ae`, 13/08 12:12 UTC |
| Atlas, id 16 | `MDR-Advocacia/Atlas` | `main` | imagem `0de2c95` |
| OneSid-Apex-I, id 19 | `MDR-Advocacia/OneSid-Apex-I` | `main` | recurso ativo atual |
| Lake, id 20 | `MDR-Advocacia/lake` | `main` | `853b81a`, 07/08 18:43 UTC |

O banco do Coolify mantem `HEAD` na configuracao dos recursos; os SHAs acima
foram confirmados pela fila de deploy e tags das imagens. Todos pertencem ao
projeto `mdr-producao`, ambiente `production`.

No incidente houve deploy OneLog que falhou das 14:57 as 15:18 UTC e deploys
Flow entre 15:44 e 15:56, depois outro as 16:19 UTC. O OOM foi as 15:05 UTC,
antes dos deploys bem-sucedidos do Flow. Builds e redeploys agravaram uma
maquina ja pressionada, mas nao explicam sozinhos o OOM.

## Atribuicao por projeto

### OneLog

Configuracao atual: `init: true`, `PidsLimit=256`, worker com 1,0 vCPU e
1.536 MiB, API com 0,5 vCPU e 400 MiB, PostgreSQL com 256 MiB e Redis com
128 MiB. A versao ativa e `01f45f1` do `main`.

O `tini` elimina a origem historica de zumbis do worker. Nenhum dos 63 zumbis
observados pertence ao OneLog atual; eles pertencem principalmente ao Lake e
ao proxy Coolify. O worker possui 202 periodos throttled, com 24,99 s de
throttling desde o deploy. Isso e consequencia esperada do teto de uma vCPU,
nao indicio de que ele esteja consumindo a CPU global.

O custo inevitavel e um Chrome durante cada login, com navegador, driver,
renderizacao e espera externa. O pre-aquecimento validado apos o deploy durou
aproximadamente 47 segundos. Esse trabalho foi deliberadamente limitado a um
nucleo e uma missao por vez.

Melhorias pendentes no projeto: criar healthcheck semantico para API e aplicar
retencao aos 90 mil PNGs historicos no volume compartilhado de 3,8 GiB.

### OneSid-Apex-I

E o principal candidato a intervencao. `monitor` e `processador` usam
`xvfb-run`, Chrome e `shm_size: 2gb`. Cada servico tem limite de 3 GiB e 768
PIDs, mas **nao tem limite de CPU**. Na coleta, o `monitor` oscilava entre 45%
e 76% de CPU, com 570-587 MiB e 125 PIDs; `processador` mantinha cerca de
360 MiB e 124 PIDs mesmo em menor atividade.

O OOM de 12/08 matou um Chrome exatamente no cgroup do `monitor`. Dois RPAs de
navegador livres para disputar CPU com OneLog, Atlas e os servicos web sao um
risco comprovado.

### Flow

A API do Flow roda Uvicorn com quatro filhos Python. Ela estava entre 1,2 GiB
e 1,62 GiB nas duas coletas, sem limite de CPU ou memoria; seu PostgreSQL
tambem nao possui quota. O historico de deploy confirma runners
Playwright/Xvfb no projeto. `init: true` foi incluido para zumbis de Chromium,
mas nao resolve a base alta de memoria nem a concorrencia de quatro workers em
um host com quatro vCPU para todos os projetos.

### Lake

O worker usa `celery -A core worker -l info`, sem `--concurrency` explicito e
sem limites de CPU, memoria ou PID. O processo atual tinha quatro filhos de
trabalho. Coleta anterior registrou pico de aproximadamente 240% e media de
118,7%; na coleta atual ele estava baixo, confirmando carga intermitente.

Ha um defeito objetivo no healthcheck, executado a cada 60 segundos:

```text
celery -A core inspect ping -d celery@$(hostname) --timeout 10 | grep -q pong
```

O PID 1 acumula pares zumbis `celery` e `grep`: 36 zumbis na coleta. Zumbis nao
gastam CPU diretamente, mas ocupam PIDs, poluem o monitoramento e revelam que
o check precisa ser refeito. O PostgreSQL Lake nao possui limite e e o maior
volume de dados, com 12 GiB.

### Atlas, Coolify e demais servicos

Atlas mantem API, worker, PostgreSQL, Xvfb/VNC e perfil Chrome sem limites de
CPU, memoria ou PID. Estava moderado na coleta, mas e outro RPA capaz de gerar
picos quando ativo. Traefik do Coolify tem 26 `wget` zumbis e `coolify-realtime`
reiniciou oito vezes desde 12/08. Chatwoot, n8n, bancos, Redis e outros recursos
tambem permanecem sem quota. O risco e cumulativo, nao de um unico container.

## Acoes que podem ocorrer sem upgrade imediato

O Compose e a fonte de verdade para limites de servico no Coolify. Referencias:
[Docker Compose](https://coolify.io/docs/knowledge-base/docker/compose) e
[Custom Compose Overrides](https://coolify.io/docs/knowledge-base/custom-compose-overrides).

| Prioridade | Dono | Ajuste | Resultado esperado |
| --- | --- | --- | --- |
| P0 | OneSid | CPU, memoria e PID por `monitor`/`processador`; um browser concorrente por servico | Impede monopolio de CPU e reduz OOM global |
| P0 | Lake | Fixar Celery em concorrencia 1 ou 2; trocar healthcheck que vaza zumbis; limitar worker, backend e DB | Converte pico em fila controlada e remove vazamento conhecido |
| P0 | Flow | Reduzir Uvicorn inicialmente de 4 para 2; limitar API e DB; separar jobs Playwright | Diminui base de 1,2-1,6 GiB e conflito com requests web |
| P1 | Atlas | Um browser por vez, quotas e retencao de perfis/downloads | Mantem automacao previsivel |
| P1 | Coolify/Docker | Cleanup de imagens/cache e rotacao de logs; nunca volumes sem backup | Recupera cerca de 21,6 GiB sem risco aos dados |
| P1 | Observabilidade | PSI, swap, OOM, PIDs, CPU por cgroup, filas e deploys | Identifica dono do proximo pico em minutos |
| P2 | Host | Avaliar `vm.swappiness=10` apenas em janela planejada | Reduz swap excessivo, mas nao cria RAM |

Limite de CPU nao elimina trabalho: ele transforma concorrencia caotica em fila
e latencia previsivel. Para Celery e RPA em host compartilhado, esse e o
comportamento desejado.

## O que e inevitavel

1. Chromium em RPA abre varios processos e usa memoria compartilhada. OneLog,
   OneSid, Atlas e Playwright sempre terao picos durante automacoes.
2. PostgreSQL e Redis usam cache de memoria. O problema e varias instancias sem
   orcamento compartilhando somente 16 GiB.
3. Builds de `npm`, Playwright e Chromium competem com producao no mesmo Docker
   daemon. Deploy pesado em horario comercial necessariamente aumenta disputa.
4. Picos curtos continuarao a existir. O objetivo e mante-los dentro de uma
   reserva, nao eliminar trabalho real por flags de navegador.

## Recomendacao de capacidade

### Opcao A: host unico mais robusto

Migrar de `t3a.xlarge` para **`m7a.2xlarge` (8 vCPU, 32 GiB)** ou equivalente
x86 nao burstable. Isso dobra CPU e memoria, elimina dependencia de creditos e
preserva compatibilidade com as imagens Chrome atuais. E o minimo recomendado
para os 57 containers enquanto as quotas ainda estao sendo regularizadas.

### Opcao B: separar automacoes

Manter aplicacoes web, bancos e Coolify no host atualizado e mover OneSid,
Atlas e OneLog para instancia x86 dedicada a RPA. `m7a.xlarge` pode atender
uma ou duas automacoes controladas; `m7a.2xlarge` oferece margem adequada para
Chrome e crescimento. Esta e a melhor opcao de longo prazo, pois um portal
externo lento ou captcha nao deve congelar banco, painel e deploys internos.

## Plano proposto

1. Aplicar os tres itens P0 em OneSid, Lake e Flow em deploys planejados.
2. Habilitar limpeza automatizada de imagens/cache e definir retencao por
   volume, especialmente screenshots OneLog, logs OneSid e artefatos Atlas.
3. Conceder leitura AWS para `DescribeInstanceCreditSpecifications` e
   `cloudwatch:GetMetricData`.
4. Medir por tres a cinco dias CPU, memoria, swap, PSI, filas e PIDs por
   projeto, correlacionando cada pico com tarefa ou deploy.
5. Aprovar em paralelo o upgrade para `m7a.2xlarge` ou a separacao dos RPAs.

## Conclusao

Nao ha evidencia tecnica para atribuir os picos globais ao OneLog atual. Ha
evidencia de pressao real no host, OOM global, RPAs sem teto de CPU, Flow com
base elevada de memoria, Lake com healthcheck que vaza zumbis, bancos sem
orcamento e uma instancia burstable pequena para 57 containers. A correcao
segura combina governanca de recursos por projeto com capacidade sustentada
maior ou separacao das automacoes.

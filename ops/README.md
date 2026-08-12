# Operacao do OneLog

## Watchdog observador

`onelog-watchdog-observe.sh` e um monitor de cinco minutos que verifica os
containers do OneLog, a memoria disponivel, falhas recentes de Chrome e a idade
da ultima sessao ativa. Ele **nao reinicia containers**.

O watchdog anterior usava ausencia de registros de pool no trecho final do log
da API como evidencia de incidente e reiniciava API, worker, Postgres e Redis.
Polling intenso podia ocultar a janela de interesse nesse trecho e causar falso
positivo, apagando a continuidade operacional durante uma falha real de host.

### Instalar em producao

```bash
sudo install -o root -g root -m 700 ops/onelog-watchdog-observe.sh \
  /home/ubuntu/scripts/onelog-watchdog-observe.sh
sudo crontab -e
```

Adicione somente:

```cron
*/5 * * * * /home/ubuntu/scripts/onelog-watchdog-observe.sh >/dev/null 2>&1
```

Mantenha desabilitada qualquer linha que execute o watchdog antigo. Em caso de
alerta, investigue os recursos do host e a saude dos containers antes de
reiniciar somente o componente afetado.

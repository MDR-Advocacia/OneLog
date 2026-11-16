# Resumos do OneLog no Teams

Os Adaptive Cards são enviados ao canal **TI → ⚠️ Alertas** às 08h, 13h e
21h (`America/Fortaleza`). O agendamento roda na EC2 de observabilidade, sem
depender do Mac ou de uma automação do Codex.

## Fluxo em produção

1. Os três `onelog-report-*.timer` da EC2 de observabilidade chamam
   `/usr/local/libexec/onelog-report-teams` com `morning`, `midday` ou `evening`.
2. Uma chave SSH exclusiva e restrita à origem privada da observabilidade
   executa somente `/usr/local/libexec/onelog-report-ssh` na EC2 de produção.
   Esse wrapper aceita apenas os três nomes de janela e chama
   `/usr/local/libexec/onelog-report-snapshot`.
3. O coletor lê os logs persistentes `api_debug.log*` e `worker_debug.log*`,
   consulta o status da API e as três filas Redis. Ele retorna **somente**
   contagens e estados em JSON. IPs, nomes dos solicitantes, IDs de pedidos,
   senhas e cookies não saem da EC2 de produção.
4. O emissor usa a autenticação Graph já existente no container `ops-webhook`
   para publicar um Adaptive Card. O registro local
   `/srv/observabilidade/data/ops-incidents/onelog-reports.sqlite` guarda
   `(data, janela, estado, ID da mensagem)` e evita posts repetidos.

Janelas: 21h do dia anterior–08h; 08h–13h; 13h–21h. `ad_denied` aparece
somente como quantidade de erros Windows/AD (401), sem inferir qual cliente
foi recusado. `pool_hit` e `cookie_returned` não comprovam o login final do
robô cliente no portal BB. Fila vazia após as 19h pode ser repouso programado.

## Conferência e recuperação

Na EC2 de observabilidade:

```sh
sudo systemctl list-timers --all 'onelog-report-*.timer'
sudo journalctl -u 'onelog-report@*.service' --since today --no-pager
sudo /usr/local/libexec/onelog-report-teams midday --dry-run
```

`--dry-run` coleta e renderiza sem enviar. Se o envio falhar, o estado pode
ficar `posting`: isso evita uma duplicação após uma resposta ambígua do Graph.
Antes de reenviar, conferir o canal ⚠️ Alertas e o ID da mensagem; só então
corrigir o registro ou disparar uma nova tentativa. Uma falha de coleta gera
card `SEM DADOS`, sem números inventados.

Arquivos instalados:

- Produção: `onelog_report_snapshot.py` e `onelog_report_ssh.sh` em
  `/usr/local/libexec/`; chave pública restrita em
  `/home/rildon-codex/.ssh/authorized_keys`.
- Observabilidade: `onelog_report_teams.py` em `/usr/local/libexec/`, chave
  privada e host key em `/etc/onelog-report/`, unidades em
  `/etc/systemd/system/`.

O primeiro envio, referente à janela de 08h–13h de 16/11/2026, foi publicado
às 16h33 no canal com ID `1794857586565`; o horário das 08h desse dia foi
marcado como anterior à ativação. O próximo envio automático é o das 21h.

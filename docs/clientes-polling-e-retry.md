# Contrato de clientes do OneLog

Este documento define como extensoes e outros clientes devem consumir os
endpoints de sessao sem gerar carga desnecessaria nem abandonar uma solicitacao
que o worker preservou para retry.

## O que ja funciona para clientes legados

- As rotas existentes continuam inalteradas.
- Clientes que consultam `GET /api/zerocore/status?setor=...` a cada dois
  segundos continuam recebendo uma resposta valida.
- Quando o worker encontra uma falha temporaria, a solicitacao agora fica
  preservada em uma fila adiada em vez de depender de novo login manual ou
  deploy.

## Alteracao recomendada para extensoes

Uma nova versao deve usar a cadencia fornecida pelo servidor:

1. Ler `poll_after_seconds` no JSON ou o header HTTP `Retry-After`.
2. Esperar esse intervalo antes da proxima consulta de status.
3. Usar cinco segundos como minimo seguro quando o servidor nao enviar
   orientacao.
4. Aceitar `retryable: true` e `retry_after_seconds` como situacao temporaria;
   nao exibir erro definitivo nem pedir credenciais novamente.
5. Manter um limite total de espera de ao menos doze minutos antes de oferecer
   uma nova acao manual.

Exemplo de resposta temporaria:

```json
{
  "concluido": false,
  "erro": false,
  "retryable": true,
  "retry_after_seconds": 300,
  "poll_after_seconds": 30,
  "mensagem": "Falha no processo. Nova tentativa automática em 5 min."
}
```

`retry_after_seconds` informa quando a tarefa agendada pode ser reexecutada.
`poll_after_seconds` informa somente a proxima consulta recomendada e deve ser
sempre respeitado para evitar tempestade de polling.

## Experiencia esperada pelo usuario

- Falha temporaria de Cloudflare: mensagem de estabilizacao e continuação
  automatica, sem acoes repetidas do usuario.
- Sessao quente: o cliente recebe os cookies e conclui normalmente.
- Erro definitivo de AD ou falta de permissao: encerrar o fluxo e solicitar
  credenciais apenas nesses casos.
- Falha de rede local: aplicar backoff exponencial, limitado a 30 segundos,
  sem disparar novos pedidos de login em paralelo.

## Regras de implementacao

- Deve existir no maximo um polling ativo por setor/usuario no navegador.
- O botao de login deve ficar desabilitado enquanto houver operacao pendente.
- O cliente nao deve chamar `/login` repetidamente quando ja recebeu
  `status: queued`; deve acompanhar apenas `/status`.
- O cliente deve preservar a credencial local apenas pelo tempo estritamente
  necessario e seguir a politica de armazenamento definida pelo escritorio.

## Observabilidade no cliente

Registrar localmente, sem incluir senhas, cookies ou valores de autenticacao:

- inicio e fim do fluxo;
- setor;
- resultado (`pool_hot`, `queued`, `retryable`, `erro`);
- tempo total;
- quantidade de consultas de status;
- `request_id`, se presente.

Esses dados permitem identificar extensoes desatualizadas que continuem
consultando a cada dois segundos sem expor informacoes sensiveis.

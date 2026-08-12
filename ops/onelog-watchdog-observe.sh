#!/usr/bin/env bash
# Monitoramento passivo do OneLog. Este script nunca reinicia containers.
set -u -o pipefail

PROJECT="vo4k08cwc444sgkgsgog8gcs"
VOLUME="/var/lib/docker/volumes/${PROJECT}_shared-data/_data"
WORKER_LOG="${VOLUME}/worker_debug.log"
SELF_LOG="/home/ubuntu/scripts/onelog-watchdog.log"
STATE_FILE="/var/run/onelog-watchdog.observe.last-alert"
CHROME_WINDOW_MIN=15
SESSION_STALE_MIN=60
ALERT_COOLDOWN_MIN=60
MIN_AVAILABLE_MEM_MIB=1024

MAIL_TO="ti@mdradvocacia.com neto@mdradvocacia.com"
SMTP_HOST="smtp.gmail.com"
SMTP_PORT=587

log() {
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') [observe] $*" >> "$SELF_LOG"
}

container_name() {
    local service="$1"
    docker ps --filter "label=com.docker.compose.project=${PROJECT}" \
        --filter "name=${service}" --format '{{.Names}}' | head -n1
}

worker="$(container_name worker)"
api="$(container_name api)"
db="$(container_name db)"
redis="$(container_name redis)"
observations=()

for pair in "worker:${worker}" "api:${api}" "db:${db}" "redis:${redis}"; do
    service="${pair%%:*}"
    name="${pair#*:}"
    if [ -z "$name" ] || [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)" != "true" ]; then
        observations+=("container_${service}_indisponivel")
    fi
done

available_kib="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
available_mib=$((available_kib / 1024))
swap_total_kib="$(awk '/SwapTotal:/ {print $2}' /proc/meminfo)"
swap_free_kib="$(awk '/SwapFree:/ {print $2}' /proc/meminfo)"
swap_used_mib=$(((swap_total_kib - swap_free_kib) / 1024))

if [ "$available_mib" -lt "$MIN_AVAILABLE_MEM_MIB" ]; then
    observations+=("memoria_disponivel_baixa_${available_mib}MiB")
fi

if [ -n "$worker" ] && [ -f "$WORKER_LOG" ]; then
    since_chrome="$(date -u -d "-${CHROME_WINDOW_MIN} min" '+%Y-%m-%d %H:%M:%S')"
    chrome_failure="$(tail -c 2000000 "$WORKER_LOG" 2>/dev/null | awk -v ts="$since_chrome" \
        'substr($0, 1, 19) >= ts && /cannot connect to chrome/' | tail -n1)"
    [ -n "$chrome_failure" ] && observations+=("chrome_inacessivel")
fi

# Consulta o banco diretamente: uma enxurrada de polling nao deve ser tratada
# como prova de que o pool esta frio.
if [ -n "$db" ]; then
    session_age_seconds="$(docker exec "$db" sh -lc \
        'psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" -Atc "SELECT COALESCE(EXTRACT(EPOCH FROM (NOW() - MAX(last_login_at)))::bigint, -1) FROM accounts_bb WHERE status = '\''ativo'\'';"' \
        2>/dev/null || echo -2)"
    case "$session_age_seconds" in
        ''|*[!0-9-]*) observations+=("idade_sessao_indisponivel") ;;
        -1) observations+=("nenhuma_sessao_ativa") ;;
        -2) observations+=("consulta_sessao_falhou") ;;
        *)
            session_age_minutes=$((session_age_seconds / 60))
            [ "$session_age_minutes" -ge "$SESSION_STALE_MIN" ] && observations+=("sessao_ativa_ha_${session_age_minutes}min")
            ;;
    esac
fi

[ "${#observations[@]}" -eq 0 ] && exit 0

reason="$(IFS=,; echo "${observations[*]}")"
now="$(date -u +%s)"
if [ -f "$STATE_FILE" ]; then
    IFS='|' read -r last_reason last_epoch < "$STATE_FILE" || true
    if [ "$last_reason" = "$reason" ] && [ "${last_epoch:-0}" -gt 0 ] && \
       [ $(((now - last_epoch) / 60)) -lt "$ALERT_COOLDOWN_MIN" ]; then
        log "Observacao repetida em cooldown: ${reason}"
        exit 0
    fi
fi
printf '%s|%s\n' "$reason" "$now" > "$STATE_FILE"

zombies="$(ps -eo stat= | awk '$1 ~ /^Z/ {n++} END {print n+0}')"
onelog_stats="$(docker stats --no-stream --format '{{.Name}} CPU={{.CPUPerc}} MEM={{.MemUsage}} PIDS={{.PIDs}}' \
    ${worker:+"$worker"} ${api:+"$api"} ${db:+"$db"} ${redis:+"$redis"} 2>/dev/null || true)"
log "ALERTA PASSIVO: ${reason}; disponivel=${available_mib}MiB swap_usada=${swap_used_mib}MiB zombies=${zombies}"

chatwoot="$(docker ps --format '{{.Names}}' | grep '^chatwoot-' | head -n1)"
smtp_user="$(docker exec "$chatwoot" printenv SMTP_USERNAME 2>/dev/null || true)"
smtp_pass="$(docker exec "$chatwoot" printenv SMTP_PASSWORD 2>/dev/null || true)"
[ -z "$smtp_user" ] || [ -z "$smtp_pass" ] && exit 0

message="$(mktemp)"
{
    echo "From: OneLog Watchdog <$smtp_user>"
    echo "To: $(echo "$MAIL_TO" | sed 's/ /, /g')"
    echo "Subject: [ONELOG] Observacao operacional - nenhuma acao automatica"
    echo "Content-Type: text/plain; charset=utf-8"
    echo
    echo "O monitor detectou uma condicao que merece verificacao."
    echo "Nenhum container foi reiniciado automaticamente."
    echo
    echo "Data/hora (UTC): $(date -u '+%Y-%m-%d %H:%M:%S')"
    echo "Sinais: $reason"
    echo "Memoria disponivel: ${available_mib} MiB"
    echo "Swap em uso: ${swap_used_mib} MiB"
    echo "Processos zumbi no host: $zombies"
    echo
    echo "Recursos do OneLog:"
    echo "$onelog_stats"
    echo
    echo "Acao automatica: nenhuma."
} > "$message"

if curl -sS --ssl-reqd "smtp://${SMTP_HOST}:${SMTP_PORT}" \
    --mail-from "$smtp_user" $(printf -- "--mail-rcpt %s " $MAIL_TO) \
    -u "${smtp_user}:${smtp_pass}" -T "$message" --max-time 60 >> "$SELF_LOG" 2>&1; then
    log "E-mail de observacao enviado para ${MAIL_TO}."
else
    log "Falha ao enviar e-mail de observacao."
fi
rm -f "$message"

#!/usr/bin/env python3
"""Post an idempotent OneLog Adaptive Card from the observability EC2."""

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
from zoneinfo import ZoneInfo


TZ = ZoneInfo("America/Fortaleza")
KEY = "/etc/onelog-report/id_ed25519"
KNOWN_HOSTS = "/etc/onelog-report/known_hosts"
LEDGER = "/srv/observabilidade/data/ops-incidents/onelog-reports.sqlite"
SOURCE = "rildon-codex@172.31.44.220"
WINDOWS = {
    "morning": ("Madrugada · 08h", 8),
    "midday": ("Meio do dia · 13h", 13),
    "evening": ("Início da noite · 21h", 21),
}
ERROR_LABELS = {
    "ad_denied": "autenticação Windows/AD (401)",
    "missing_credentials": "credenciais ausentes (400)",
    "no_account": "conta do setor ausente",
    "not_found": "sessão não encontrada",
    "api_error": "erro interno da API",
}


def period(slot, now):
    end = dt.datetime.combine(now.date(), dt.time(WINDOWS[slot][1]), TZ)
    start = end - dt.timedelta(hours={"morning": 11, "midday": 5, "evening": 8}[slot])
    return start, end


def collect(slot):
    command = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-i", KEY, SOURCE, slot,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=100, check=True)
    snapshot = json.loads(result.stdout)
    if snapshot.get("schema") != 1 or snapshot.get("slot") != slot:
        raise ValueError("unexpected OneLog snapshot")
    return snapshot


def fmt_clock(value):
    if not value:
        return "sem registro nas últimas 36h"
    return dt.datetime.fromisoformat(value).astimezone(TZ).strftime("%Hh%M")


def classify(snapshot):
    if snapshot.get("collection_error"):
        return "SEM DADOS", "Attention", "A coleta do OneLog falhou; conferir a EC2 e o coletor."
    worker = snapshot["worker"]
    api = snapshot["api"]
    age = worker["last_login_age_minutes"]
    if api["http"] != 200:
        return "ATENÇÃO", "Attention", "API sem resposta HTTP 200."
    if snapshot["slot"] == "evening" and api["after_19_requests"] == 0 and (age is None or age >= 22):
        return "REPOUSO PROGRAMADO", "Accent", "Sem pedidos após 19h; sessão antiga é esperada."
    if age is None or age >= snapshot["reuse_minutes"]:
        return "SEM SESSÃO PRONTA", "Attention", "Último login fora da janela de reutilização."
    if worker["exhausted"]:
        if age is not None and age < snapshot["reuse_minutes"] and worker["successes_after_last_exhausted"] >= 2:
            return "RECUPERADO", "Good", "Falhas anteriores no período foram seguidas por novos logins salvos."
        return "ATENÇÃO", "Attention", "Houve tarefa esgotada; verificar o worker."
    if api["errors"].get("api_error"):
        return "ATENÇÃO", "Warning", "A API registrou erro interno; conferir os logs."
    return "ESTÁVEL", "Good", "Worker e API responderam; sessão dentro da janela."


def fact(title, value):
    return {"title": title, "value": value}


def card(snapshot):
    slot = snapshot["slot"]
    title = WINDOWS[slot][0]
    start = dt.datetime.fromisoformat(snapshot["window_start"])
    end = dt.datetime.fromisoformat(snapshot["window_end"])
    span = f"{start:%d/%m %Hh}–{end:%d/%m %Hh} · Fortaleza"
    status, color, action = classify(snapshot)
    missing = bool(snapshot.get("collection_error"))
    worker = snapshot.get("worker") or {}
    api = snapshot.get("api") or {}
    queues = snapshot.get("queues") or {}
    raw_errors = api.get("errors") or {}
    error_text = "; ".join(
        f"{count} × {ERROR_LABELS.get(kind, kind)}"
        for kind, count in sorted(raw_errors.items()) if count
    ) or ("sem dados" if missing else "0 erros registrados")
    age = worker.get("last_login_age_minutes")
    gap = worker.get("max_login_gap_minutes")
    last = f"{fmt_clock(worker.get('last_login'))}"
    if age is not None:
        last += f" · idade {age:.0f} min ao fim do período"
    continuity = (
        f"Maior intervalo entre logins: {gap:.1f} min; "
        f"limite de reutilização: {snapshot.get('reuse_minutes', 22)} min."
        if gap is not None else "Sem dois logins observados para medir o intervalo."
    )
    ready = api.get("session_ready")
    session_state = "pronta" if ready is True else "em espera" if ready is False else "desconhecida"
    api_text = (
        f"HTTP {api.get('http') or 'sem resposta'} · "
        f"sessão {session_state} · "
        f"filas {queues.get('immediate', '—')}/{queues.get('priority', '—')}/"
        f"{queues.get('delayed', '—')} (imediata/prioritária/adiada)"
    )
    collected = snapshot.get("collected_at")
    api_title = f"API e filas · {fmt_clock(collected)}" if collected else "API e filas"
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "msteams": {"width": "Full"},
        "body": [
            {"type": "TextBlock", "text": "🤖 OneLog · Resumo operacional", "weight": "Bolder", "color": "Accent", "size": "Small"},
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Large", "wrap": True, "spacing": "Small"},
            {"type": "TextBlock", "text": status, "weight": "Bolder", "color": color, "spacing": "Small"},
            {"type": "TextBlock", "text": span, "isSubtle": True, "size": "Small", "spacing": "Small"},
            {"type": "ColumnSet", "spacing": "Medium", "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": str(worker.get("attempts", "—")), "weight": "Bolder", "size": "ExtraLarge"},
                    {"type": "TextBlock", "text": "tentativas", "isSubtle": True, "size": "Small"},
                ]},
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": str(worker.get("successes", "—")), "weight": "Bolder", "size": "ExtraLarge"},
                    {"type": "TextBlock", "text": "logins salvos", "isSubtle": True, "size": "Small"},
                ]},
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": str(worker.get("exhausted", "—")), "weight": "Bolder", "size": "ExtraLarge"},
                    {"type": "TextBlock", "text": "tarefas esgotadas", "isSubtle": True, "size": "Small"},
                ]},
            ]},
            {"type": "FactSet", "spacing": "Medium", "facts": [
                fact("Último login", last),
                fact("Continuidade", continuity),
                fact(api_title, api_text),
                fact("Erros nos pedidos", error_text),
            ]},
            {"type": "TextBlock", "text": action, "wrap": True, "spacing": "Medium"},
            {"type": "TextBlock", "text": "Contagens da API; não indicam clientes distintos nem comprovam o resultado final no portal BB.", "isSubtle": True, "size": "Small", "wrap": True},
        ],
    }


def sender_container():
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    names = [name for name in result.stdout.splitlines() if name.startswith("mdr-observabilidade-shadow-ops-webhook-")]
    if len(names) != 1:
        raise RuntimeError("ops-webhook container unavailable or ambiguous")
    return names[0]


GRAPH_SEND = """
import json, os, sys
from graph_auth import acquire_token, graph_request
p = json.load(sys.stdin)
token = acquire_token(interactive=False)
path = '/teams/{}/channels/{}/messages'.format(os.environ['OPS_GRAPH_TEAM_ID'], os.environ['OPS_GRAPH_ALERTS_CHANNEL_ID'])
result = graph_request(token, 'POST', path, p, api_version='beta')
print(json.dumps({'id': result.get('id'), 'webUrl': result.get('webUrl')}))
"""


def send(snapshot, rendered):
    slot = snapshot["slot"]
    label = WINDOWS[slot][0]
    body = (
        f"<b>OneLog — {label} · {snapshot['date']}</b>"
        '<br><attachment id="1"></attachment>'
        '<br><br><i>Enviado pelo Codex, a pedido de Rildon.</i>'
    )
    payload = {
        "body": {"contentType": "html", "content": body},
        "attachments": [{
            "id": "1", "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None, "content": json.dumps(rendered, ensure_ascii=False),
        }],
    }
    result = subprocess.run(
        ["docker", "exec", "-i", sender_container(), "python", "-c", GRAPH_SEND],
        input=json.dumps(payload, ensure_ascii=False), capture_output=True,
        text=True, timeout=45, check=True,
    )
    receipt = json.loads(result.stdout)
    if not receipt.get("id"):
        raise RuntimeError("Graph did not return a message ID")
    return receipt


def ledger():
    connection = sqlite3.connect(LEDGER)
    connection.execute("""CREATE TABLE IF NOT EXISTS reports (
        report_day TEXT NOT NULL, slot TEXT NOT NULL, state TEXT NOT NULL,
        attempted_at TEXT NOT NULL, message_id TEXT, message_url TEXT,
        PRIMARY KEY (report_day, slot)
    )""")
    connection.commit()
    return connection


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("slot", choices=WINDOWS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    now = dt.datetime.now(TZ)
    start, end = period(args.slot, now)
    if now < end:
        parser.error("report window has not ended")
    if not args.dry_run:
        connection = ledger()
        existing = connection.execute(
            "SELECT state,message_id FROM reports WHERE report_day=? AND slot=?",
            (end.date().isoformat(), args.slot),
        ).fetchone()
        if existing:
            print(json.dumps({"result": "already_recorded", "state": existing[0], "message_id": existing[1]}))
            return
    try:
        snapshot = collect(args.slot)
    except Exception as exc:
        snapshot = {
            "slot": args.slot, "date": end.date().isoformat(),
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "collection_error": type(exc).__name__,
        }
    rendered = card(snapshot)
    if args.dry_run:
        print(json.dumps({"snapshot": snapshot, "card": rendered}, ensure_ascii=False, indent=2))
        return
    connection.execute(
        "INSERT INTO reports(report_day,slot,state,attempted_at) VALUES(?,?,?,?)",
        (end.date().isoformat(), args.slot, "posting", now.isoformat()),
    )
    connection.commit()
    receipt = send(snapshot, rendered)
    connection.execute(
        "UPDATE reports SET state='sent',message_id=?,message_url=? WHERE report_day=? AND slot=?",
        (receipt["id"], receipt.get("webUrl"), end.date().isoformat(), args.slot),
    )
    connection.commit()
    print(json.dumps({"result": "sent", "slot": args.slot, "message_id": receipt["id"], "message_url": receipt.get("webUrl")}))


if __name__ == "__main__":
    main()

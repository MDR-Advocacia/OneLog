const API_URL = "https://api-onelog.mdradvocacia.com";

let currentState = { isWorking: false, step: "", error: "" };

// Ouve os comandos do Popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "START_FULL_LOGIN") {
        performFullLogin(request.user, request.pass);
        sendResponse({status: "started"});
    } else if (request.action === "START_RENEW_LOGIN") {
        performRenewLogin(request.setor);
        sendResponse({status: "started"});
    } else if (request.action === "GET_STATE") {
        sendResponse(currentState);
    } else if (request.action === "LOGOUT") {
        chrome.storage.local.remove(['onelog_user', 'onelog_active_setor']);
        updateState(false, "", "");
        chrome.alarms.clear("renew_session");
        sendResponse({status: "logout"});
    }
});

function updateState(isWorking, step, error = "") {
    currentState = { isWorking, step, error };
    chrome.runtime.sendMessage({ action: "STATE_UPDATE", state: currentState }).catch(() => {});
}

async function performFullLogin(user, pass) {
    updateState(true, "Autenticando no AD...");
    try {
        const res = await fetch(`${API_URL}/api/zerocore/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: user, password: pass })
        });
        
        if (res.status === 401 || res.status === 403) {
            const err = await res.json();
            return updateState(false, "", err.mensagem || "Acesso Negado pelo AD.");
        }
        
        const data = await res.json();
        const setor = data.setor;
        
        // Login aprovado! Salva pra sempre no Chrome.
        chrome.storage.local.set({ "onelog_user": { username: user, setor: setor } });
        
        if (data.status === "sucesso") await finalizeLogin(data.cookies, setor);
        else if (data.status === "queued") await pollStatusUntilDone(setor);
        
    } catch (e) {
        updateState(false, "", "Erro de rede ao conectar no servidor.");
    }
}

async function performRenewLogin(setor) {
    updateState(true, "Acordando o robô no servidor...");
    try {
        await fetch(`${API_URL}/api/zerocore/renew?setor=${setor}`, { method: 'POST' });
        await pollStatusUntilDone(setor);
    } catch(e) {
        updateState(false, "", "Erro de rede ao falar com a API.");
    }
}

async function pollStatusUntilDone(setor) {
    let polling = true;
    let fallbackTimer = 0;
    while (polling) {
        await new Promise(r => setTimeout(r, 2000));
        fallbackTimer++;
        try {
            const res = await fetch(`${API_URL}/api/zerocore/status?setor=${setor}`);
            const data = await res.json();
            
            if (data.mensagem) updateState(true, data.mensagem);
            
            if (data.erro || fallbackTimer > 150) { // Timeout segurança 5 min (Considerando os retries do bot)
                updateState(false, "", "Falha no robô. Tente novamente.");
                return;
            }
            if (data.concluido) {
                polling = false;
                updateState(true, "Robô finalizou! Baixando sessão...");
                const resSessao = await fetch(`${API_URL}/api/zerocore/session?setor=${setor}`);
                const sessionData = await resSessao.json();
                if (sessionData.status === "sucesso") await finalizeLogin(sessionData.cookies, setor);
                else updateState(false, "", "Erro ao recuperar cookies da sessão nova.");
            }
        } catch(e) {}
    }
}

async function finalizeLogin(cookies, setor) {
    updateState(true, "Injetando blindagem...");
    
    const cookiePromises = cookies.map(cookie => {
        return new Promise((resolve) => {
            if (cookie.domain.includes("juridico.bb.com.br")) { resolve(); return; }
            let cleanDomain = cookie.domain.startsWith('.') ? cookie.domain.substring(1) : cookie.domain;
            chrome.cookies.set({ url: "https://" + cleanDomain + cookie.path, name: cookie.name, value: cookie.value, domain: cookie.domain, path: cookie.path, secure: true, sameSite: "no_restriction" }, resolve);
        });
    });

    await Promise.all(cookiePromises);
    updateState(true, "Abrindo Portal...");
    
    // Inicia marcapasso seguro usando storage local
    chrome.storage.local.set({ "onelog_active_setor": setor });
    chrome.alarms.create("renew_session", { delayInMinutes: 20, periodInMinutes: 20 });
    
    setTimeout(() => {
        chrome.tabs.create({ url: "https://juridico.bb.com.br/wfj" });
        updateState(false, "", ""); // Limpa status
    }, 1000);
}

// O Marcapasso Silencioso
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "renew_session") {
        chrome.storage.local.get(["onelog_active_setor"], async (result) => {
            if (result.onelog_active_setor) {
                console.log("Renovando sessão background:", result.onelog_active_setor);
                await fetch(`${API_URL}/api/zerocore/renew?setor=${result.onelog_active_setor}`, { method: 'POST' });
            }
        });
    }
});
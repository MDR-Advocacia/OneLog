const API_URL = "https://api-onelog.mdradvocacia.com";

let currentState = { isWorking: false, step: "", error: "" };

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "START_FULL_LOGIN") {
        performFullLogin(request.user, request.pass);
        sendResponse({status: "started"});
    } else if (request.action === "START_RENEW_LOGIN") {
        performRenewLogin();
        sendResponse({status: "started"});
    } else if (request.action === "GET_STATE") {
        sendResponse(currentState);
    } else if (request.action === "LOGOUT") {
        chrome.storage.local.remove(['onelog_user', 'onelog_active_setor']);
        updateState(false, "", "");
        chrome.alarms.clear("renew_session");
        
        limparCookiesAntigos().then(() => {
            sendResponse({status: "logout"});
        });
        return true; 
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
        
        chrome.storage.local.set({ "onelog_user": { username: user, password: pass, setor: setor } });
        
        if (data.status === "sucesso") await finalizeLogin(data.cookies, setor);
        else if (data.status === "queued") await pollStatusUntilDone();
        
    } catch (e) {
        updateState(false, "", "Erro de rede ao conectar no servidor.");
    }
}

async function performRenewLogin() {
    updateState(true, "Verificando credenciais e acordando robô...");
    try {
        chrome.storage.local.get(["onelog_user"], async (res) => {
            if(!res.onelog_user) return updateState(false, "", "Sessão expirada. Faça login novamente.");
            
            const req = await fetch(`${API_URL}/api/zerocore/renew`, { 
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(res.onelog_user)
            });

            if(req.status === 401) {
                chrome.storage.local.remove(['onelog_user']);
                return updateState(false, "", "Credenciais revogadas no AD. Acesso suspenso.");
            }

            await pollStatusUntilDone();
        });
    } catch(e) {
        updateState(false, "", "Erro de rede ao falar com a API.");
    }
}

async function pollStatusUntilDone() {
    chrome.storage.local.get(["onelog_user"], async (resUser) => {
        if(!resUser.onelog_user) return;
        const userObj = resUser.onelog_user;
        const setor = userObj.setor;

        let polling = true;
        let fallbackTimer = 0;
        
        while (polling) {
            await new Promise(r => setTimeout(r, 2000));
            fallbackTimer++;
            try {
                const res = await fetch(`${API_URL}/api/zerocore/status?setor=${setor}`);
                const data = await res.json();
                
                if (data.mensagem) updateState(true, data.mensagem);
                
                if (data.erro || fallbackTimer > 150) { 
                    updateState(false, "", "Falha no robô. Tente acessar novamente.");
                    return;
                }
                
                if (data.concluido) {
                    polling = false;
                    updateState(true, "Robô finalizou! Baixando sessão segura...");
                    
                    const resSessao = await fetch(`${API_URL}/api/zerocore/session`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(userObj)
                    });
                    
                    const sessionData = await resSessao.json();
                    
                    if (sessionData.status === "sucesso") await finalizeLogin(sessionData.cookies, setor);
                    else updateState(false, "", "Erro ao recuperar cookies da sessão nova.");
                }
            } catch(e) {}
        }
    });
}

async function limparCookiesAntigos() {
    return new Promise((resolve) => {
        chrome.cookies.getAll({ domain: "bb.com.br" }, (cookies) => {
            if (cookies.length === 0) {
                resolve();
                return;
            }
            const promessas = cookies.map(cookie => {
                return new Promise((res) => {
                    let prefix = cookie.secure ? "https://" : "http://";
                    let cleanDomain = cookie.domain.startsWith('.') ? cookie.domain.substring(1) : cookie.domain;
                    let url = prefix + cleanDomain + cookie.path;
                    chrome.cookies.remove({ url: url, name: cookie.name }, () => res());
                });
            });
            Promise.all(promessas).then(resolve);
        });
    });
}

async function finalizeLogin(cookies, setor) {
    updateState(true, "Limpando resíduos antigos...");
    
    await limparCookiesAntigos();
    
    updateState(true, "Injetando blindagem...");
    const cookiePromises = cookies.map(cookie => {
        return new Promise((resolve) => {
            let cleanDomain = cookie.domain.startsWith('.') ? cookie.domain.substring(1) : cookie.domain;
            chrome.cookies.set({ 
                url: "https://" + cleanDomain + cookie.path, 
                name: cookie.name, 
                value: cookie.value, 
                domain: cookie.domain, 
                path: cookie.path, 
                secure: true, 
                sameSite: "no_restriction" 
            }, resolve);
        });
    });

    await Promise.all(cookiePromises);
    updateState(true, "Abrindo Portal...");
    
    chrome.alarms.create("renew_session", { delayInMinutes: 20, periodInMinutes: 20 });
    
    setTimeout(() => {
        // Se já tiver uma janela do portal aberta, evita abrir outra (Opcional)
        chrome.tabs.create({ url: "https://juridico.bb.com.br/wfj" });
        updateState(false, "", ""); 
    }, 1000);
}

// O Marcapasso Silencioso e Seguro
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "renew_session") {
        console.log("⏰ Marcapasso disparado! Renovando sessão e injetando...");
        // CORREÇÃO: Usa a função completa que ativa o robô, faz o poll e INJETA os novos cookies no Chrome!
        performRenewLogin();
    }
});
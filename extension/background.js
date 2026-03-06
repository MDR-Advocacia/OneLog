const API_URL = "http://api-onelog.mdradvocacia.com";

let currentState = { isWorking: false, step: "", error: "" };

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "START_FULL_LOGIN") {
        performFullLogin(request.user, request.pass);
        sendResponse({status: "started"});
    } else if (request.action === "START_RENEW_LOGIN") {
        performRenewLogin(false);
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
    } else if (request.action === "START_HEARTBEAT") {
        console.log("Marcapasso iniciado! Renovação agendada para 20 minutos.");
        chrome.alarms.create("renew_session", { delayInMinutes: 20, periodInMinutes: 20 });
    }
});

function updateState(isWorking, step, error = "") {
    currentState = { isWorking, step, error };
    chrome.runtime.sendMessage({ action: "STATE_UPDATE", state: currentState }).catch(() => {});
}

async function performFullLogin(user, pass) {
    if (!user || !pass) {
        chrome.storage.local.remove(['onelog_user', 'onelog_active_setor']);
        return updateState(false, "", "Segurança atualizada. Por favor, refaça o login.");
    }

    updateState(true, "Autenticando no AD...");
    try {
        const res = await fetch(`${API_URL}/api/zerocore/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: user, password: pass, user_agent: navigator.userAgent })
        });
        
        if (res.status === 401 || res.status === 403) {
            const err = await res.json();
            return updateState(false, "", err.mensagem || "Acesso Negado pelo AD.");
        }

        if (res.status === 400) {
            return updateState(false, "", "Erro de dados. Tente sair da conta e entrar de novo.");
        }
        
        const data = await res.json();
        const setor = data.setor;
        
        chrome.storage.local.set({ "onelog_user": { username: user, password: pass, setor: setor } });
        
        if (data.status === "sucesso") await finalizeLogin(data.cookies, false);
        else if (data.status === "queued") await pollStatusUntilDone(false);
        
    } catch (e) {
        updateState(false, "", "Erro de rede ao conectar no servidor.");
    }
}

async function performRenewLogin(isBackground = false) {
    updateState(true, "Verificando credenciais e acordando robô...");
    try {
        chrome.storage.local.get(["onelog_user"], async (res) => {
            if(!res.onelog_user || !res.onelog_user.password) {
                chrome.storage.local.remove(['onelog_user']);
                return updateState(false, "", "Sessão desatualizada. Faça login novamente.");
            }
            
            const payload = { ...res.onelog_user, user_agent: navigator.userAgent };
            const req = await fetch(`${API_URL}/api/zerocore/renew`, { 
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if(req.status === 401) {
                chrome.storage.local.remove(['onelog_user']);
                return updateState(false, "", "Credenciais revogadas no AD. Acesso suspenso.");
            }

            await pollStatusUntilDone(isBackground);
        });
    } catch(e) {
        updateState(false, "", "Erro de rede ao falar com a API.");
    }
}

async function pollStatusUntilDone(isBackground = false) {
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
                    
                    if (sessionData.status === "sucesso") await finalizeLogin(sessionData.cookies, isBackground);
                    else updateState(false, "", "Erro ao recuperar cookies da sessão nova.");
                }
            } catch(e) {}
        }
    });
}

async function limparCookiesAntigos() {
    return new Promise((resolve) => {
        chrome.cookies.getAll({ domain: "bb.com.br" }, (cookies) => {
            if (cookies.length === 0) return resolve();
            const promessas = cookies.map(cookie => {
                return new Promise((res) => {
                    let prefix = cookie.secure ? "https://" : "http://";
                    let cleanDomain = cookie.domain.startsWith('.') ? cookie.domain.substring(1) : cookie.domain;
                    let url = prefix + cleanDomain + cookie.path;
                    chrome.cookies.remove({ url: url, name: cookie.name }, res);
                });
            });
            Promise.all(promessas).then(resolve);
        });
    });
}

async function finalizeLogin(cookies, isBackground = false) {
    updateState(true, "Limpando resíduos antigos...");
    await limparCookiesAntigos();
    
    updateState(true, "Injetando blindagem...");
    
    // A extensão agora é "burra": injeta tudo que a API mandar. 
    // A inteligência de limpar os cookies tóxicos fica 100% no worker.py do servidor.
    const cookiePromises = cookies.map(cookie => {
        return new Promise((resolve) => {
            let cleanDomain = cookie.domain.startsWith('.') ? cookie.domain.substring(1) : cookie.domain;
            chrome.cookies.set({ 
                url: "https://" + cleanDomain + cookie.path, 
                name: cookie.name, value: cookie.value, domain: cookie.domain, 
                path: cookie.path, secure: true, sameSite: "no_restriction" 
            }, resolve);
        });
    });

    await Promise.all(cookiePromises);
    
    chrome.alarms.create("renew_session", { delayInMinutes: 20, periodInMinutes: 20 });
    
    if (!isBackground) {
        updateState(true, "Abrindo Portal...");
        setTimeout(() => {
            chrome.tabs.create({ url: "https://juridico.bb.com.br/wfj" });
            updateState(false, "", ""); 
        }, 1000);
    } else {
        console.log("🔄 Sessão renovada silenciosamente em background.");
        updateState(false, "", "");
    }
}

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "renew_session") {
        console.log("⏰ Marcapasso disparado! Renovando sessão...");
        performRenewLogin(true);
    }
});
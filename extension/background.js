// URL da tua API (Muda para localhost:5000 se estiveres a testar localmente)
const API_URL = "http://api-onelog.mdradvocacia.com";

// Ouve quando a UI (popup.js) finaliza um login para iniciar o cronómetro
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "START_HEARTBEAT") {
        console.log("❤ Marcapasso iniciado! Renovação agendada a cada 20 minutos.");
        // Configura o alarme para 20 minutos
        chrome.alarms.create("renew_session", { delayInMinutes: 20, periodInMinutes: 20 });
        sendResponse({ status: "Heartbeat started" });
    }
});

// Ouve o disparo do alarme
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "renew_session") {
        console.log("⏰ Alarme disparado: A renovar a sessão silenciosamente...");
        renovarSessaoSilenciosa();
    }
});

async function renovarSessaoSilenciosa() {
    try {
        // 1. Pede à API para colocar a renovação na fila
        // Usamos setor=GERAL por padrão nos testes
        await fetch(`${API_URL}/api/zerocore/renew?setor=GERAL`, { method: 'POST' });
        
        // 2. Fica a verificar o status até concluir a extração do novo cookie
        let tentativas = 0;
        const checkInterval = setInterval(async () => {
            tentativas++;
            try {
                const res = await fetch(`${API_URL}/api/zerocore/login?setor=GERAL`);
                const data = await res.json();
                
                if (data.status === "sucesso" && data.cookies) {
                    clearInterval(checkInterval);
                    injetarCookies(data.cookies);
                    console.log("✅ Sessão renovada e injetada com sucesso no background!");
                } else if (tentativas > 30) {
                    // Desiste após 30 tentativas (~1 minuto a aguardar o robô)
                    clearInterval(checkInterval);
                    console.log("❌ Falha na renovação silenciosa. O Robô demorou demasiado.");
                }
            } catch (err) {
                console.error("Erro a verificar status de renovação:", err);
            }
        }, 2000);

    } catch (e) {
        console.error("Erro crítico no Marcapasso:", e);
    }
}

function injetarCookies(cookies) {
    cookies.forEach(cookie => {
        // Protege cookies locais para não quebrar outras abas
        if (cookie.domain.includes("juridico.bb.com.br")) return; 
        
        let cleanDomain = cookie.domain.startsWith('.') ? cookie.domain.substring(1) : cookie.domain;
        let url = "https://" + cleanDomain + cookie.path;
        
        const c = { 
            url: url, 
            name: cookie.name, 
            value: cookie.value, 
            domain: cookie.domain, 
            path: cookie.path, 
            secure: true, 
            sameSite: "no_restriction" 
        };
        chrome.cookies.set(c);
    });
}
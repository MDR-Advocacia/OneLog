// URL da tua API
const API_URL = "https://api-onelog.mdradvocacia.com"; // <-- Usando HTTPS

let activeSetor = "GERAL"; // Salva o setor da máquina local

// Ouve quando a UI finaliza um login para iniciar o cronómetro
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "START_HEARTBEAT") {
        if (request.setor) activeSetor = request.setor; // Atualiza com o setor real do AD do usuário
        console.log(`❤ Marcapasso iniciado para o setor ${activeSetor}! Renovação agendada a cada 20 minutos.`);
        
        chrome.alarms.create("renew_session", { delayInMinutes: 20, periodInMinutes: 20 });
        sendResponse({ status: "Heartbeat started" });
    }
});

// Ouve o disparo do alarme
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "renew_session") {
        console.log(`⏰ Alarme disparado: A renovar a sessão (${activeSetor}) silenciosamente...`);
        renovarSessaoSilenciosa();
    }
});

async function renovarSessaoSilenciosa() {
    try {
        // 1. Pede à API para colocar a renovação na fila
        await fetch(`${API_URL}/api/zerocore/renew?setor=${activeSetor}`, { method: 'POST' });
        
        // 2. Fica a verificar o STATUS até concluir a extração do novo cookie
        let tentativas = 0;
        const checkInterval = setInterval(async () => {
            tentativas++;
            try {
                // Checa o status sem pedir login novo (Evita o erro 405 Method Not Allowed)
                const resStatus = await fetch(`${API_URL}/api/zerocore/status?setor=${activeSetor}`);
                const statusData = await resStatus.json();
                
                if (statusData.concluido) {
                    clearInterval(checkInterval);
                    
                    // 3. Robô concluiu! Puxa a nova sessão quente
                    const resSessao = await fetch(`${API_URL}/api/zerocore/session?setor=${activeSetor}`);
                    const sessionData = await resSessao.json();

                    if (sessionData.status === "sucesso" && sessionData.cookies) {
                        injetarCookies(sessionData.cookies);
                        console.log("✅ Sessão renovada e injetada com sucesso no background!");
                    }
                } else if (statusData.erro) {
                    clearInterval(checkInterval);
                    console.log("❌ Falha no robô durante a renovação silenciosa.");
                } else if (tentativas > 30) {
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
        if (cookie.domain.includes("juridico.bb.com.br")) return; 
        
        let cleanDomain = cookie.domain.startsWith('.') ? cookie.domain.substring(1) : cookie.domain;
        let url = "https://" + cleanDomain + cookie.path;
        
        const c = { 
            url: url, name: cookie.name, value: cookie.value, 
            domain: cookie.domain, path: cookie.path, 
            secure: true, sameSite: "no_restriction" 
        };
        chrome.cookies.set(c);
    });
}
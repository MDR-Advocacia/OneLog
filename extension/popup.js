const API_URL = "http://api-onelog.mdradvocacia.com";

const btnStart = document.getElementById('btn-start');
const loginForm = document.getElementById('login-form');
const loader = document.getElementById('loader');
const stepList = document.getElementById('step-list');
const debugArea = document.getElementById('debug-area');
const debugImg = document.getElementById('debug-img');
const errorMsg = document.getElementById('error-msg');

let isPolling = false;
let lastMsg = "";
let lastImg = "";
let currentSetor = ""; 

function addLog(msg) {
    if (!msg || msg === lastMsg) return;
    lastMsg = msg;
    stepList.style.display = "block";
    const li = document.createElement('li');
    li.className = 'step-item';
    li.innerHTML = `<span class="step-icon">✓</span> ${msg}`;
    stepList.appendChild(li);
    stepList.scrollTop = stepList.scrollHeight;
}

async function pollStatus() {
    if (!isPolling || !currentSetor) return;
    try {
        const res = await fetch(`${API_URL}/api/zerocore/status?setor=${currentSetor}`);
        const data = await res.json();
        
        if (data.mensagem) addLog(data.mensagem);
        
        if (data.imagem && data.imagem !== lastImg) {
            lastImg = data.imagem;
            debugImg.src = data.imagem + "?t=" + Date.now();
            debugArea.style.display = "block";
        }

        if (data.erro) {
            showError("Falha na automação. Tente novamente.");
            return;
        }

        if (!data.concluido) {
            setTimeout(pollStatus, 2000);
        }
    } catch (e) {
        setTimeout(pollStatus, 3000);
    }
}

function showError(msg) {
    errorMsg.innerText = msg;
    errorMsg.style.display = "block";
    isPolling = false;
    loader.style.display = "none";
    loginForm.style.display = "block";
}

btnStart.addEventListener('click', async () => {
    const user = document.getElementById('username').value;
    const pass = document.getElementById('password').value;

    if (!user || !pass) {
        alert("Por favor, preencha as credenciais da rede.");
        return;
    }

    loginForm.style.display = "none";
    loader.style.display = "block";
    errorMsg.style.display = "none";
    stepList.innerHTML = "";
    debugArea.style.display = "none";

    addLog("Autenticando no Active Directory...");

    try {
        const response = await fetch(`${API_URL}/api/zerocore/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: user, password: pass })
        });

        const data = await response.json();

        if (data.status === "erro") {
            showError(data.mensagem);
            return;
        }

        currentSetor = data.setor;
        isPolling = true;
        pollStatus();

        if (data.status === "sucesso") {
            handleSuccess(data);
        } else {
            // Se está em fila, o pollStatus() vai cuidar de avisar quando terminar
            const checkInterval = setInterval(async () => {
                try { // Blindagem contra "Failed to Fetch"
                    const res = await fetch(`${API_URL}/api/zerocore/login`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ username: user, password: pass })
                    });
                    const nextData = await res.json();
                    if (nextData.status === "sucesso") {
                        clearInterval(checkInterval);
                        handleSuccess(nextData);
                    }
                } catch (err) {
                    console.error("Aguardando estabilidade da rede/API...");
                }
            }, 5000);
        }

    } catch (e) {
        showError("Não foi possível conectar ao servidor OneLog.");
    }
});

function handleSuccess(data) {
    addLog("Acesso concedido! Injetando sessão...");
    
    let processed = 0;
    const cookies = data.cookies;
    cookies.forEach(cookie => {
        if (cookie.domain.includes("juridico.bb.com.br")) { processed++; return; }
        let cleanDomain = cookie.domain.startsWith('.') ? cookie.domain.substring(1) : cookie.domain;
        let url = "https://" + cleanDomain + cookie.path;
        
        chrome.cookies.set({ 
            url: url, name: cookie.name, value: cookie.value, domain: cookie.domain, path: cookie.path, secure: true, sameSite: "no_restriction" 
        }, () => {
            processed++;
            if (processed === cookies.length) {
                chrome.runtime.sendMessage({action: "START_HEARTBEAT", setor: currentSetor});
                setTimeout(() => { chrome.tabs.create({ url: "https://juridico.bb.com.br/wfj" }); }, 1000);
            }
        });
    });
}
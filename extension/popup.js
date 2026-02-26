const viewLogin = document.getElementById('view-login');
const viewLogged = document.getElementById('view-logged');
const viewWorking = document.getElementById('view-working');
const errorMsg = document.getElementById('error-msg');
const statusText = document.getElementById('status-text');
const loggedUserName = document.getElementById('logged-user-name');

chrome.storage.local.get(["onelog_user"], (res) => {
    checkBackgroundState(res.onelog_user);
});

function checkBackgroundState(savedUser) {
    chrome.runtime.sendMessage({ action: "GET_STATE" }, (response) => {
        if (response && response.isWorking) {
            showWorking(response.step);
        } else if (savedUser) {
            showLogged(savedUser);
        } else {
            showLogin();
        }
        if (response && response.error) showError(response.error);
    });
}

chrome.runtime.onMessage.addListener((request) => {
    if (request.action === "STATE_UPDATE") {
        if (request.state.isWorking) {
            showWorking(request.state.step);
        } else {
            if (!request.state.error) window.close();
            else {
                chrome.storage.local.get(["onelog_user"], (res) => {
                    res.onelog_user ? showLogged(res.onelog_user) : showLogin();
                });
                showError(request.state.error);
            }
        }
    }
});

document.getElementById('btn-login').addEventListener('click', () => {
    const user = document.getElementById('username').value;
    const pass = document.getElementById('password').value;
    if (!user || !pass) return showError("Preencha usuário e senha.");
    
    errorMsg.style.display = "none";
    chrome.runtime.sendMessage({ action: "START_FULL_LOGIN", user, pass });
});

document.getElementById('btn-access').addEventListener('click', () => {
    chrome.storage.local.get(["onelog_user"], (res) => {
        if(res.onelog_user) {
            errorMsg.style.display = "none";
            chrome.runtime.sendMessage({ action: "START_RENEW_LOGIN" });
        }
    });
});

document.getElementById('btn-logout').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: "LOGOUT" }, () => showLogin());
});

function showLogin() { viewLogin.style.display = "block"; viewLogged.style.display = "none"; viewWorking.style.display = "none"; }
function showLogged(user) { viewLogin.style.display = "none"; viewLogged.style.display = "block"; viewWorking.style.display = "none"; loggedUserName.innerText = `👤 ${user.username} (${user.setor})`; }
function showWorking(msg) { viewLogin.style.display = "none"; viewLogged.style.display = "none"; viewWorking.style.display = "block"; statusText.innerText = msg; }
function showError(msg) { errorMsg.innerText = msg; errorMsg.style.display = "block"; }
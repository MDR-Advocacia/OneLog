console.log("🛡️ OneLog Pro: Destravador de Editor ativado (Modo Nativo).");

function destrancarEditorBB() {
    // 1. Destranca campos nativos normais
    document.querySelectorAll('textarea, input, select, button').forEach(el => {
        if (el.hasAttribute('disabled')) el.removeAttribute('disabled');
        if (el.hasAttribute('readonly')) el.removeAttribute('readonly');
    });

    // 2. Acesso direto ao TinyMCE (Agora funciona graças ao 'world: MAIN' no manifest)
    if (typeof tinyMCE !== 'undefined') {
        var txtArea = document.getElementById('editorTextoForm:editorNovoTextArea');
        
        if (txtArea && txtArea.dataset.zerocore !== 'destravado') {
            try {
                tinyMCE.execCommand('mceRemoveControl', false, 'editorTextoForm:editorNovoTextArea');
                setTimeout(function() {
                    tinyMCE.execCommand('mceAddControl', false, 'editorTextoForm:editorNovoTextArea');
                    txtArea.dataset.zerocore = 'destravado'; 
                    console.log('ZeroCore: Editor TinyMCE reiniciado e destravado!');
                }, 300);
            } catch(e) {}
        }
    }
}

// Roda no carregamento e repete para pegar os popups do banco
window.addEventListener('load', destrancarEditorBB);
setInterval(destrancarEditorBB, 2000);
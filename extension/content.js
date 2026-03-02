console.log("🛡️ OneLog Pro: Destravador de Editor ativado.");

function destrancarEditorBB() {
    // 1. Destranca campos nativos normais
    document.querySelectorAll('textarea, input, select, button').forEach(el => {
        if (el.hasAttribute('disabled')) el.removeAttribute('disabled');
        if (el.hasAttribute('readonly')) el.removeAttribute('readonly');
    });

    // 2. O seu código original do TinyMCE (Injetado via script tag para burlar o Isolated World do Chrome)
    const scriptCode = `
        (function() {
            if (typeof tinyMCE !== 'undefined') {
                var txtArea = document.getElementById('editorTextoForm:editorNovoTextArea');
                
                // Só executa se o textarea existir e a gente ainda NÃO tiver destravado ele (evita piscar o editor)
                if (txtArea && txtArea.dataset.zerocore !== 'destravado') {
                    try {
                        tinyMCE.execCommand('mceRemoveControl', false, 'editorTextoForm:editorNovoTextArea');
                        setTimeout(function() {
                            tinyMCE.execCommand('mceAddControl', false, 'editorTextoForm:editorNovoTextArea');
                            txtArea.dataset.zerocore = 'destravado'; // Marca para não rodar de novo atoa
                            console.log('ZeroCore: Editor TinyMCE reiniciado e destravado!');
                        }, 300);
                    } catch(e) {}
                }
            }
        })();
    `;
    
    // Injeta o código na página real do BB
    const script = document.createElement('script');
    script.textContent = scriptCode;
    (document.head || document.documentElement).appendChild(script);
    script.remove(); // Limpa o rastro logo após rodar
}

// Roda no carregamento
window.addEventListener('load', destrancarEditorBB);

// Roda a cada 2 segundos (Pois os portais JSF do BB recarregam os blocos do nada via Ajax)
setInterval(destrancarEditorBB, 2000);
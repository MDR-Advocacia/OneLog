import os
import sys
import ctypes
import winreg
import time

# =======================================================
# CONFIGURAÇÕES DA SUA EXTENSÃO
# =======================================================
EXTENSION_ID = "jbakldonjghfiakcoipjlnedjlfpkfpp"
UPDATE_XML_URL = "https://api-onelog.mdradvocacia.com/static/update.xml"

def is_admin():
    """Verifica se o script está a correr como Administrador"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def install_enterprise_policy():
    print("\n[+] Configurando as Políticas Corporativas do Chrome...")
    value = f"{EXTENSION_ID};{UPDATE_XML_URL}"

    try:
        # Caminho da política do Chrome no Windows Registry
        key_path = r"SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"
        
        # Cria a chave (caso o Chrome não tenha políticas configuradas ainda)
        winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, key_path)
        
        # Abre a chave para escrita
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_WRITE)
        
        # Adiciona a nossa extensão na lista de Forçar Instalação
        winreg.SetValueEx(key, "1", 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
        
        print("\n SUCESSO! O OneLog foi injetado na máquina.")
        print("Para a extensão aparecer, FECHE TOTALMENTE o Google Chrome e abra de novo.")
        print("A extensão será instalada silenciosamente em background.")
        
    except Exception as e:
        print(f"\nErro crítico ao configurar o Registro: {e}")

if __name__ == "__main__":
    # Se não for admin, ele pede o popup de Administrador igual a um instalador profissional
    if not is_admin():
        print("Solicitando elevação de privilégios...")
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
        sys.exit()
    else:
        print("===========================================")
        print("             INSTALADOR ONELOG             ")
        print("===========================================")
        install_enterprise_policy()
        
        print("\n")
        os.system("pause")
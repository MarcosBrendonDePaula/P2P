@echo off
REM Script para iniciar o sistema P2P (servidor STUN e cliente)

echo Iniciando o sistema P2P...

REM Verifica se o Python está instalado
where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Python nao encontrado. Por favor, instale o Python 3.7 ou superior.
    pause
    exit /b 1
)

REM Cria o diretório resources se não existir
if not exist resources mkdir resources

REM Verifica se o arquivo de configuração existe
if not exist resources\stun_servers.json (
    echo Criando arquivo de configuracao padrao...
    echo {> resources\stun_servers.json
    echo     "stun": [>> resources\stun_servers.json
    echo       "192.168.1.23:54321">> resources\stun_servers.json
    echo     ],>> resources\stun_servers.json
    echo     "turn": [>> resources\stun_servers.json
    echo       {>> resources\stun_servers.json
    echo         "url": "turn:meuservidor.com:5349",>> resources\stun_servers.json
    echo         "username": "usuario",>> resources\stun_servers.json
    echo         "credential": "senha_forte">> resources\stun_servers.json
    echo       }>> resources\stun_servers.json
    echo     ]>> resources\stun_servers.json
    echo   }>> resources\stun_servers.json
)

REM Inicia o servidor STUN em uma nova janela
echo Iniciando o servidor STUN...
start "STUN Server" cmd /k "cd STUN_SERVER && python main.py"

REM Aguarda um pouco para o servidor iniciar
timeout /t 3 /nobreak >nul

REM Inicia o cliente P2P em uma nova janela
echo Iniciando o cliente P2P...
start "P2P Client" cmd /k "cd P2P_CLIENT && python main.py"

echo Sistema P2P iniciado com sucesso!
echo Para testar, inicie outra instancia do cliente P2P em uma nova janela.
echo.
echo Pressione qualquer tecla para iniciar outro cliente P2P...
pause >nul

REM Inicia outro cliente P2P em uma nova janela
echo Iniciando outro cliente P2P...
start "P2P Client 2" cmd /k "cd P2P_CLIENT && python main.py"

echo Pronto! Agora voce pode testar a comunicacao entre os clientes.
echo Use o comando 'list' para ver os peers disponiveis e 'connect <peer_id>' para conectar.
pause

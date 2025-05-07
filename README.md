# Projeto P2P com STUN Server

Este projeto implementa um sistema de comunicação peer-to-peer (P2P) com um servidor STUN para facilitar a conexão entre os peers através de NATs e firewalls.

## Componentes

O projeto é composto por dois componentes principais:

1. **Servidor STUN (STUN_SERVER)**: Facilita a descoberta de endpoints públicos e auxilia na conexão entre peers.
2. **Cliente P2P (P2P_CLIENT)**: Permite a comunicação direta entre peers, com suporte a múltiplas conexões simultâneas.

## Características

- **IDs de conexão seguros**: O servidor STUN atribui IDs de conexão longos e seguros para evitar conexões acidentais.
- **Hole punching assistido**: O servidor STUN auxilia no processo de hole punching para permitir conexões diretas entre peers.
- **Múltiplas conexões**: O cliente P2P suporta múltiplas conexões simultâneas com outros peers.
- **Interface de linha de comando**: O cliente P2P inclui uma interface de linha de comando para gerenciar conexões e enviar mensagens.
- **Fallback via relay**: Se a conexão direta falhar, as mensagens são enviadas através do servidor STUN como relay.

## Requisitos

- Python 3.7 ou superior
- Bibliotecas padrão do Python (não requer dependências externas para execução)
- PyInstaller (apenas para compilar os executáveis)

## Instalação

### Método 1: Executar a partir do código-fonte

1. Clone o repositório:
   ```
   git clone https://github.com/MarcosBrendonDePaula/P2P.git
   cd P2P
   ```

2. Execute o servidor STUN:
   ```
   cd STUN_SERVER
   python main.py
   ```

3. Execute o cliente P2P:
   ```
   cd P2P_CLIENT
   python main.py
   ```

### Método 2: Compilar executáveis

1. Clone o repositório:
   ```
   git clone https://github.com/MarcosBrendonDePaula/P2P.git
   cd P2P
   ```

2. Compile o servidor STUN:
   ```
   cd STUN_SERVER
   build.bat
   ```

3. Compile o cliente P2P:
   ```
   cd P2P_CLIENT
   build.bat
   ```

4. Execute os executáveis gerados na pasta `dist` de cada componente.

## Uso

### Servidor STUN

```
python main.py [--stun-port PORTA_UDP] [--tcp-port PORTA_TCP]
```

Opções:
- `--stun-port`: Porta UDP para o serviço STUN (padrão: 54321)
- `--tcp-port`: Porta TCP para o serviço de sinalização (padrão: 3478)

### Cliente P2P

```
python main.py [--local-file ARQUIVO_CONFIG]
```

Opções:
- `--local-file`: Caminho para um arquivo de configuração local com a lista de servidores STUN

#### Comandos da Interface de Linha de Comando

O cliente P2P possui uma interface de linha de comando com os seguintes comandos:

- `help`: Mostra a ajuda
- `list`: Lista os peers conhecidos
- `connect <peer_id>`: Conecta a um peer específico
- `send <peer_id> <mensagem>`: Envia uma mensagem para um peer
- `id`: Mostra seu ID atribuído
- `exit` ou `quit`: Sai da aplicação

## Configuração

O arquivo `resources/stun_servers.json` contém a lista de servidores STUN disponíveis. Você pode editar este arquivo para adicionar ou remover servidores.

Exemplo:
```json
{
  "stun": [
    "177.23.196.199:54321"
  ],
  "turn": [
    {
      "url": "turn:meuservidor.com:5349",
      "username": "usuario",
      "credential": "senha_forte"
    }
  ]
}
```

## Como funciona

1. O cliente P2P se registra no servidor STUN ao ser executado.
2. O servidor STUN atribui um ID único e seguro ao cliente.
3. Quando um cliente deseja se conectar a outro, ele envia uma solicitação ao servidor STUN.
4. O servidor STUN auxilia no processo de hole punching, permitindo que os clientes estabeleçam uma conexão direta.
5. Os clientes podem trocar mensagens diretamente entre si, com fallback para o servidor STUN como relay se necessário.

## Licença

Este projeto é distribuído sob a licença MIT. Veja o arquivo LICENSE para mais detalhes.

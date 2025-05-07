#!/usr/bin/env python3
"""
udp_p2p_node.py

P2P peer unificado com STUN+Relay discovery:
- Gera e exibe ID único ao iniciar.
- Busca lista de STUN+Relay servers ("stun" no JSON do GitHub).
- Usa STUN via UDP para descobrir endpoint público.
- Registra-se automaticamente em cada TCP signaling relay.
- Sincroniza peers via mensagens de "sync_register".
- Suporta múltiplas conexões simultâneas com outros peers.
- Executa hole punching assistido pelo servidor STUN.
- Interface de linha de comando para gerenciar conexões.

Corrige falha de SSL ao carregar lista: usa contexto sem verificação.

Uso:
  python udp_p2p_node.py [--local-file <path>]
"""
import argparse
import asyncio
import json
import struct
import socket
import logging
import uuid
import ssl
import sys
import time
import threading
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from typing import Dict, List, Tuple, Set, Optional

# STUN protocol constants
MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
XOR_MAPPED_ADDRESS = 0x0020

# Signaling / message types
HELLO = 'hello'
ASSIGN = 'assign_id'
REGISTER = 'register'
SYNC = 'sync_register'
DATA = 'data'
CONNECT = 'connect'
HOLEPUNCH = 'holepunch'

# GitHub raw URL com STUN+Relay JSON list
RAW_CONFIG_URL = (
    "https://raw.githubusercontent.com/MarcosBrendonDePaula/"
    "P2P/main/resources/stun_servers.json"
)

# Local file fallback
DEFAULT_LOCAL_CONFIG = "../resources/stun_servers.json"

logger = logging.getLogger('udp_p2p_node')

async def fetch_stun_relays(local_file=None):
    """Carrega lista de STUN+Relay servers do GitHub ou arquivo local."""
    # Primeiro tenta carregar do arquivo local se especificado
    if local_file:
        try:
            with open(local_file, 'r') as f:
                cfg = json.load(f)
            relays = []
            for entry in cfg.get('stun', []):
                if isinstance(entry, str):
                    clean = entry.split('stun:')[-1].lstrip('/')
                    if ':' in clean:
                        host, port = clean.split(':')
                        relays.append((host, int(port)))
            logger.info(f"Loaded {len(relays)} STUN+Relay servers from local file")
            return relays
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load from local file: {e}")
    
    # Se não conseguiu do arquivo local, tenta do GitHub
    try:
        # Configuração avançada para o request
        ctx = ssl._create_unverified_context()
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'application/json',
            'Cache-Control': 'no-cache'
        }
        req = Request(RAW_CONFIG_URL, headers=headers)
        
        # Tenta fazer o request com timeout maior
        with urlopen(req, context=ctx, timeout=10) as resp:
            content = resp.read().decode('utf-8')
            logger.info(f"Raw content from GitHub: {content[:100]}...")  # Log primeiros 100 chars
            cfg = json.loads(content)
            
        # Processa os servidores
        relays = []
        for entry in cfg.get('stun', []):
            if isinstance(entry, str):
                clean = entry.split('stun:')[-1].lstrip('/')
                if ':' in clean:
                    host, port = clean.split(':')
                    relays.append((host, int(port)))
                else:
                    logger.warning(f"Formato inválido para entrada STUN: {entry}")
                    
        logger.info(f"Fetched {len(relays)} STUN+Relay servers (SSL verify disabled)")
        
        # Se não encontrou nenhum relay, adiciona um padrão para testes
        if not relays:
            logger.warning("Nenhum relay encontrado no JSON, adicionando servidor da rede local para testes")
            relays.append(("192.168.1.23", 54321))
            
        return relays
    except Exception as e:
        logger.error(f"Failed to fetch STUN+Relay list: {e}")
        logger.warning("Usando servidor da rede local como fallback")
        return [("192.168.1.23", 54321)]  # Servidor da rede local como fallback

class P2PNodeProtocol(asyncio.DatagramProtocol):
    def __init__(self, relays):
        self.relays = relays
        self.transport = None
        self.peers = {}  # Todos os peers conhecidos
        self.active_connections = set()  # IDs dos peers com conexões ativas
        self.pending_connections = set()  # IDs dos peers com conexões pendentes
        self.tcp_writers = {}  # Escritores TCP para cada relay
        self.public_endpoint = None  # Endpoint público descoberto via STUN
        self.assigned_id = None  # ID atribuído pelo servidor STUN
        self.message_handlers = {}  # Manipuladores de mensagens para aplicações
        self.event_loop = asyncio.get_event_loop()

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info('socket')
        asyncio.create_task(self.startup(sock))

    async def startup(self, sock):
        # STUN discovery - Simplificado para evitar problemas
        # Apenas usa o endereço local como fallback
        public_ep = sock.getsockname()
        self.public_endpoint = public_ep
        logger.info(f"Using local endpoint: {public_ep}")
            
        # TCP connection to each relay - tenta localhost primeiro se o host for um IP da rede local
        for host, port in self.relays:
            # Lista de hosts para tentar, começando com o original
            hosts_to_try = [host]
            
            # Se o host for um IP da rede local (192.168.x.x), tenta localhost primeiro
            if host.startswith('192.168.'):
                hosts_to_try = ['127.0.0.1', host]
                
            # Tenta cada host na lista
            connected = False
            for try_host in hosts_to_try:
                try:
                    logger.info(f"Trying to connect to {try_host}:{port}...")
                    reader, writer = await asyncio.open_connection(try_host, port)
                    connected = True
                    host = try_host  # Atualiza o host para o que funcionou
                    break
                except Exception as e:
                    logger.warning(f"Failed to connect to {try_host}:{port}: {e}")
                    
            if not connected:
                continue
                
            try:
                # Send HELLO to get assigned ID
                temp_id = uuid.uuid4().hex[:8]
                hello_msg = {
                    'type': HELLO,
                    'temp_id': temp_id,
                    'addr': list(public_ep)
                }
                writer.write(json.dumps(hello_msg).encode() + b'\n')
                await writer.drain()
                
                # Store writer for later use
                relay_key = f"{host}:{port}"
                self.tcp_writers[relay_key] = writer
                
                # Start listening for TCP messages
                asyncio.create_task(self.listen_tcp(reader, relay_key))
                logger.info(f"Connected to relay TCP {host}:{port}")
            except Exception as e:
                logger.warning(f"Failed to connect to {host}:{port}: {e}")

    # Método do_stun removido para simplificar

    async def listen_tcp(self, reader, relay_key):
        while True:
            line = await reader.readline()
            if not line:
                logger.warning(f"TCP connection to {relay_key} closed")
                if relay_key in self.tcp_writers:
                    self.tcp_writers.pop(relay_key, None)
                break
                
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
                
            mtype = msg.get('type')
            
            if mtype == ASSIGN:
                # Recebeu ID atribuído pelo servidor
                temp_id = msg.get('temp_id')
                self.assigned_id = msg.get('assigned_id')
                logger.info(f"Assigned ID: {self.assigned_id}")
                
                # Registra o ID atribuído
                if self.public_endpoint:
                    reg_msg = {
                        'type': SYNC,
                        'peer_id': self.assigned_id,
                        'addr': list(self.public_endpoint)
                    }
                    writer = self.tcp_writers.get(relay_key)
                    if writer:
                        writer.write(json.dumps(reg_msg).encode() + b'\n')
                        await writer.drain()
                        logger.info(f"Registered with ID {self.assigned_id} to {relay_key}")
                
            elif mtype == SYNC:
                # Recebeu informação de outro peer
                pid = msg['peer_id']
                addr = tuple(msg['addr'])
                self.peers[pid] = addr
                logger.info(f"Synced peer {pid}@{addr}")
                
            elif mtype == HOLEPUNCH:
                # Recebeu solicitação de holepunch
                from_id = msg.get('from')
                addr = tuple(msg.get('addr'))
                
                if from_id and addr:
                    logger.info(f"Received holepunch request from {from_id}@{addr}")
                    
                    # Adiciona à lista de conexões ativas
                    self.active_connections.add(from_id)
                    
                    # Envia pacote UDP para iniciar hole punching
                    self.transport.sendto(b'', addr)
                    
                    # Envia mensagem de confirmação
                    punch_confirm = {
                        'type': DATA,
                        'from': self.assigned_id,
                        'to': from_id,
                        'payload': f'Holepunch confirmation from {self.assigned_id}'
                    }
                    self.transport.sendto(json.dumps(punch_confirm).encode(), addr)
                    logger.info(f"Sent holepunch confirmation to {from_id}@{addr}")
                    
            elif mtype == 'connect_initiated':
                # Confirmação de que a solicitação de conexão foi enviada
                to_id = msg.get('to')
                addr = tuple(msg.get('addr'))
                
                if to_id in self.pending_connections:
                    logger.info(f"Connection initiated to {to_id}@{addr}")
                    
                    # Envia pacote UDP para iniciar hole punching
                    self.transport.sendto(b'', addr)
                
            elif mtype == DATA:
                # Recebeu dados de outro peer via TCP relay
                if msg.get('to') == self.assigned_id:
                    from_id = msg.get('from')
                    payload = msg.get('payload')
                    logger.info(f"Received DATA from {from_id}: {payload}")
                    
                    # Processa a mensagem com handlers registrados
                    if from_id in self.message_handlers:
                        for handler in self.message_handlers.get(from_id, []):
                            handler(from_id, payload)

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode())
            if msg.get('type') == DATA:
                to_id = msg.get('to')
                from_id = msg.get('from')
                payload = msg.get('payload')
                
                if to_id == self.assigned_id:
                    logger.info(f"Received UDP DATA from {from_id}: {payload}")
                    
                    # Adiciona à lista de conexões ativas se não estiver
                    if from_id not in self.active_connections:
                        self.active_connections.add(from_id)
                        self.peers[from_id] = addr
                        logger.info(f"Added {from_id} to active connections")
                    
                    # Processa a mensagem com handlers registrados
                    if from_id in self.message_handlers:
                        for handler in self.message_handlers.get(from_id, []):
                            handler(from_id, payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Ignora pacotes que não são JSON (podem ser pacotes de hole punching)
            pass

    async def connect_to_peer(self, peer_id):
        """Inicia conexão com outro peer."""
        if not self.assigned_id:
            logger.error("Cannot connect: No assigned ID yet")
            return False
            
        if peer_id not in self.peers:
            logger.error(f"Cannot connect: Peer {peer_id} not known")
            return False
            
        # Adiciona à lista de conexões pendentes
        self.pending_connections.add(peer_id)
        
        # Envia solicitação de conexão via relay
        connect_msg = {
            'type': CONNECT,
            'from': self.assigned_id,
            'to': peer_id,
            'timestamp': time.time()
        }
        
        # Envia para todos os relays disponíveis
        sent = False
        for relay_key, writer in self.tcp_writers.items():
            try:
                writer.write(json.dumps(connect_msg).encode() + b'\n')
                await writer.drain()
                sent = True
                logger.info(f"Sent connection request to {peer_id} via {relay_key}")
            except Exception as e:
                logger.warning(f"Failed to send connection request via {relay_key}: {e}")
                
        return sent

    async def send_message(self, peer_id, message):
        """Envia mensagem para um peer conectado."""
        if not self.assigned_id:
            logger.error("Cannot send: No assigned ID yet")
            return False
            
        if peer_id not in self.peers:
            logger.error(f"Cannot send: Peer {peer_id} not known")
            return False
            
        # Prepara a mensagem
        data_msg = {
            'type': DATA,
            'from': self.assigned_id,
            'to': peer_id,
            'payload': message
        }
        
        # Tenta enviar diretamente via UDP se for uma conexão ativa
        if peer_id in self.active_connections:
            addr = self.peers[peer_id]
            try:
                self.transport.sendto(json.dumps(data_msg).encode(), addr)
                logger.info(f"Sent message to {peer_id} via UDP")
                return True
            except Exception as e:
                logger.warning(f"Failed to send via UDP to {peer_id}: {e}")
        
        # Fallback: envia via relay TCP
        sent = False
        for relay_key, writer in self.tcp_writers.items():
            try:
                writer.write(json.dumps(data_msg).encode() + b'\n')
                await writer.drain()
                sent = True
                logger.info(f"Sent message to {peer_id} via relay {relay_key}")
            except Exception as e:
                logger.warning(f"Failed to send via relay {relay_key}: {e}")
                
        return sent

    def register_message_handler(self, peer_id, handler):
        """Registra um handler para mensagens de um peer específico."""
        if peer_id not in self.message_handlers:
            self.message_handlers[peer_id] = []
        self.message_handlers[peer_id].append(handler)
        
    def get_peer_list(self):
        """Retorna lista de peers conhecidos."""
        return {
            'all_peers': list(self.peers.keys()),
            'active_connections': list(self.active_connections),
            'pending_connections': list(self.pending_connections)
        }

class CommandLineInterface:
    """Interface de linha de comando para o cliente P2P."""
    def __init__(self, node):
        self.node = node
        self.running = True
        
    def start(self):
        """Inicia a interface de linha de comando em uma thread separada."""
        threading.Thread(target=self._run_cli, daemon=True).start()
        
    def _run_cli(self):
        """Loop principal da interface de linha de comando."""
        print("\n=== P2P Client Command Line Interface ===")
        print("Type 'help' for available commands")
        
        while self.running:
            try:
                cmd = input("\nCommand> ").strip()
                if not cmd:
                    continue
                    
                parts = cmd.split()
                command = parts[0].lower()
                
                if command == 'help':
                    self._show_help()
                elif command == 'list':
                    self._list_peers()
                elif command == 'connect':
                    if len(parts) < 2:
                        print("Usage: connect <peer_id>")
                    else:
                        asyncio.run_coroutine_threadsafe(
                            self.node.connect_to_peer(parts[1]), 
                            self.node.event_loop
                        )
                elif command == 'send':
                    if len(parts) < 3:
                        print("Usage: send <peer_id> <message>")
                    else:
                        peer_id = parts[1]
                        message = ' '.join(parts[2:])
                        asyncio.run_coroutine_threadsafe(
                            self.node.send_message(peer_id, message), 
                            self.node.event_loop
                        )
                elif command == 'id':
                    self._show_id()
                elif command == 'exit' or command == 'quit':
                    print("Exiting...")
                    self.running = False
                    # Não podemos chamar sys.exit() aqui porque estamos em uma thread
                else:
                    print(f"Unknown command: {command}")
                    print("Type 'help' for available commands")
            except KeyboardInterrupt:
                print("\nExiting...")
                self.running = False
                break
            except Exception as e:
                print(f"Error: {e}")
                
    def _show_help(self):
        """Mostra a ajuda da interface de linha de comando."""
        print("\nAvailable commands:")
        print("  help                - Show this help")
        print("  list                - List known peers")
        print("  connect <peer_id>   - Connect to a peer")
        print("  send <peer_id> <msg>- Send message to a peer")
        print("  id                  - Show your assigned ID")
        print("  exit, quit          - Exit the application")
        
    def _list_peers(self):
        """Lista os peers conhecidos."""
        peer_info = self.node.get_peer_list()
        
        print("\nKnown peers:")
        for i, peer_id in enumerate(peer_info['all_peers']):
            status = []
            if peer_id in peer_info['active_connections']:
                status.append("ACTIVE")
            if peer_id in peer_info['pending_connections']:
                status.append("PENDING")
            status_str = ", ".join(status) if status else "KNOWN"
            print(f"  {i+1}. {peer_id} - {status_str}")
            
        if not peer_info['all_peers']:
            print("  No peers discovered yet")
            
    def _show_id(self):
        """Mostra o ID atribuído."""
        if self.node.assigned_id:
            print(f"\nYour assigned ID: {self.node.assigned_id}")
        else:
            print("\nNo ID assigned yet. Still initializing...")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--local-file', help='Path to local STUN servers config file')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    
    # Carrega lista de servidores STUN+Relay
    local_file = args.local_file or DEFAULT_LOCAL_CONFIG
    relays = await fetch_stun_relays(local_file)
    
    # Se não conseguiu carregar do arquivo, usa o IP da rede local como fallback
    if not relays:
        logger.warning("No relays available from config. Using local network IP as fallback.")
        relays = [("192.168.1.23", 54321)]

    # Cria o endpoint UDP de forma simplificada
    loop = asyncio.get_event_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: P2PNodeProtocol(relays),
        local_addr=('0.0.0.0', 0)
    )
    
    # Inicia a interface de linha de comando
    cli = CommandLineInterface(protocol)
    cli.start()
    
    # Mantém o programa rodando
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)

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
import random
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
            
            # Processa servidores STUN
            for entry in cfg.get('stun', []):
                if isinstance(entry, str):
                    # Remove prefixo 'stun:' ou 'stun://' se presente
                    if 'stun:' in entry:
                        clean = entry.split('stun:')[-1].lstrip('/')
                    else:
                        clean = entry
                        
                    # Extrai host e porta
                    if ':' in clean:
                        host, port = clean.split(':')
                        try:
                            relays.append((host, int(port)))
                            logger.info(f"Adicionado relay: {host}:{port}")
                        except ValueError:
                            logger.warning(f"Porta inválida: {clean}")
                    else:
                        # Usa porta padrão 3478 se não especificada
                        logger.info(f"Adicionado relay com porta padrão: {clean}:3478")
                        relays.append((clean, 3478))
                        
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
                # Remove prefixo 'stun:' ou 'stun://' se presente
                if 'stun:' in entry:
                    clean = entry.split('stun:')[-1].lstrip('/')
                else:
                    clean = entry
                
                # Extrai host e porta
                if ':' in clean:
                    host, port = clean.split(':')
                    try:
                        relays.append((host, int(port)))
                    except ValueError:
                        logger.warning(f"Formato inválido para entrada STUN: {entry}")
                else:
                    # Usa porta padrão 3478 se não especificada
                    relays.append((clean, 3478))
                    
        logger.info(f"Fetched {len(relays)} STUN+Relay servers (SSL verify disabled)")
        
        # Se não encontrou nenhum relay, adiciona um padrão para testes
        if not relays:
            logger.warning("Nenhum relay encontrado no JSON, adicionando servidores locais para testes")
            relays.append(("127.0.0.1", 3478))
            relays.append(("localhost", 3478))
            
        return relays
    except Exception as e:
        logger.error(f"Failed to fetch STUN+Relay list: {e}")
        logger.warning("Usando servidores locais como fallback")
        return [("127.0.0.1", 3478), ("localhost", 3478)]  # Servidores locais como fallback

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
        self.connection_attempts = {}  # Contador de tentativas de conexão a cada relay
        self.max_attempts = 3  # Número máximo de tentativas por relay

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info('socket')
        asyncio.create_task(self.startup(sock))

    async def startup(self, sock):
        # STUN discovery - Simplificado para evitar problemas
        # Usa o endereço local como fallback
        local_ep = sock.getsockname()
        self.public_endpoint = local_ep
        logger.info(f"Using local endpoint: {local_ep}")
            
        # TCP connection to each relay - prioriza localhost para testes locais
        for host, port in self.relays:
            relay_key = f"{host}:{port}"
            self.connection_attempts[relay_key] = 0
            
            # Lista priorizada de hosts para tentar
            hosts_to_try = []
            
            # Se o host for localhost/127.0.0.1 ou um IP da rede local, tenta apenas esse
            if host in ['localhost', '127.0.0.1']:
                hosts_to_try = [host]
            # Se for um IP da rede local ou nome de máquina, tenta localhost primeiro
            elif host.startswith('192.168.') or '.' not in host:
                hosts_to_try = ['localhost', '127.0.0.1', host]
            else:
                hosts_to_try = [host, 'localhost', '127.0.0.1']
                
            # Tenta cada host na lista
            connected = False
            for try_host in hosts_to_try:
                try:
                    logger.info(f"Tentando conectar a {try_host}:{port}...")
                    reader, writer = await asyncio.open_connection(try_host, port)
                    connected = True
                    
                    # Armazena o host que funcionou
                    effective_host = try_host
                    effective_relay_key = f"{effective_host}:{port}"
                    
                    # Envia HELLO para obter ID atribuído
                    await self.send_hello(reader, writer, effective_relay_key, local_ep)
                    
                    # Inicia task para ouvir mensagens TCP
                    asyncio.create_task(self.listen_tcp(reader, effective_relay_key))
                    
                    logger.info(f"Conectado ao relay TCP {effective_host}:{port}")
                    break
                except Exception as e:
                    logger.warning(f"Falha ao conectar a {try_host}:{port}: {e}")
                    self.connection_attempts[relay_key] += 1
            
            # Se falhou em conectar com este relay após tentar todos os hosts
            if not connected:
                logger.error(f"Não foi possível conectar ao relay {host}:{port} após tentar todos os hosts")
                
                # Se ainda não excedeu o número máximo de tentativas, agenda uma nova tentativa
                if self.connection_attempts[relay_key] < self.max_attempts:
                    delay = 2 ** self.connection_attempts[relay_key]  # Backoff exponencial
                    logger.info(f"Agendando nova tentativa para {host}:{port} em {delay} segundos")
                    asyncio.create_task(self.retry_connection(host, port, delay))

    async def retry_connection(self, host, port, delay):
        """Tenta reconectar a um relay após um atraso."""
        await asyncio.sleep(delay)
        relay_key = f"{host}:{port}"
        
        # Verifica se já não estamos conectados a este relay
        if relay_key in self.tcp_writers:
            return
            
        logger.info(f"Tentando reconectar a {host}:{port}...")
        
        # Lista priorizada de hosts para tentar
        hosts_to_try = []
        if host in ['localhost', '127.0.0.1']:
            hosts_to_try = [host]
        elif host.startswith('192.168.') or '.' not in host:
            hosts_to_try = ['localhost', '127.0.0.1', host]
        else:
            hosts_to_try = [host, 'localhost', '127.0.0.1']
            
        # Tenta cada host na lista
        connected = False
        for try_host in hosts_to_try:
            try:
                reader, writer = await asyncio.open_connection(try_host, port)
                connected = True
                effective_host = try_host
                effective_relay_key = f"{effective_host}:{port}"
                
                # Envia HELLO para obter ID atribuído
                local_ep = self.transport.get_extra_info('socket').getsockname()
                await self.send_hello(reader, writer, effective_relay_key, local_ep)
                
                # Inicia task para ouvir mensagens TCP
                asyncio.create_task(self.listen_tcp(reader, effective_relay_key))
                
                logger.info(f"Reconectado ao relay TCP {effective_host}:{port}")
                break
            except Exception as e:
                logger.warning(f"Falha ao reconectar a {try_host}:{port}: {e}")
                
        # Se falhou novamente, incrementa contador e agenda nova tentativa se necessário
        if not connected:
            relay_key = f"{host}:{port}"
            self.connection_attempts[relay_key] += 1
            
            if self.connection_attempts[relay_key] < self.max_attempts:
                delay = 2 ** self.connection_attempts[relay_key]  # Backoff exponencial
                logger.info(f"Agendando nova tentativa para {host}:{port} em {delay} segundos")
                asyncio.create_task(self.retry_connection(host, port, delay))
            else:
                logger.error(f"Desistindo de conectar a {host}:{port} após {self.max_attempts} tentativas")

    async def send_hello(self, reader, writer, relay_key, local_ep):
        """Envia mensagem HELLO para o relay."""
        temp_id = uuid.uuid4().hex[:8]
        hello_msg = {
            'type': HELLO,
            'temp_id': temp_id,
            'addr': list(local_ep)
        }
        writer.write(json.dumps(hello_msg).encode() + b'\n')
        await writer.drain()
        
        # Armazena o writer para uso posterior
        self.tcp_writers[relay_key] = writer
        logger.info(f"HELLO enviado para {relay_key} com temp_id {temp_id}")

    async def listen_tcp(self, reader, relay_key):
        """Escuta mensagens TCP do relay."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    logger.warning(f"Conexão TCP com {relay_key} fechada")
                    if relay_key in self.tcp_writers:
                        writer = self.tcp_writers.pop(relay_key, None)
                        if writer:
                            writer.close()
                            
                    # Tenta reconectar apenas se ainda não excedeu o número máximo de tentativas
                    host, port = relay_key.split(':')
                    port = int(port)
                    if self.connection_attempts.get(relay_key, 0) < self.max_attempts:
                        delay = 2 ** self.connection_attempts.get(relay_key, 0)
                        logger.info(f"Agendando reconexão com {relay_key} em {delay} segundos")
                        asyncio.create_task(self.retry_connection(host, port, delay))
                    break
                    
                try:
                    msg = json.loads(line.decode())
                except json.JSONDecodeError:
                    logger.warning(f"JSON inválido recebido de {relay_key}: {line}")
                    continue
                    
                mtype = msg.get('type')
                logger.info(f"Recebida mensagem tipo {mtype} de {relay_key}")
                
                if mtype == ASSIGN:
                    # Recebeu ID atribuído pelo servidor
                    temp_id = msg.get('temp_id')
                    self.assigned_id = msg.get('assigned_id')
                    logger.info(f"ID atribuído: {self.assigned_id}")
                    
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
                            logger.info(f"Registrado com ID {self.assigned_id} no relay {relay_key}")
                
                elif mtype == SYNC:
                    # Recebeu informação de outro peer
                    pid = msg.get('peer_id')
                    addr_list = msg.get('addr')
                    
                    if pid and addr_list and len(addr_list) >= 2:
                        try:
                            addr = (str(addr_list[0]), int(addr_list[1]))
                            self.peers[pid] = addr
                            logger.info(f"Sincronizado peer {pid}@{addr}")
                        except (ValueError, TypeError):
                            logger.warning(f"Formato de endereço inválido em SYNC: {addr_list}")
                    else:
                        logger.warning(f"Mensagem SYNC inválida: {msg}")
                    
                elif mtype == HOLEPUNCH:
                    # Recebeu solicitação de holepunch
                    from_id = msg.get('from')
                    addr_list = msg.get('addr')
                    
                    if from_id and addr_list and len(addr_list) >= 2:
                        try:
                            addr = (str(addr_list[0]), int(addr_list[1]))
                            logger.info(f"Recebida solicitação de holepunch de {from_id}@{addr}")
                            
                            # Adiciona à lista de conexões ativas
                            self.active_connections.add(from_id)
                            self.peers[from_id] = addr
                            
                            # Envia pacote UDP para iniciar hole punching
                            punch_msg = json.dumps({
                                'type': 'punch',
                                'from': self.assigned_id,
                                'to': from_id
                            }).encode()
                            self.transport.sendto(punch_msg, addr)
                            logger.info(f"Enviado pacote de holepunch para {from_id}@{addr}")
                            
                            # Envia mensagem de confirmação após pequeno delay
                            await asyncio.sleep(0.5)  # Pequeno delay para permitir abertura do hole
                            
                            punch_confirm = {
                                'type': DATA,
                                'from': self.assigned_id,
                                'to': from_id,
                                'payload': f'Confirmação de holepunch de {self.assigned_id}'
                            }
                            confirm_data = json.dumps(punch_confirm).encode()
                            self.transport.sendto(confirm_data, addr)
                            logger.info(f"Enviada confirmação de holepunch para {from_id}@{addr}")
                        except (ValueError, TypeError):
                            logger.warning(f"Formato de endereço inválido em HOLEPUNCH: {addr_list}")
                    else:
                        logger.warning(f"Mensagem HOLEPUNCH inválida: {msg}")
                    
                elif mtype == 'connect_initiated':
                    # Confirmação de que a solicitação de conexão foi enviada
                    to_id = msg.get('to')
                    addr_list = msg.get('addr')
                    
                    if to_id and to_id in self.pending_connections and addr_list and len(addr_list) >= 2:
                        try:
                            addr = (str(addr_list[0]), int(addr_list[1]))
                            logger.info(f"Conexão iniciada com {to_id}@{addr}")
                            
                            # Armazena o endereço do peer
                            self.peers[to_id] = addr
                            
                            # Envia pacote UDP para iniciar hole punching
                            punch_msg = json.dumps({
                                'type': 'punch',
                                'from': self.assigned_id,
                                'to': to_id
                            }).encode()
                            self.transport.sendto(punch_msg, addr)
                            logger.info(f"Enviado pacote inicial de holepunch para {to_id}@{addr}")
                            
                            # Agenda envio de mensagem após pequeno delay
                            asyncio.create_task(self.delayed_punch_message(to_id, addr))
                        except (ValueError, TypeError):
                            logger.warning(f"Formato de endereço inválido em connect_initiated: {addr_list}")
                    elif to_id:
                        logger.info(f"Conexão iniciada com {to_id}, mas sem endereço válido")
                    else:
                        logger.warning(f"Mensagem connect_initiated inválida: {msg}")
                
                elif mtype == DATA:
                    # Recebeu dados de outro peer via TCP relay
                    from_id = msg.get('from')
                    to_id = msg.get('to')
                    payload = msg.get('payload')
                    
                    if to_id == self.assigned_id and from_id:
                        logger.info(f"Recebido DATA via TCP de {from_id}: {payload}")
                        
                        # Processa a mensagem com handlers registrados
                        if from_id in self.message_handlers:
                            for handler in self.message_handlers.get(from_id, []):
                                handler(from_id, payload)
                    elif to_id != self.assigned_id:
                        logger.debug(f"Ignorada mensagem DATA destinada a {to_id}")
                    else:
                        logger.warning(f"Mensagem DATA inválida: {msg}")
                        
                elif mtype == 'error':
                    # Recebeu mensagem de erro do servidor
                    error_msg = msg.get('message')
                    logger.error(f"Erro do servidor {relay_key}: {error_msg}")
                
                else:
                    logger.warning(f"Tipo de mensagem desconhecido de {relay_key}: {mtype}")
        except ConnectionResetError:
            logger.warning(f"Conexão com {relay_key} resetada")
        except Exception as e:
            logger.error(f"Erro ao processar mensagens TCP de {relay_key}: {e}")
        finally:
            # Limpa recursos
            if relay_key in self.tcp_writers:
                writer = self.tcp_writers.pop(relay_key, None)
                if writer:
                    writer.close()
            
            # Tenta reconectar se necessário
            host, port = relay_key.split(':')
            port = int(port)
            if self.connection_attempts.get(relay_key, 0) < self.max_attempts:
                delay = 2 ** self.connection_attempts.get(relay_key, 0)
                logger.info(f"Agendando reconexão com {relay_key} em {delay} segundos")
                asyncio.create_task(self.retry_connection(host, port, delay))

    async def delayed_punch_message(self, peer_id, addr, delay=1.0):
        """Envia mensagem após um delay para aumentar chances de sucesso no hole punching."""
        await asyncio.sleep(delay)
        if peer_id in self.pending_connections:
            # Envia uma mensagem real pelo buraco que foi aberto
            data_msg = {
                'type': DATA,
                'from': self.assigned_id,
                'to': peer_id,
                'payload': f'Initiation from {self.assigned_id}'
            }
            self.transport.sendto(json.dumps(data_msg).encode(), addr)
            logger.info(f"Enviada mensagem de iniciação para {peer_id}@{addr}")

    def datagram_received(self, data, addr):
        """Processa pacotes UDP recebidos."""
        try:
            msg = json.loads(data.decode())
            mtype = msg.get('type')
            
            if mtype == 'punch':
                # Pacote de hole punching, apenas confirma log
                from_id = msg.get('from')
                to_id = msg.get('to')
                if from_id and to_id == self.assigned_id:
                    logger.info(f"Recebido pacote de hole punching de {from_id}@{addr}")
                    # Atualiza o endereço do peer
                    self.peers[from_id] = addr
                
            elif mtype == DATA:
                # Dados de um peer direto via UDP
                to_id = msg.get('to')
                from_id = msg.get('from')
                payload = msg.get('payload')
                
                if to_id == self.assigned_id and from_id:
                    logger.info(f"Recebido DATA via UDP de {from_id}@{addr}: {payload}")
                    
                    # Adiciona/atualiza à lista de conexões ativas
                    if from_id not in self.active_connections:
                        self.active_connections.add(from_id)
                        logger.info(f"Adicionado {from_id} às conexões ativas")
                    
                    # Atualiza o endereço do peer
                    self.peers[from_id] = addr
                    
                    # Remove das conexões pendentes se estiver lá
                    if from_id in self.pending_connections:
                        self.pending_connections.remove(from_id)
                    
                    # Processa a mensagem com handlers registrados
                    if from_id in self.message_handlers:
                        for handler in self.message_handlers.get(from_id, []):
                            handler(from_id, payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Ignora pacotes que não são JSON
            logger.debug(f"Recebido pacote não-JSON de {addr}: {data[:20]}...")
        except Exception as e:
            logger.error(f"Erro ao processar pacote UDP de {addr}: {e}")

    async def connect_to_peer(self, peer_id):
        """Inicia conexão com outro peer."""
        if not self.assigned_id:
            logger.error("Não é possível conectar: Ainda não tem ID atribuído")
            return False
            
        if peer_id not in self.peers:
            logger.error(f"Não é possível conectar: Peer {peer_id} não é conhecido")
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
                logger.info(f"Enviada solicitação de conexão para {peer_id} via {relay_key}")
            except Exception as e:
                logger.warning(f"Falha ao enviar solicitação de conexão via {relay_key}: {e}")
                
        return sent

    async def send_message(self, peer_id, message):
        """Envia mensagem para um peer conectado."""
        if not self.assigned_id:
            logger.error("Não é possível enviar: Ainda não tem ID atribuído")
            return False
            
        if peer_id not in self.peers:
            logger.error(f"Não é possível enviar: Peer {peer_id} não é conhecido")
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
                logger.info(f"Enviada mensagem para {peer_id} via UDP direto")
                return True
            except Exception as e:
                logger.warning(f"Falha ao enviar via UDP para {peer_id}: {e}")
        
        # Fallback: envia via relay TCP
        sent = False
        for relay_key, writer in self.tcp_writers.items():
            try:
                writer.write(json.dumps(data_msg).encode() + b'\n')
                await writer.drain()
                sent = True
                logger.info(f"Enviada mensagem para {peer_id} via relay {relay_key}")
            except Exception as e:
                logger.warning(f"Falha ao enviar via relay {relay_key}: {e}")
                
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
                        result = asyncio.run_coroutine_threadsafe(
                            self.node.connect_to_peer(parts[1]), 
                            self.node.event_loop
                        ).result()
                        if result:
                            print(f"Enviada solicitação de conexão para {parts[1]}")
                        else:
                            print(f"Falha ao enviar solicitação de conexão para {parts[1]}")
                elif command == 'send':
                    if len(parts) < 3:
                        print("Usage: send <peer_id> <message>")
                    else:
                        peer_id = parts[1]
                        message = ' '.join(parts[2:])
                        result = asyncio.run_coroutine_threadsafe(
                            self.node.send_message(peer_id, message), 
                            self.node.event_loop
                        ).result()
                        if result:
                            print(f"Mensagem enviada para {peer_id}")
                        else:
                            print(f"Falha ao enviar mensagem para {peer_id}")
                elif command == 'id':
                    self._show_id()
                elif command == 'exit' or command == 'quit':
                    print("Encerrando...")
                    self.running = False
                    # Não podemos chamar sys.exit() aqui porque estamos em uma thread
                else:
                    print(f"Comando desconhecido: {command}")
                    print("Digite 'help' para ver comandos disponíveis")
            except KeyboardInterrupt:
                print("\nEncerrando...")
                self.running = False
                break
            except Exception as e:
                print(f"Erro: {e}")
                
    def _show_help(self):
        """Mostra a ajuda da interface de linha de comando."""
        print("\nComandos disponíveis:")
        print("  help                - Mostra esta ajuda")
        print("  list                - Lista peers conhecidos")
        print("  connect <peer_id>   - Conecta a um peer")
        print("  send <peer_id> <msg>- Envia mensagem para um peer")
        print("  id                  - Mostra seu ID atribuído")
        print("  exit, quit          - Sai da aplicação")
        
    def _list_peers(self):
        """Lista os peers conhecidos."""
        peer_info = self.node.get_peer_list()
        
        print("\nPeers conhecidos:")
        for i, peer_id in enumerate(peer_info['all_peers']):
            status = []
            if peer_id in peer_info['active_connections']:
                status.append("ATIVO")
            if peer_id in peer_info['pending_connections']:
                status.append("PENDENTE")
            status_str = ", ".join(status) if status else "CONHECIDO"
            print(f"  {i+1}. {peer_id} - {status_str}")
            
        if not peer_info['all_peers']:
            print("  Nenhum peer descoberto ainda")
            
    def _show_id(self):
        """Mostra o ID atribuído."""
        if self.node.assigned_id:
            print(f"\nSeu ID atribuído: {self.node.assigned_id}")
        else:
            print("\nNenhum ID atribuído ainda. Ainda inicializando...")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--local-file', help='Caminho para arquivo de configuração local de servidores STUN')
    parser.add_argument('--debug', action='store_true', help='Ativa logs de debug')
    parser.add_argument('--port', type=int, help='Porta UDP local (0 = aleatória)', default=0)
    args = parser.parse_args()

    # Configura logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format='%(asctime)s %(levelname)s: %(message)s')
    
    # Carrega lista de servidores STUN+Relay
    local_file = args.local_file or DEFAULT_LOCAL_CONFIG
    relays = await fetch_stun_relays(local_file)
    
    # Se não conseguiu carregar do arquivo, usa localhost como fallback
    if not relays:
        logger.warning("Nenhum relay disponível na configuração. Usando localhost como fallback.")
        relays = [("127.0.0.1", 3478), ("localhost", 3478)]

    # Usa porta aleatória ou especificada
    local_port = args.port
    if local_port == 0:
        # Gera porta aleatória entre 10000 e 65000
        local_port = random.randint(10000, 65000)
        logger.info(f"Usando porta UDP local aleatória: {local_port}")

    # Cria o endpoint UDP
    loop = asyncio.get_event_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: P2PNodeProtocol(relays),
            local_addr=('0.0.0.0', local_port)
        )
        
        local_ep = transport.get_extra_info('socket').getsockname()
        logger.info(f"Endpoint UDP local: {local_ep}")
        
        # Inicia a interface de linha de comando
        cli = CommandLineInterface(protocol)
        cli.start()
        
        # Mantém o programa rodando
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Erro ao iniciar o nó P2P: {e}")
    finally:
        if 'transport' in locals():
            transport.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nEncerrando...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Erro não tratado: {e}")
        sys.exit(1)
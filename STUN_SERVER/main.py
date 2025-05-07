#!/usr/bin/env python3
"""
udp_p2p_server.py

STUN+Relay Server:
- UDP STUN Binding Requests (port 54321)
- TCP signaling (port 3478) for:
  * HELLO: client temp_id, server responds ASSIGN with secure unique ID
  * SYNC: client registers assigned_id and addr, server broadcasts sync_register to all other clients
  * DATA: server forwards data messages to target clients via TCP
  * CONNECT: client requests connection to another client, server assists with hole punching

Usage:
  python udp_p2p_server.py --stun-port 54321 --tcp-port 3478
"""
import argparse
import asyncio
import json
import struct
import socket
import logging
import uuid
import secrets
import time
from typing import Dict, Tuple, List, Set

# STUN constants
MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_SUCCESS_RESPONSE = 0x0101
XOR_MAPPED_ADDRESS = 0x0020

# Signaling message types
HELLO = 'hello'
ASSIGN = 'assign_id'
SYNC = 'sync_register'
DATA = 'data'
CONNECT = 'connect'
HOLEPUNCH = 'holepunch'

logger = logging.getLogger('udp_p2p_server')

class StunProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        super().__init__()
    def datagram_received(self, data: bytes, addr: Tuple[str,int]):
        if len(data) < 20:
            return
        msg_type, length, magic = struct.unpack('!HHI', data[:8])
        if msg_type != BINDING_REQUEST or magic != MAGIC_COOKIE:
            return
        transaction_id = data[8:20]
        # prepare XOR-MAPPED-ADDRESS attribute
        host, port = addr
        xport = port ^ (MAGIC_COOKIE >> 16)
        ip_int = struct.unpack('!I', socket.inet_aton(host))[0] ^ MAGIC_COOKIE
        attr_value = struct.pack('!BBH', 0, socket.AF_INET, xport) + struct.pack('!I', ip_int)
        attr_header = struct.pack('!HH', XOR_MAPPED_ADDRESS, len(attr_value))
        body = attr_header + attr_value
        header = struct.pack('!HHI', BINDING_SUCCESS_RESPONSE, len(body), MAGIC_COOKIE) + transaction_id
        self.transport.sendto(header + body, addr)
        logger.debug(f"STUN binding reply sent to {addr}")

class RelayServer:
    def __init__(self):
        self.clients: Dict[str, asyncio.StreamWriter] = {}
        self.addrs: Dict[str, Tuple[str,int]] = {}
        self.connections: Dict[str, Set[str]] = {}  # Tracks active connections for each client

    async def handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peername = writer.get_extra_info('peername')
        logger.info(f"TCP connection from {peername}")
        temp_id = None
        assigned = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode())
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON received: {line}")
                    continue
                    
                mtype = msg.get('type')
                logger.info(f"Received message type: {mtype}")
                
                # Aceita tanto HELLO quanto REGISTER (para compatibilidade)
                if mtype == HELLO or mtype == REGISTER:
                    # Extrai o ID temporário (pode ser temp_id ou peer_id)
                    temp_id = msg.get('temp_id') or msg.get('peer_id')
                    if not temp_id:
                        logger.warning(f"Message missing ID: {msg}")
                        continue
                        
                    # Extrai o endereço
                    addr_list = msg.get('addr')
                    if addr_list and len(addr_list) >= 2:
                        addr = tuple(addr_list[:2])  # Pega apenas host e porta
                    else:
                        addr = peername
                    # Gera um ID seguro (ou usa o existente para REGISTER)
                    if mtype == REGISTER and 'peer_id' in msg:
                        assigned = msg['peer_id']
                        logger.info(f"Using existing ID {assigned} from REGISTER")
                    else:
                        # Generate a secure connection ID (32 characters)
                        assigned = f"peer-{uuid.uuid4().hex}-{secrets.token_hex(8)}"
                        logger.info(f"Generated new ID {assigned}")
                    # Registra o cliente
                    self.clients[assigned] = writer
                    self.addrs[assigned] = addr
                    self.connections[assigned] = set()  # Initialize empty connections set
                    
                    # Responde com o ID atribuído
                    if mtype == HELLO:
                        resp = {'type': ASSIGN, 'temp_id': temp_id, 'assigned_id': assigned}
                    else:  # REGISTER
                        resp = {'type': SYNC, 'peer_id': assigned, 'addr': list(addr)}
                        
                    writer.write(json.dumps(resp).encode() + b'\n')
                    await writer.drain()
                    logger.info(f"Assigned ID {assigned} to client {temp_id}@{addr}")
                elif mtype == SYNC:
                    # Extrai o ID do peer
                    pid = msg.get('peer_id')
                    if not pid:
                        logger.warning(f"SYNC message missing peer_id: {msg}")
                        continue
                        
                    # Extrai o endereço
                    addr_list = msg.get('addr')
                    if not addr_list or len(addr_list) < 2:
                        logger.warning(f"SYNC message has invalid addr: {msg}")
                        continue
                        
                    addr = tuple(addr_list[:2])  # Pega apenas host e porta
                    # Atualiza o endereço do peer
                    self.addrs[pid] = addr
                    
                    # Broadcast para todos os outros clientes
                    broadcast_count = 0
                    for other_id, w in self.clients.items():
                        if other_id != pid:
                            try:
                                out = {'type': SYNC, 'peer_id': pid, 'addr': list(addr)}
                                w.write(json.dumps(out).encode() + b'\n')
                                await w.drain()
                                broadcast_count += 1
                            except Exception as e:
                                logger.warning(f"Failed to broadcast to {other_id}: {e}")
                                
                    logger.info(f"Synced peer {pid}@{addr} to {broadcast_count} other peers")
                elif mtype == DATA:
                    # Extrai o destinatário
                    dest = msg.get('to')
                    if not dest:
                        logger.warning(f"DATA message missing 'to' field: {msg}")
                        continue
                        
                    # Encaminha a mensagem
                    if dest in self.clients:
                        try:
                            w = self.clients[dest]
                            w.write(line)
                            await w.drain()
                            logger.info(f"Forwarded DATA from {msg.get('from')} to {dest}")
                        except Exception as e:
                            logger.warning(f"Failed to forward DATA to {dest}: {e}")
                    else:
                        logger.warning(f"DATA destination not found: {dest}")
                
                elif mtype == CONNECT:
                    # Extrai os IDs de origem e destino
                    from_id = msg.get('from')
                    to_id = msg.get('to')
                    
                    if not from_id or not to_id:
                        logger.warning(f"CONNECT message missing from/to: {msg}")
                        continue
                    
                    logger.info(f"Connection request from {from_id} to {to_id}")
                    
                    # Verifica se ambos os peers estão registrados
                    if from_id in self.addrs and to_id in self.addrs and to_id in self.clients:
                        # Adiciona ao rastreamento de conexões
                        if from_id in self.connections:
                            self.connections[from_id].add(to_id)
                            
                        # Obtém os endereços
                        from_addr = self.addrs[from_id]
                        to_addr = self.addrs[to_id]
                        
                        logger.info(f"Initiating connection: {from_id}@{from_addr} -> {to_id}@{to_addr}")
                        
                        try:
                            # Envia solicitação de holepunch para o destino
                            holepunch_msg = {
                                'type': HOLEPUNCH,
                                'from': from_id,
                                'addr': list(from_addr),
                                'timestamp': time.time()
                            }
                            
                            # Envia para o cliente de destino
                            to_writer = self.clients[to_id]
                            to_writer.write(json.dumps(holepunch_msg).encode() + b'\n')
                            await to_writer.drain()
                            logger.info(f"Sent holepunch request to {to_id}")
                        except Exception as e:
                            logger.warning(f"Failed to send holepunch to {to_id}: {e}")
                            writer.write(json.dumps({
                                'type': 'error',
                                'message': f"Failed to contact peer {to_id}",
                                'timestamp': time.time()
                            }).encode() + b'\n')
                            await writer.drain()
                            continue
                        
                        try:
                            # Envia confirmação de volta para o solicitante
                            confirm_msg = {
                                'type': 'connect_initiated',
                                'to': to_id,
                                'addr': list(to_addr),
                                'timestamp': time.time()
                            }
                            writer.write(json.dumps(confirm_msg).encode() + b'\n')
                            await writer.drain()
                            
                            logger.info(f"Initiated connection from {from_id} to {to_id}")
                        except Exception as e:
                            logger.warning(f"Failed to send confirmation to {from_id}: {e}")
                    else:
                        # Peer de destino não encontrado
                        logger.warning(f"Connection target not found: {to_id}")
                        try:
                            error_msg = {
                                'type': 'error',
                                'message': f"Peer {to_id} not found or not connected",
                                'timestamp': time.time()
                            }
                            writer.write(json.dumps(error_msg).encode() + b'\n')
                            await writer.drain()
                        except Exception as e:
                            logger.warning(f"Failed to send error to {from_id}: {e}")
        except ConnectionResetError:
            logger.info(f"Connection reset by peer: {peername}")
        except Exception as e:
            logger.error(f"Error handling TCP connection: {e}")
        finally:
            if assigned:
                self.clients.pop(assigned, None)
                self.addrs.pop(assigned, None)
                self.connections.pop(assigned, None)
            writer.close()
            await writer.wait_closed()
            logger.info(f"TCP connection closed for {assigned or peername}")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stun-port', type=int, default=54321)
    parser.add_argument('--tcp-port', type=int, default=3478)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    loop = asyncio.get_running_loop()

    # STUN UDP
    stun_listen = loop.create_datagram_endpoint(StunProtocol, local_addr=('0.0.0.0', args.stun_port))
    await stun_listen
    logger.info(f"STUN server listening on UDP {args.stun_port}")

    # Relay TCP
    relay = RelayServer()
    server = await asyncio.start_server(relay.handle_tcp, '0.0.0.0', args.tcp_port)
    logger.info(f"Relay server listening on TCP {args.tcp_port}")

    async with server:
        await server.serve_forever()

if __name__ == '__main__':
    asyncio.run(main())

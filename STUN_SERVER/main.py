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
                msg = json.loads(line.decode())
                mtype = msg.get('type')
                if mtype == HELLO:
                    temp_id = msg['temp_id']
                    addr = tuple(msg.get('addr', peername))
                    # Generate a secure connection ID (32 characters)
                    assigned = f"peer-{uuid.uuid4().hex}-{secrets.token_hex(8)}"
                    self.clients[assigned] = writer
                    self.addrs[assigned] = addr
                    self.connections[assigned] = set()  # Initialize empty connections set
                    resp = {'type': ASSIGN, 'temp_id': temp_id, 'assigned_id': assigned}
                    writer.write(json.dumps(resp).encode() + b'\n')
                    await writer.drain()
                    logger.info(f"Assigned ID {assigned} to temp {temp_id}")
                elif mtype == SYNC:
                    pid = msg['peer_id']
                    addr = tuple(msg['addr'])
                    self.addrs[pid] = addr
                    # broadcast to all other clients
                    for other_id, w in self.clients.items():
                        if other_id != pid:
                            out = {'type': SYNC, 'peer_id': pid, 'addr': addr}
                            w.write(json.dumps(out).encode() + b'\n')
                            await w.drain()
                    logger.info(f"Synced peer {pid}@{addr}")
                elif mtype == DATA:
                    dest = msg.get('to')
                    if dest in self.clients:
                        w = self.clients[dest]
                        w.write(line)
                        await w.drain()
                        logger.info(f"Forwarded DATA from {msg.get('from')} to {dest}")
                
                elif mtype == CONNECT:
                    # Client wants to connect to another peer
                    from_id = msg.get('from')
                    to_id = msg.get('to')
                    
                    if from_id in self.addrs and to_id in self.addrs and to_id in self.clients:
                        # Add to connections tracking
                        if from_id in self.connections:
                            self.connections[from_id].add(to_id)
                        
                        # Get addresses
                        from_addr = self.addrs[from_id]
                        to_addr = self.addrs[to_id]
                        
                        # Send holepunch request to target
                        holepunch_msg = {
                            'type': HOLEPUNCH,
                            'from': from_id,
                            'addr': list(from_addr),
                            'timestamp': time.time()
                        }
                        
                        # Send to target client
                        to_writer = self.clients[to_id]
                        to_writer.write(json.dumps(holepunch_msg).encode() + b'\n')
                        await to_writer.drain()
                        
                        # Send confirmation back to requester
                        confirm_msg = {
                            'type': 'connect_initiated',
                            'to': to_id,
                            'addr': list(to_addr),
                            'timestamp': time.time()
                        }
                        writer.write(json.dumps(confirm_msg).encode() + b'\n')
                        await writer.drain()
                        
                        logger.info(f"Initiated connection from {from_id} to {to_id}")
        except ConnectionResetError:
            pass
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

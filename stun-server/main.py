import asyncio
import struct
import socket
import json

# STUN constants
MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_SUCCESS_RESPONSE = 0x0101
XOR_MAPPED_ADDRESS = 0x0020

class StunRelayProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        super().__init__()
        # Map peer_id to (host, port)
        self.peers = {}

    def connection_made(self, transport):
        self.transport = transport
        print("STUN+Relay server started on UDP port 54321.")

    def datagram_received(self, data, addr):
        # Try JSON-based relay commands first
        try:
            msg = json.loads(data.decode('utf-8'))
            msg_type = msg.get('type')
            if msg_type == 'register':
                peer_id = msg.get('peer_id')
                if peer_id:
                    self.peers[peer_id] = addr
                    resp = json.dumps({'status': 'registered'})
                    self.transport.sendto(resp.encode('utf-8'), addr)
                    print(f"Registered peer '{peer_id}' at {addr}")
                return
            elif msg_type == 'data':
                dest = msg.get('to')
                payload = msg.get('payload')
                if dest in self.peers and payload is not None:
                    peer_addr = self.peers[dest]
                    # Forward raw payload
                    self.transport.sendto(payload.encode('utf-8'), peer_addr)
                    print(f"Forwarded data from {addr} to {dest} at {peer_addr}")
                return
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Not a relay message; fall back to STUN handling
            pass

        # STUN handling for Binding Request
        if len(data) < 20:
            return
        msg_type, msg_len, magic = struct.unpack('!HHI', data[:8])
        transaction_id = data[8:20]
        if msg_type != BINDING_REQUEST or magic != MAGIC_COOKIE:
            return

        host, port = addr
        # XOR port and address
        xport = port ^ (MAGIC_COOKIE >> 16)
        ip_int = struct.unpack('!I', socket.inet_aton(host))[0] ^ MAGIC_COOKIE
        attr_value = struct.pack('!BBH', 0, socket.AF_INET, xport) + struct.pack('!I', ip_int)
        attr_header = struct.pack('!HH', XOR_MAPPED_ADDRESS, len(attr_value))
        response_body = attr_header + attr_value
        header = struct.pack('!HHI', BINDING_SUCCESS_RESPONSE, len(response_body), MAGIC_COOKIE) + transaction_id
        response = header + response_body
        self.transport.sendto(response, addr)
        print(f"Replied STUN Binding to {addr}")

async def main():
    loop = asyncio.get_running_loop()
    # Listen on a high UDP port (54321)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: StunRelayProtocol(),
        local_addr=('0.0.0.0', 54321)
    )
    try:
        await asyncio.Event().wait()
    finally:
        transport.close()

if __name__ == '__main__':
    asyncio.run(main())
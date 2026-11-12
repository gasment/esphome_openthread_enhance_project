"""Direct native reverse TCP socket adoption for HA 2026.11+."""
from __future__ import annotations
import asyncio
import logging
import re
import socket
from aioesphomeapi import APIConnectionError

_LOGGER = logging.getLogger(__name__)

def normalize_mac(value: str) -> str:
    value = value.replace(':', '').replace('-', '').lower()
    if not re.fullmatch(r'[0-9a-f]{12}', value):
        raise ValueError('Invalid MAC address')
    return value

PLAIN_PREFIX = b'TSED1 '
PLAIN_IDENTITY_SIZE = 24

def decode_plain_identity(data):
    if len(data) < len(PLAIN_PREFIX):
        return None
    if not data.startswith(PLAIN_PREFIX):
        raise ValueError('Invalid plaintext reverse identity')
    if len(data) < PLAIN_IDENTITY_SIZE:
        return None
    if data[PLAIN_IDENTITY_SIZE-1] != 10:
        raise ValueError('Invalid plaintext identity terminator')
    return normalize_mac(data[6:23].decode('ascii'))

def decode_server_hello(data):
    if len(data) < 3:
        return None
    size = int.from_bytes(data[1:3], 'big')
    if data[0] != 1 or not 2 <= size <= 512:
        raise ValueError('Invalid Noise server hello')
    if len(data) < size+3:
        return None
    payload = data[3:size+3]
    fields = payload.split(b'\0')
    if payload[0] != 1 or len(fields) < 3:
        raise ValueError('Invalid Noise identity')
    return fields[0][1:].decode('utf-8'), normalize_mac(fields[1].decode('ascii'))

async def peek_reverse_identity(sock):
    # Leave every byte unread for aioesphomeapi's native Noise handshake.
    async with asyncio.timeout(5):
        while True:
            try:
                data = sock.recv(515, socket.MSG_PEEK)
            except BlockingIOError:
                data = None
            if data == b'':
                raise ConnectionError('Peer closed before reverse identity')
            if data:
                if data[0] == 1:
                    if (identity := decode_server_hello(data)) is not None:
                        return identity[1], 'noise'
                elif data[0] == PLAIN_PREFIX[0]:
                    if (mac := decode_plain_identity(data)) is not None:
                        return mac, 'none'
                else:
                    raise ValueError('Unknown reverse transport')
            await asyncio.sleep(.01)

class ReverseListener:
    def __init__(self, sessions):
        self.sessions = sessions
        self.socket = None
        self.accept_task = None
        self.tasks = set()

    async def start(self, host='::', port=6064):
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
            sock.setblocking(False)
            sock.bind((host,port))
            sock.listen(16)
        except BaseException:
            sock.close()
            raise
        self.socket = sock
        self.accept_task = asyncio.create_task(self._accept())

    async def _accept(self):
        loop = asyncio.get_running_loop()
        while True:
            sock,peer = await loop.sock_accept(self.socket)
            if len(self.tasks) >= 16:
                sock.close()
                continue
            task = asyncio.create_task(self._route(sock,peer))
            self.tasks.add(task)
            task.add_done_callback(self._finished)

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            _LOGGER.debug('Reverse socket closed: %s',type(task.exception()).__name__)

    async def _route(self, sock, peer):
        adopted = False
        try:
            mac,mode = await peek_reverse_identity(sock)
            session = self.sessions.get(mac)
            if session is None or session.connecting or session.online:
                return
            if session.entry.data.get('transport','tcp')=='udp':
                return
            expected_mode = 'noise' if session.entry.data.get('noise_psk') else 'none'
            if mode != expected_mode:
                raise ValueError('Transport does not match configured encryption')
            if mode == 'none':
                # Peek completed the entire preface; consume exactly those bytes.
                # Native plaintext frames that follow remain unread.
                remaining = PLAIN_IDENTITY_SIZE
                while remaining:
                    consumed = await asyncio.get_running_loop().sock_recv(sock,remaining)
                    if not consumed: raise ConnectionError('Truncated plaintext identity')
                    remaining -= len(consumed)
            await session.connect(peer[0],peer[1],sock=sock)
            adopted = True
        except (OSError, ValueError, TimeoutError, APIConnectionError):
            _LOGGER.debug('Reverse connection was not accepted',exc_info=True)
        finally:
            if not adopted:
                sock.close()

    async def close(self):
        if self.accept_task:
            self.accept_task.cancel()
            await asyncio.gather(self.accept_task,return_exceptions=True)
        if self.socket:
            self.socket.close()
        pending = tuple(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)

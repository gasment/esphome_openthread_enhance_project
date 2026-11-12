"""Small IPv6 v3 demo node. Uses only stdlib; does not import Home Assistant."""
import argparse
import asyncio
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import secrets
import sys
import types

package = types.ModuleType("otgw_demo")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components" / "openthread_gateway")]
sys.modules[package.__name__] = package
wire = importlib.import_module("otgw_demo.protocol")


class Demo(asyncio.DatagramProtocol):
    def __init__(self, args):
        self.args = args
        self.path = Path(args.state)
        self.saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.saved.setdefault("device", secrets.token_hex(8))
        self.saved["boot"] = self.saved.get("boot", 0) + 1
        self.saved.setdefault("generation", 0)
        self.session, self.request = secrets.token_hex(8), secrets.token_hex(8)
        self.context = ""
        self.queue = asyncio.Queue(maxsize=64)
        self.ready = self.manifest_ready = False
        self.sequence = 0
        self.relay = False
        self.command_results = {}
        self.pending_report = None
        self.manifest = json.dumps({"name": "Protocol demo", "entities": [
            {"id": "temperature", "type": "numeric", "name": "Temperature", "unit": "°C", "state_class": "measurement"},
            {"id": "door", "type": "binary", "name": "Door", "device_class": "door"},
            {"id": "relay", "type": "switch", "name": "Relay"}]}, separators=(",", ":")).encode()
        self.digest = hashlib.sha256(self.manifest).hexdigest()
        self.persist()

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            os.chmod(temporary, 0o600)
            json.dump(self.saved, file)
        os.replace(temporary, self.path)

    def connection_made(self, transport): self.transport = transport
    def datagram_received(self, packet, peer):
        try: self.queue.put_nowait(packet)
        except asyncio.QueueFull: pass

    def base(self, kind):
        return {"v": 3, "type": kind, "device": self.saved["device"], "backend_id": self.saved.get("backend_id", ""),
                "binding_id": self.saved.get("binding_id", ""), "session": self.session, "ctx": self.context}

    def send(self, kind, **fields):
        self.transport.sendto(wire.encode({**self.base(kind), **fields}, self.saved.get("secret")))

    def hello(self):
        self.send("hello", node="protocol_demo", name="Protocol demo", network=self.args.network, node_type="reed",
                  boot=self.saved["boot"], generation=self.saved["generation"], request_id=self.request,
                  expected_backend=self.args.backend_id)

    def send_manifest(self):
        chunks = [self.manifest[index:index + 512] for index in range(0, len(self.manifest), 512)]
        for index, chunk in enumerate(chunks):
            self.send("manifest", manifest_hash=self.digest, part=index, parts=len(chunks), payload=base64.b64encode(chunk).decode())

    def state(self):
        if self.pending_report is None:
            self.sequence = (self.sequence + 1) & 0xFFFFFFFF
            self.pending_report = dict(manifest_hash=self.digest, report_id=self.sequence, seq=self.sequence, part=0, parts=1,
                  values={"temperature": 22.5, "door": False, "relay": self.relay}, heartbeat_interval_ms=5000, control_ready=True)
        self.send("state", **self.pending_report)

    async def handle(self, packet):
        message, signature, raw = wire.decode(packet)
        if message.get("device") != self.saved["device"] or message.get("session") != self.session:
            return
        if message["type"] == "bind_offer" and not self.saved.get("secret"):
            if signature != "-" or message.get("request_id") != self.request or message.get("backend_id") != self.args.backend_id or not wire.hex_id(message.get("secret"), 64) or not wire.hex_id(message.get("binding_id"), 32):
                return
            self.saved.update({key: message[key] for key in ("secret", "backend_id", "binding_id", "generation")})
            self.persist()
            self.send("bind_ack", request_id=self.request)
            return
        if not self.saved.get("secret") or message.get("backend_id") != self.saved.get("backend_id") or message.get("binding_id") != self.saved.get("binding_id") or not wire.authenticated(signature, raw, self.saved["secret"]):
            return
        kind = message["type"]
        if kind == "binding_revoked":
            self.saved["generation"] += 1
            for key in ("secret", "backend_id", "binding_id"): self.saved.pop(key, None)
            self.persist()
            self.context = ""
            self.ready = self.manifest_ready = False
            self.request = secrets.token_hex(8)
            print("Binding revoked; waiting for manual admission again.", flush=True)
        elif kind in {"bind_complete", "context_expired"}:
            self.ready = self.manifest_ready = False
            self.hello()
        elif kind == "session_offer" and message.get("request_id") == self.request and wire.hex_id(message.get("ctx"), 16):
            if self.context != message["ctx"]:
                self.command_results.clear()
                self.pending_report = None
                self.context = message["ctx"]
            self.send("session_ack", request_id=self.request)
        elif kind == "session_ready" and message.get("ctx") == self.context:
            self.ready = True
            self.manifest_ready = message.get("manifest_hash") == self.digest
            if not self.manifest_ready: self.send_manifest()
            else: self.state()
        elif message.get("ctx") == self.context:
            if kind == "manifest_request":
                self.manifest_ready = False
                self.send_manifest()
            elif kind == "manifest_ack" and message.get("complete") and message.get("manifest_hash") == self.digest:
                self.manifest_ready = True
                self.state()
            elif kind == "report_ack" and message.get("complete") and self.pending_report and message.get("report_id") == self.pending_report["report_id"] and message.get("seq") == self.pending_report["seq"]:
                self.pending_report = None
            elif kind == "command":
                command_id = message.get("command_id")
                if command_id not in self.command_results:
                    success = message.get("entity") == "relay" and message.get("operation") == "set" and type(message.get("value")) is bool
                    if success: self.relay = message["value"]
                    self.sequence = (self.sequence + 1) & 0xFFFFFFFF
                    self.command_results[command_id] = {"command_id": command_id, "success": success, "value": self.relay, "state_seq": self.sequence}
                    while len(self.command_results) > 64: self.command_results.pop(next(iter(self.command_results)))
                    print(f"Relay command {command_id}: accepted={success}, state={self.relay}", flush=True)
                self.send("command_result", **self.command_results[command_id])

    async def run(self):
        print(f'Device {self.saved["device"]}; select Protocol demo in HA integration options.', flush=True)
        while True:
            if not self.ready: self.hello()
            elif not self.manifest_ready: self.send_manifest()
            else: self.state()
            deadline = asyncio.get_running_loop().time() + 5
            while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
                try:
                    packet = await asyncio.wait_for(self.queue.get(), remaining)
                    await self.handle(packet)
                except asyncio.TimeoutError: break
                except wire.ProtocolError as error: print(f"Ignored invalid packet: {error}", flush=True)


async def main(args):
    demo = Demo(args)
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: demo, remote_addr=(args.host, args.port), family=__import__('socket').AF_INET6)
    try: await demo.run()
    finally: transport.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Routable HA IPv6 address, no brackets or URL')
    parser.add_argument('--port', type=int, default=18513)
    parser.add_argument('--backend-id', required=True)
    parser.add_argument('--network', default='1234567890abcdef')
    parser.add_argument('--state', default='otgw-demo-node.json', help='Private persistent binding file')
    args = parser.parse_args()
    if not wire.identifier(args.backend_id) or not wire.hex_id(args.network, 16): parser.error('Invalid backend ID or network ID')
    try: asyncio.run(main(args))
    except KeyboardInterrupt: pass

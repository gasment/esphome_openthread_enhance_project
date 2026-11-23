#!/usr/bin/env python3
"""Linux/DSM userspace RIO learner. Observe by default; --apply modifies routes."""
import argparse
import ipaddress
import json
import logging
import math
import os
import random
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time

LOG = logging.getLogger("dsm_ra")
PROTOCOL = "9"  # RTPROT_RA. Ownership is tracked separately; never flush proto ra.
PREFERENCES = {1: "high", 0: "medium", 3: "low"}
# Separate preference tiers so Linux cannot merge different prefs into one ECMP.
PREFERENCE_RANK = {1: 0, 0: 1, 3: 2}
FORWARDING_RETRY_DELAY = 120
PROBE_TARGET_LIFETIME = 600
MAX_PROBE_TARGETS = 3


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    value = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while value >> 16:
        value = (value & 0xffff) + (value >> 16)
    return (~value) & 0xffff


def parse_ra(packet, source, destination, hop_limit, include_default=False):
    """Validate RA and return RIOs; malformed option framing rejects the RA."""
    source = ipaddress.IPv6Address(source)
    if not source.is_link_local or hop_limit != 255:
        raise ValueError("RA source must be link-local and hop limit must be 255")
    if len(packet) < 16 or packet[0] != 134 or packet[1] != 0:
        raise ValueError("invalid RA header")
    pseudo = source.packed + ipaddress.IPv6Address(destination).packed
    pseudo += struct.pack("!I3xB", len(packet), 58)
    if checksum(pseudo + packet):
        raise ValueError("invalid ICMPv6 checksum")
    routes = []
    if include_default:
        preference = (packet[5] >> 3) & 3
        routes.append((ipaddress.IPv6Network("::/0"),
                       0 if preference == 2 else preference,
                       struct.unpack("!H", packet[6:8])[0]))
    offset = 16
    while offset < len(packet):
        if offset + 2 > len(packet):
            raise ValueError("truncated option header")
        kind, units = packet[offset:offset + 2]
        size = units * 8
        if not size or offset + size > len(packet):
            raise ValueError("invalid option length")
        option = packet[offset:offset + size]
        offset += size
        if kind != 24:
            continue
        if units not in (1, 2, 3):
            continue
        plen, flags, lifetime = struct.unpack("!BBI", option[2:8])
        preference = (flags >> 3) & 3
        if plen > 128 or plen > (size - 8) * 8 or preference == 2:
            continue
        address = ipaddress.IPv6Address(option[8:].ljust(16, b"\0"))
        prefix = ipaddress.IPv6Network((address, plen), strict=False)
        routes.append((prefix, preference, lifetime))
    return routes


def no_route_destination(packet, source, destination):
    """Extract the quoted destination of an ICMPv6 No Route sent to this host."""
    if len(packet) < 48 or packet[:2] != b"\x01\x00" or packet[8] >> 4 != 6:
        return None
    destination = ipaddress.IPv6Address(destination)
    if destination.is_multicast or packet[16:32] != destination.packed:
        return None
    pseudo = ipaddress.IPv6Address(source).packed + destination.packed
    pseudo += struct.pack("!I3xB", len(packet), 58)
    if checksum(pseudo + packet):
        return None
    target = ipaddress.IPv6Address(packet[32:48])
    if target.is_multicast or target.is_link_local or target.is_unspecified:
        return None
    return target


class RouteState:
    def __init__(self):
        self.entries = {}
        self.forwarding_retry_at = {}
        self.forwarding_failed_target = {}
        self.probe_targets = {}

    def update(self, router, routes, now):
        for prefix, preference, lifetime in routes:
            key = (str(prefix), router)
            if lifetime == 0:
                self.entries.pop(key, None)
                self.forwarding_retry_at.pop(key, None)
                self.forwarding_failed_target.pop(key, None)
            else:
                expiry = math.inf if lifetime == 0xffffffff else now + lifetime
                self.entries[key] = (preference, expiry)

    def forwarding_error(self, source, target, now, installed):
        # Match the reporter against active routes. ESP BRs may use a LAN ULA
        # with the same interface ID as their link-local gateway address.
        source = ipaddress.IPv6Address(source)
        matching = [(prefix, preference, router)
                    for (prefix, preference), routes in installed.items()
                    if ipaddress.IPv6Network(prefix).prefixlen and target in ipaddress.IPv6Network(prefix)
                    for router, _ in routes if self.entries.get((prefix, router), (0, 0))[1] > now]
        if not matching:
            return
        prefix = max(matching, key=lambda row: ipaddress.IPv6Network(row[0]).prefixlen)[0]
        new_target = self.learn_probe_target(prefix, target, now)
        preference = min((p for n, p, _ in matching if n == prefix), key=PREFERENCE_RANK.get)
        gateways = {r for n, p, r in matching if n == prefix and p == preference}
        if source.is_link_local:
            reporters = gateways & {str(source)}
        else:
            reporters = {r for r in gateways
                         if ipaddress.IPv6Address(r).packed[8:] == source.packed[8:]}
        if len(reporters) == 1:
            self.reject_nexthop(prefix, reporters.pop(), target, now)
        return new_target

    def learn_probe_target(self, prefix, target, now):
        targets = self.probe_targets.setdefault(prefix, {})
        new_target = target not in targets
        targets.pop(target, None)
        targets[target] = now + PROBE_TARGET_LIFETIME
        if len(targets) > MAX_PROBE_TARGETS:
            del targets[next(iter(targets))]
        return new_target

    def reject_nexthop(self, prefix, router, target, now):
        key = (prefix, router)
        if self.entries.get(key, (0, 0))[1] <= now:
            return
        previous = self.forwarding_retry_at.get(key, 0)
        self.forwarding_retry_at[key] = now + FORWARDING_RETRY_DELAY
        self.forwarding_failed_target[key] = target
        if previous <= now:
            LOG.warning("No route to %s via %s; avoiding this nexthop for %s when another is available",
                        target, router, prefix)

    def restore_nexthop(self, prefix, router, target):
        key = (prefix, router)
        if self.forwarding_failed_target.get(key) == target:
            self.forwarding_failed_target.pop(key)
            self.forwarding_retry_at.pop(key, None)
            LOG.info("Forwarding probe succeeded; restored %s via %s", prefix, router)

    def selected(self, now, failed=()):
        """Group all usable advertisements; failed neighbors remain learned."""
        self.entries = {k: v for k, v in self.entries.items() if v[1] > now}
        advertised = {prefix for prefix, _ in self.entries}
        for prefix, targets in list(self.probe_targets.items()):
            recent = {target: expiry for target, expiry in targets.items() if expiry > now}
            if prefix in advertised and recent:
                self.probe_targets[prefix] = recent
            else:
                del self.probe_targets[prefix]
        self.forwarding_retry_at = {k: t for k, t in self.forwarding_retry_at.items()
                                    if k in self.entries and t > now}
        self.forwarding_failed_target = {k: v for k, v in self.forwarding_failed_target.items()
                                         if k in self.forwarding_retry_at}
        groups = {}
        fallback = {}
        for prefix, router in self.entries:
            if router not in failed:
                score = ((prefix, router) in self.forwarding_retry_at,
                         PREFERENCE_RANK[self.entries[(prefix, router)][0]], router)
                if prefix not in fallback or score < fallback[prefix]:
                    fallback[prefix] = score
        usable = [ipaddress.IPv6Network(prefix)
                  for prefix, router in self.entries if router not in failed]
        for (prefix, router), (preference, expiry) in self.entries.items():
            if ((prefix, router) in self.forwarding_retry_at and prefix in fallback
                    and fallback[prefix][2] != router):
                continue
            # A usable covering route permits fallback even to a shorter prefix.
            # If no alternative is known, retain failed routes so traffic can
            # trigger kernel NUD rather than creating a permanent dead end.
            if router in failed and any(ipaddress.IPv6Network(prefix).subnet_of(n)
                                        for n in usable):
                continue
            groups.setdefault((prefix, preference), []).append((router, expiry))
        return {k: tuple(sorted(v)) for k, v in groups.items()}


def parse_kernel_routes(output):
    """Read traditional iproute2 output, including indented IPv6 nexthops.

    No JSON-output dependency: DSM often ships an older iproute2. Return all
    unicast slots, including routes owned by other protocols, for collision checks.
    """
    records = []
    current = None
    for line in output.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] != "nexthop":
            if tokens[0] == "unicast":
                tokens = tokens[1:]
            try:
                prefix = str(ipaddress.IPv6Network("::/0" if tokens[0] == "default" else tokens[0]))
            except ValueError:
                current = None
                continue
            current = {"prefix": prefix, "metric": 1024, "pref": "medium",
                       "proto": None, "hops": []}
            for field in ("metric", "pref", "proto"):
                if field in tokens:
                    current[field] = tokens[tokens.index(field) + 1]
            current["metric"] = int(current["metric"])
            records.append(current)
        if current is not None and "via" in tokens and "dev" in tokens:
            current["hops"].append((tokens[tokens.index("via") + 1],
                                    tokens[tokens.index("dev") + 1],
                                    int(tokens[tokens.index("weight") + 1])
                                    if "weight" in tokens else 1))
    return records


class RouteWriter:
    def __init__(self, args):
        self.args = args
        self.installed = {}
        self.pending = {}

    def command(self, arguments):
        result = subprocess.run(
            [self.args.ip, "-6"] + arguments,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            timeout=5,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "ip route command failed")
        return result.stdout

    def metric(self, preference):
        return self.args.metric + PREFERENCE_RANK[preference]

    def slot(self, records, key):
        prefix, preference = key
        return [r for r in records if r["prefix"] == prefix
                and r["metric"] == self.metric(preference)]

    def owns(self, records, key, hops):
        slot = self.slot(records, key)
        return (len(slot) == 1 and slot[0]["proto"] in (PROTOCOL, "ra")
                and slot[0]["pref"] == PREFERENCES[key[1]]
                and sorted(slot[0]["hops"]) == sorted(
                    (router, self.args.interface, 1) for router, _ in hops))

    def arguments(self, verb, key, hops, now):
        prefix, preference = key
        args = ["route", verb, prefix, "table", self.args.table,
                "proto", PROTOCOL, "metric", str(self.metric(preference)),
                "pref", PREFERENCES[preference]]
        expiry = min(expiry for _, expiry in hops)
        if verb != "del" and not math.isinf(expiry):
            # A group lease ends with its earliest member. sync() rebuilds the
            # remaining group on expiry; no member may extend another's lifetime.
            args += ["expires", str(max(1, math.ceil(expiry - now)))]
        if len(hops) == 1:
            args += ["via", hops[0][0], "dev", self.args.interface]
        else:
            for router, _ in hops:
                args += ["nexthop", "via", router, "dev", self.args.interface, "weight", "1"]
        return args

    def sync(self, desired, now, save=lambda: None):
        records = parse_kernel_routes(self.command(["route", "show", "table", self.args.table]))
        # A write-ahead intent covers crashes/timeouts between the ip command
        # and recording its result. Only adopt an exact matching planned route.
        for key, hops in list(self.pending.items()):
            if self.owns(records, key, hops):
                self.installed[key] = hops
            del self.pending[key]
            save()
        blocked = set()
        for key, previous in list(self.installed.items()):
            if not self.owns(records, key, previous):
                # Expired, removed, or changed externally. Never replace a route
                # whose complete signature no longer matches our journal.
                del self.installed[key]
                save()
                continue
            if key not in desired:
                try:
                    self.command(self.arguments("del", key, previous, now))
                    records = [r for r in records if r not in self.slot(records, key)]
                    del self.installed[key]
                    save()
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    LOG.warning("Remove %s: %s", key, error)
                    blocked.add(key)
        for key, hops in desired.items():
            if key in blocked:
                continue
            previous = self.installed.get(key)
            if previous == hops:
                continue
            if previous is None and self.slot(records, key):
                LOG.warning("Route slot %s metric=%s already exists; leaving it untouched",
                            key[0], self.metric(key[1]))
                continue
            try:
                self.pending[key] = hops
                save()  # If durable intent fails, do not issue the mutation.
                self.command(self.arguments("replace" if previous else "add", key, hops, now))
                self.installed[key] = hops
                del self.pending[key]
                save()
                LOG.info("Installed %s pref=%s via %s", key[0], PREFERENCES[key[1]],
                         ",".join(router for router, _ in hops))
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                LOG.error("Install %s: %s", key, error)

    def failed_neighbors(self):
        failed = set()
        output = self.command(["neigh", "show", "dev", self.args.interface])
        for line in output.splitlines():
            fields = line.split()
            if "FAILED" in fields:
                failed.add(str(ipaddress.IPv6Address(fields[0])))
        return failed

    def relearn_failed_neighbor(self, router, packet):
        """Use a validated RA's Ethernet SLLAO to resume ordinary kernel NUD.

        Called only for FAILED routers with live advertisements. An unsolicited
        RA gives link-layer information, not a REACHABLE confirmation.
        """
        offset = 16
        while offset < len(packet):
            kind, units = packet[offset:offset + 2]
            if kind == 1 and units == 1:
                address = packet[offset + 2:offset + 8]
                if address == bytes(6) or address[0] & 1:
                    return False
                self.command(["neigh", "replace", router, "lladdr",
                              ":".join("%02x" % byte for byte in address),
                              "nud", "stale", "dev", self.args.interface, "router"])
                return True
            offset += units * 8
        return False


class Journal:
    """Same-boot recovery of advertisement deadlines and route ownership."""
    def __init__(self, path, args, state, writer, boot_id):
        self.path, self.args, self.state, self.writer = path, args, state, writer
        self.scope = [boot_id, args.interface, args.table, args.metric]

    def load(self):
        try:
            with open(self.path) as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return
        if not isinstance(data, dict) or not isinstance(data.get("scope"), list) or len(data["scope"]) != 4:
            raise ValueError("invalid journal scope")
        if data["scope"][0] != self.scope[0]:
            return  # Monotonic deadlines and kernel routes do not survive reboot.
        if data.get("version") != 1:
            raise ValueError("unsupported journal version")
        if data["scope"] != self.scope and not data.get("installed") and not data.get("pending"):
            return  # A clean shutdown leaves no owned routes to reinterpret.
        if data["scope"] != self.scope:
            raise RuntimeError("Journal scope/version changed; use the previous configuration to clean up first")
        # Build temporary maps: never partially restore a corrupt journal.
        entries, installed, pending = {}, {}, {}
        for prefix, router, pref, expiry in data["entries"]:
            prefix = str(ipaddress.IPv6Network(prefix))
            router = str(ipaddress.IPv6Address(router))
            if pref not in PREFERENCES or not ipaddress.IPv6Address(router).is_link_local:
                raise ValueError("invalid journal route")
            entries[(prefix, router)] = (pref, math.inf if expiry is None else float(expiry))
        for name, target in (("installed", installed), ("pending", pending)):
            for prefix, pref, hops in data.get(name, []):
                if pref not in PREFERENCES:
                    raise ValueError("invalid journal preference")
                key = (str(ipaddress.IPv6Network(prefix)), pref)
                restored = tuple((str(ipaddress.IPv6Address(r)), math.inf if e is None else float(e))
                                 for r, e in hops)
                if not restored or any(not ipaddress.IPv6Address(r).is_link_local for r, _ in restored):
                    raise ValueError("invalid journal nexthops")
                target[key] = restored
        self.state.entries, self.writer.installed = entries, installed
        self.writer.pending = pending

    def save(self):
        finite = lambda value: None if math.isinf(value) else value
        data = {"version": 1, "scope": self.scope,
                "entries": [[p, r, pref, finite(e)]
                            for (p, r), (pref, e) in self.state.entries.items()],
                "installed": [[p, pref, [[r, finite(e)] for r, e in hops]]
                              for (p, pref), hops in self.writer.installed.items()],
                "pending": [[p, pref, [[r, finite(e)] for r, e in hops]]
                            for (p, pref), hops in self.writer.pending.items()]}
        temporary = self.path + ".tmp"
        with open(temporary, "w") as stream:
            json.dump(data, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)


def allowed_prefix(prefix, args):
    if prefix.prefixlen == 0:
        return args.allow_default_route
    return (not prefix.network_address.is_multicast
            and not prefix.network_address.is_link_local
            and any(prefix.subnet_of(network) for network in args.prefix))


class ForwardingProbe:
    def __init__(self, args):
        self.args = args
        # Sending via the gateway's Ethernet address pins the outgoing hop
        # without modifying the kernel routes used by Home Assistant.
        self.socket = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self.pending = {}
        self.reachable = set()
        self.unreachable = {}

    def send(self, state, writer, icmp, index, now):
        neighbors = {}
        for line in writer.command(["neigh", "show", "dev", self.args.interface]).splitlines():
            fields = line.split()
            if "lladdr" in fields and "FAILED" not in fields:
                neighbors[str(ipaddress.IPv6Address(fields[0]))] = bytes.fromhex(
                    fields[fields.index("lladdr") + 1].replace(":", ""))
        self.pending.clear()
        self.reachable.clear()
        self.unreachable.clear()
        candidates = [(prefix, router, target)
                      for (prefix, router), (_, expiry) in state.entries.items()
                      if expiry > now for target in state.probe_targets.get(prefix, ())]
        sources = {}
        for target in {target for _, _, target in candidates}:
            try:
                fields = writer.command(["route", "get", str(target), "oif", self.args.interface]).split()
                source = ipaddress.IPv6Address(fields[fields.index("src") + 1])
            except (RuntimeError, ValueError, IndexError, subprocess.TimeoutExpired) as error:
                LOG.debug("Probe source for %s: %s", target, error)
                continue
            if not source.is_link_local and not source.is_unspecified:
                sources[target] = source
        resolving = set()
        for prefix, router, target in candidates:
            source = sources.get(target)
            if source is None:
                continue
            try:
                mac = neighbors.get(router)
                if not mac or len(mac) != 6:
                    # Let normal NUD learn this gateway for the next cycle.
                    if router not in resolving:
                        resolving.add(router)
                        icmp.sendto(b"\x80" + bytes(7), (router, 0, 0, index))
                    continue
                token = os.urandom(4)
                while token in self.pending:
                    token = os.urandom(4)
                payload = b"\x80\0\0\0" + token + b"thread-rio"
                pseudo = source.packed + target.packed + struct.pack("!I3xB", len(payload), 58)
                payload = payload[:2] + struct.pack("!H", checksum(pseudo + payload)) + payload[4:]
                packet = struct.pack("!IHBB", 6 << 28, len(payload), 58, 64)
                packet += source.packed + target.packed + payload
                self.socket.sendto(packet, (self.args.interface, 0x86dd, 0, 0, mac))
                self.pending[token] = (source, target, prefix, router)
            except OSError as error:
                LOG.debug("Forwarding probe to %s via %s: %s", target, router, error)

    def receive(self, packet, source, destination, state, now):
        if packet[:2] == b"\x81\0" and packet[8:] == b"thread-rio":
            token = packet[4:8]
            probe = self.pending.get(token)
            if probe and str(probe[0]) == destination and str(probe[1]) == source:
                del self.pending[token]
                _, target, prefix, router = probe
                state.restore_nexthop(prefix, router, target)
                self.reachable.add((prefix, target))
                # A response from another BR proves the target itself is up.
                for failed_router in self.unreachable.pop((prefix, target), ()):
                    state.reject_nexthop(prefix, failed_router, target, now)
            return True
        if len(packet) < 56 or packet[14] != 58 or packet[48:50] != b"\x80\0":
            return False
        token = packet[52:56]
        probe = self.pending.get(token)
        if not probe or packet[16:48] != probe[0].packed + probe[1].packed:
            return False
        if no_route_destination(packet, source, destination) != probe[1]:
            return False
        del self.pending[token]
        _, target, prefix, router = probe
        key = (prefix, target)
        if key in self.reachable:
            state.reject_nexthop(prefix, router, target, now)
        else:
            self.unreachable.setdefault(key, set()).add(router)
        return True


def open_socket(interface):
    index = socket.if_nametoindex(interface)
    sock = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVPKTINFO, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVHOPLIMIT, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, index)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, 255)
    # Linux ICMP6_FILTER: receive RA and Destination Unreachable.
    mask = [0xffffffff] * 8
    for kind in (1, 129, 134):
        mask[kind >> 5] &= ~(1 << (kind & 31))
    sock.setsockopt(socket.IPPROTO_ICMPV6, 1, struct.pack("=8I", *mask))
    sock.settimeout(1)
    return sock, index


def run(args):
    if sys.platform != "linux":
        raise RuntimeError("Run this tool on Linux/DSM, not on the Windows build PC")
    import fcntl
    lock = None
    journal = None
    if args.apply:
        lock = open("/var/run/thread-rio-%s.lock" % args.interface, "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Another learner already runs on %s", args.interface)
            return
    sock, index = open_socket(args.interface)
    probe = None
    if args.apply:
        try:
            probe = ForwardingProbe(args)
        except OSError as error:
            LOG.warning("Cannot start active forwarding probes; using passive ICMPv6 only: %s", error)
    writer = RouteWriter(args)
    state = RouteState()
    stop = [False]
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.__setitem__(0, True))
    if args.apply:
        with open("/proc/sys/kernel/random/boot_id") as stream:
            boot_id = stream.read().strip()
        journal = Journal(args.state_file, args, state, writer, boot_id)
        journal.load()
        state.entries = {key: value for key, value in state.entries.items()
                         if allowed_prefix(ipaddress.IPv6Network(key[0]), args)
                         and (not args.router or key[1] in args.router)}
        journal.save()
    LOG.info("%s on %s; prefixes=%s; default-route=%s; routers=%s",
             "APPLY" if args.apply else "OBSERVE (no route changes)", args.interface,
             ",".join(map(str, args.prefix)), args.allow_default_route,
             ",".join(args.router) or "any link-local RA source")
    next_rs = time.monotonic() + random.uniform(0, 1)
    solicitations = 0
    next_neighbors = 0
    next_probe = 0
    failed = set()
    try:
        while not stop[0]:
            now = time.monotonic()
            if now >= next_rs:
                try:
                    sock.sendto(struct.pack("!BBHI", 133, 0, 0, 0), ("ff02::2", 0, 0, index))
                except OSError as error:
                    LOG.warning("Router solicitation: %s", error)
                solicitations += 1
                next_rs = now + 4 if solicitations < 3 else (
                    now + args.rs_interval if args.rs_interval else math.inf)
            try:
                packet, ancillary, flags, peer = sock.recvmsg(65535, 256)
                source = str(ipaddress.IPv6Address(peer[0].split("%")[0]))
                destination, received_index, hop_limit = None, None, None
                for level, kind, data in ancillary:
                    if level == socket.IPPROTO_IPV6 and kind == socket.IPV6_PKTINFO:
                        destination = socket.inet_ntop(socket.AF_INET6, data[:16])
                        received_index = struct.unpack("=I", data[16:20])[0]
                    elif level == socket.IPPROTO_IPV6 and kind == socket.IPV6_HOPLIMIT:
                        hop_limit = struct.unpack("=i", data)[0]
                if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or received_index != index or destination is None:
                    raise ValueError("truncated ICMPv6 or missing/wrong receive interface")
                if probe and probe.receive(packet, source, destination, state, time.monotonic()):
                    pass
                elif packet[:1] == b"\x01":
                    if args.apply:
                        target = no_route_destination(packet, source, destination)
                        if target is not None:
                            if state.forwarding_error(source, target, time.monotonic(), writer.installed):
                                next_probe = 0
                elif packet[:1] == b"\x86":
                    if args.router and source not in args.router:
                        raise ValueError("RA source not allowed")
                    routes = parse_ra(packet, source, destination, hop_limit, args.allow_default_route)
                    allowed = []
                    for prefix, preference, lifetime in routes:
                        if allowed_prefix(prefix, args):
                            allowed.append((prefix, preference, lifetime))
                            LOG.info("RA %s: %s preference=%s lifetime=%s", source, prefix, preference, lifetime)
                    state.update(source, allowed, time.monotonic())
                    if args.apply and source in failed and any(
                            router == source for _, router in state.entries):
                        try:
                            if writer.relearn_failed_neighbor(source, packet):
                                failed.discard(source)
                        except (RuntimeError, subprocess.TimeoutExpired) as error:
                            LOG.warning("Relearn neighbor %s: %s", source, error)
                    if journal:
                        journal.save()
            except socket.timeout:
                pass
            except ValueError as error:
                LOG.debug("Ignored ICMPv6: %s", error)
            now = time.monotonic()
            if now >= next_neighbors:
                try:
                    failed = writer.failed_neighbors()
                except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                    LOG.warning("Read neighbors (keeping last state): %s", error)
                next_neighbors = now + args.neighbor_interval
            desired = state.selected(now, failed)
            if args.apply:
                try:
                    writer.sync(desired, now, journal.save)
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    LOG.error("Read routes: %s", error)
            if probe and state.probe_targets and now >= next_probe:
                try:
                    probe.send(state, writer, sock, index, now)
                except (RuntimeError, OSError, ValueError, IndexError, subprocess.TimeoutExpired) as error:
                    LOG.warning("Forwarding probes: %s", error)
                next_probe = time.monotonic() + args.probe_interval
    finally:
        if probe:
            probe.socket.close()
        sock.close()
        if args.apply:
            try:
                writer.sync({}, time.monotonic(), journal.save)
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                LOG.error("Cleanup (journal retained for restart): %s", error)
            state.entries.clear()
            journal.save()
        if lock:
            lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="ovs_eth2")
    parser.add_argument("--router", action="append", default=[], help="Optional allowed link-local RA source; repeatable")
    parser.add_argument("--table", default="main", help="IPv6 route table (default: main)")
    parser.add_argument("--metric", type=int, default=1024,
                        help="Metric base; high/medium/low use base/base+1/base+2")
    parser.add_argument("--prefix", action="append", default=[],
                        help="Allowed containing prefix; repeatable (default: fc00::/7, any length)")
    parser.add_argument("--allow-default-route", action="store_true",
                        help="Also learn RA header defaults, overridden by valid ::/0 RIOs")
    parser.add_argument("--state-file", help="Ownership/recovery journal (default: /run/thread-rio-INTERFACE-TABLE.json)")
    parser.add_argument("--neighbor-interval", type=float, default=5,
                        help="Seconds between reads of kernel NUD state (default: 5)")
    parser.add_argument("--probe-interval", type=float, default=15,
                        help="Seconds between probe rounds for destinations learned from No Route (default: 15)")
    parser.add_argument("--rs-interval", type=float, default=0,
                        help="Optional recurring RS interval after initial 3 solicitations (default: disabled)")
    parser.add_argument("--ip", default=shutil.which("ip") or "/sbin/ip")
    parser.add_argument("--apply", action="store_true", help="Install routes; otherwise only observe")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.metric <= 0xffffffff - 2:
        parser.error("--metric must be between 1 and 4294967293")
    if not math.isfinite(args.neighbor_interval) or args.neighbor_interval <= 0:
        parser.error("--neighbor-interval must be positive and finite")
    if not math.isfinite(args.probe_interval) or args.probe_interval <= 0:
        parser.error("--probe-interval must be positive and finite")
    if not math.isfinite(args.rs_interval) or (args.rs_interval != 0 and args.rs_interval < 4):
        parser.error("--rs-interval must be 0 or at least 4 seconds")
    try:
        args.prefix = [ipaddress.IPv6Network(p) for p in args.prefix or ["fc00::/7"]]
    except ValueError as error:
        parser.error(str(error))
    if not args.state_file:
        if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
               for c in args.interface + args.table):
            parser.error("use --state-file for interface/table names containing special characters")
        args.state_file = "/run/thread-rio-%s-%s.json" % (args.interface, args.table)
    for i, router in enumerate(args.router):
        try:
            address = ipaddress.IPv6Address(router)
        except ValueError as error:
            parser.error(str(error))
        if not address.is_link_local or "%" in router:
            parser.error("--router must be a link-local IPv6 address without a zone suffix")
        args.router[i] = str(address)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(args)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
        LOG.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

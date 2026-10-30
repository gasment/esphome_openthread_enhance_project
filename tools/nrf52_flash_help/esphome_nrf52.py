#!/usr/bin/env python3
"""ESPHome CLI wrapper with nRF52840 SWD/RTT and compressed Thread OTA.

Examples:
  python esphome_nrf52.py run config.yaml
  python esphome_nrf52.py logs config.yaml
  python esphome_nrf52.py run config.yaml --device pyocd
  python esphome_nrf52.py logs config.yaml --device pyocd
  python esphome_nrf52.py upload config.yaml --device pyocd
  python esphome_nrf52.py run config.yaml --device fd00::1234

Uses ESPHome's original CLI in this process. No installed source is changed.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager, suppress
from datetime import datetime

_LOGGER = logging.getLogger("esphome_nrf52")


class _QuietSMPTimeouts(logging.Filter):
    def filter(self, record):
        return not record.getMessage().startswith("Timeout (")

_SECONDARY_SLOT_SIZE = 0x58000
_SYSBUILD_CONF = """SB_CONFIG_BOOTLOADER_MCUBOOT=y
SB_CONFIG_BOOT_SIGNATURE_TYPE_ECDSA_P256=y
SB_CONFIG_MCUBOOT_MODE_OVERWRITE_ONLY=y
SB_CONFIG_MCUBOOT_UPDATEABLE_IMAGES=1
SB_CONFIG_MCUBOOT_COMPRESSED_IMAGE_SUPPORT=y
"""
_MCUBOOT_CONF = "CONFIG_BOOT_MAX_IMG_SECTORS=160\n"
_PARTITION_SOURCE = (
    Path(__file__).resolve().parent.parent
    / "external_components/nrf52/thread_ota_nrf52/pm_static.yml"
)


def _write_if_changed(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


@contextmanager
def thread_ota_build_hook():
    """Supply compressed-image sysbuild and partition inputs after ESPHome writes them."""
    from esphome.components import nrf52
    from esphome.core import CORE
    from esphome.writer import clean_cmake_cache

    original = nrf52.zephyr_copy_files

    def copy_files_with_thread_ota():
        original()
        if "thread_ota_nrf52" not in CORE.loaded_integrations:
            return
        build = CORE.relative_build_path("zephyr")
        changed = _write_if_changed(build / "sysbuild.conf", _SYSBUILD_CONF)
        changed |= _write_if_changed(
            build / "pm_static.yml", _PARTITION_SOURCE.read_text(encoding="utf-8")
        )
        changed |= _write_if_changed(build / "sysbuild/mcuboot.conf", _MCUBOOT_CONF)
        if changed:
            clean_cmake_cache()

    nrf52.zephyr_copy_files = copy_files_with_thread_ota
    try:
        yield
    finally:
        nrf52.zephyr_copy_files = original


def _thread_ipv6(device: str):
    import ipaddress

    try:
        address = ipaddress.IPv6Address(device)
    except ipaddress.AddressValueError:
        return None
    if (address.is_multicast or address.is_link_local or address.is_unspecified
            or address.is_loopback or address.ipv4_mapped is not None):
        from esphome.core import EsphomeError

        raise EsphomeError("Thread upload requires a routable unicast IPv6 address")
    return address


def _thread_hostname(device: str) -> bool:
    return device.rstrip(".").lower().endswith(".local")


def resolve_thread_ipv6(host: str) -> list[str]:
    import ipaddress

    from esphome.core import EsphomeError
    from esphome.zeroconf import EsphomeZeroconf
    from zeroconf import IPVersion

    _LOGGER.info("Resolving %s through mDNS (IPv6)", host)
    try:
        resolver = EsphomeZeroconf(ip_version=IPVersion.All)
        try:
            addresses = resolver.resolve_host(host, timeout=10) or []
        finally:
            resolver.close()
    except OSError as err:
        raise EsphomeError(f"mDNS lookup failed for {host}: {err}") from err
    candidates = []
    for value in addresses:
        try:
            address = ipaddress.IPv6Address(value)
        except ipaddress.AddressValueError:
            continue
        if (address.is_multicast or address.is_link_local or address.is_unspecified
                or address.is_loopback or address.ipv4_mapped is not None):
            continue
        if str(address) not in candidates:
            candidates.append(str(address))
    if not candidates:
        raise EsphomeError(
            f"No routable IPv6 address found for {host}; keep the node awake and check "
            "BR mDNS/SRP advertisement, or specify --device <IPv6>"
        )
    return candidates


async def select_thread_ipv6(addresses: list[str]) -> str:
    """Disambiguate multiple AAAA records using a read-only MCUmgr handshake."""
    from esphome.core import EsphomeError
    from smpclient import SMPClient
    from smpclient.exceptions import SMPClientException
    from smpclient.transport.udp import SMPUDPTransport

    if len(addresses) == 1:
        return addresses[0]
    _LOGGER.info("mDNS returned %d IPv6 addresses; probing MCUmgr", len(addresses))
    for address in addresses:
        for _ in range(2):
            probe = SMPClient(SMPUDPTransport(mtu=256), address, timeout_s=3)
            try:
                await probe.connect()
                return address
            except (OSError, TimeoutError, SMPClientException):
                pass
            finally:
                with suppress(OSError, AttributeError):
                    await probe.disconnect()
    raise EsphomeError("None of the mDNS IPv6 addresses responded to MCUmgr UDP port 1337")


def _built_partitions() -> dict[str, int]:
    from esphome.core import CORE, EsphomeError

    path = CORE.relative_pioenvs_path(CORE.name, "pm.config")
    try:
        return {
            name: int(value, 0)
            for name, value in (
                line.split("=", 1)
                for line in path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            if name in {
                "PM_MCUBOOT_ADDRESS", "PM_MCUBOOT_SIZE",
                "PM_MCUBOOT_PRIMARY_ADDRESS", "PM_MCUBOOT_PRIMARY_SIZE",
                "PM_MCUBOOT_SECONDARY_ADDRESS", "PM_MCUBOOT_SECONDARY_SIZE",
                "PM_SETTINGS_STORAGE_SIZE",
            }
        }
    except (OSError, ValueError) as err:
        raise EsphomeError(f"Cannot read built flash layout from {path}: {err}") from err


def report_flash_usage(config) -> None:
    """Report signed flash extents and OTA package size before either upload path."""
    if "thread_ota_nrf52" not in config:
        return

    from intelhex import IntelHex
    from esphome.core import CORE, EsphomeError

    partitions = _built_partitions()
    build = CORE.relative_pioenvs_path(CORE.name)

    def signed_extent(relative_path: str, start: int) -> int:
        path = build / relative_path
        if not path.is_file():
            raise EsphomeError(f"Signed image not found: {path}")
        image = IntelHex(str(path))
        if image.minaddr() != start:
            raise EsphomeError(
                f"{path.name} starts at 0x{image.minaddr():X}, expected 0x{start:X}"
            )
        return image.maxaddr() + 1 - start

    try:
        boot_size = partitions["PM_MCUBOOT_SIZE"]
        primary_size = partitions["PM_MCUBOOT_PRIMARY_SIZE"]
        secondary_size = partitions["PM_MCUBOOT_SECONDARY_SIZE"]
        storage_size = partitions["PM_SETTINGS_STORAGE_SIZE"]
        boot_used = signed_extent(
            "mcuboot/zephyr/zephyr.hex", partitions["PM_MCUBOOT_ADDRESS"]
        )
        primary_used = signed_extent(
            "zephyr/zephyr/zephyr.signed.hex",
            partitions["PM_MCUBOOT_PRIMARY_ADDRESS"],
        )
        update_path = build / "zephyr/app_update.bin"
        secondary_used = update_path.stat().st_size
    except (KeyError, OSError) as err:
        raise EsphomeError(f"Cannot inspect built flash images: {err}") from err

    _LOGGER.info("Flash partition usage (signed images):")
    for label, used, capacity in (
        ("MCUboot", boot_used, boot_size),
        ("Primary", primary_used, primary_size),
        ("Secondary OTA package", secondary_used, secondary_size),
    ):
        if used > capacity:
            raise EsphomeError(f"{label} image exceeds its partition: {used} > {capacity} B")
        _LOGGER.info(
            "  %-21s %7d / %7d B  %5.1f%%  free %d B",
            label, used, capacity, used * 100 / capacity, capacity - used,
        )
    _LOGGER.info("  Settings storage      %7d B reserved; runtime use unknown", storage_size)


async def _upload_thread_image(address: str, firmware: Path) -> None:
    import asyncio

    from smpclient import SMPClient
    from smpclient.exceptions import SMPBadSequence
    from smpclient.generics import success
    from smpclient.mcuboot import IMAGE_TLV, ImageInfo
    from smpclient.requests.image_management import ImageStatesRead, ImageStatesWrite, ImageUploadWrite
    from smpclient.requests.os_management import MCUMgrParametersRead, ResetWrite
    from smpclient.transport.udp import SMPUDPTransport

    class RetryingSMPClient(SMPClient):
        async def request(self, request, timeout_s=None):
            if not isinstance(request, (ImageUploadWrite, ImageStatesRead, ImageStatesWrite, MCUMgrParametersRead)):
                return await super().request(request, timeout_s=timeout_s)
            operation = (
                f"upload at offset {request.off}"
                if isinstance(request, ImageUploadWrite) else type(request).__name__
            )
            for attempt in range(1, 6):
                try:
                    return await super().request(request, timeout_s=timeout_s)
                except (TimeoutError, SMPBadSequence) as err:
                    if attempt == 5:
                        raise RuntimeError(
                            f"MCUmgr {operation} failed after 5 attempts"
                        ) from None
                    _LOGGER.warning(
                        "MCUmgr UDP response lost (%s); retrying %d/5: %s",
                        operation, attempt, type(err).__name__,
                    )

    image = firmware.read_bytes()
    if len(image) > _SECONDARY_SLOT_SIZE:
        raise ValueError(
            f"Update image is {len(image)} bytes; secondary slot is {_SECONDARY_SLOT_SIZE} bytes"
        )
    info = ImageInfo.load_file(str(firmware))
    if not int(info.header.flags) & 0x400:
        raise ValueError("Update image is not MCUboot LZMA2 compressed")
    expected_hash = bytes(info.get_tlv(IMAGE_TLV.SHA256).value)
    active_hash = bytes(info.get_tlv(IMAGE_TLV.DECOMP_SHA).value)

    async def image_states(client, timeout_s=15):
        response = await client.request(ImageStatesRead(), timeout_s=timeout_s)
        if not success(response):
            raise RuntimeError(f"MCUmgr image-state query failed: {response}")
        return response.images

    def secondary_image(states):
        return next(
            (state for state in states if state.slot == 1 and (state.image or 0) == 0), None
        )

    def matches(state, image_hash):
        return state is not None and state.hash is not None and bytes(state.hash) == image_hash

    def active_image_matches(states):
        return any(
            state.active and (state.image or 0) == 0 and matches(state, active_hash)
            for state in states
        )

    smp_logger = logging.getLogger("smpclient")
    quiet_timeouts = _QuietSMPTimeouts()
    smp_logger.addFilter(quiet_timeouts)
    try:
        async with RetryingSMPClient(SMPUDPTransport(mtu=256), address, timeout_s=15) as client:
            before = await image_states(client)
            if any(state.pending and not matches(state, expected_hash) for state in before):
                raise RuntimeError("A different image is already pending; refusing to replace it")
            if active_image_matches(before) and not any(state.pending for state in before):
                _LOGGER.info("Requested firmware is already active; no update required")
                return
            if matches(secondary_image(before), expected_hash):
                _LOGGER.info("Matching image already uploaded; reusing secondary slot")
            else:
                _LOGGER.info("Uploading %s bytes to the MCUboot secondary slot", len(image))
                last_percent = -1
                async for offset in client.upload(
                    image, slot=0, upgrade=False, first_timeout_s=60,
                    subsequent_timeout_s=15,
                ):
                    percent = offset * 100 // len(image)
                    if percent != last_percent:
                        print(f"\rThread upload {percent:3d}% ({offset}/{len(image)})", end="", flush=True)
                        last_percent = percent
                print()
            after = await image_states(client)
            secondary = secondary_image(after)
            if secondary is None or secondary.hash is None:
                raise RuntimeError("Upload finished but secondary image was not reported")
            if bytes(secondary.hash) != expected_hash:
                raise RuntimeError("Secondary image hash differs from uploaded image")
            if secondary.active:
                raise RuntimeError("Secondary slot unexpectedly reports itself as active")
            if secondary.pending and not secondary.permanent:
                raise RuntimeError("Image is pending in test mode; permanent activation cannot proceed")
            _LOGGER.info("Secondary image verified; marking for permanent upgrade")
            response = await client.request(
                ImageStatesWrite(hash=expected_hash, confirm=True), timeout_s=15
            )
            if not success(response):
                raise RuntimeError(f"Cannot mark image for permanent upgrade: {response}")
            pending = secondary_image(await image_states(client))
            if not matches(pending, expected_hash) or not pending.pending or not pending.permanent:
                raise RuntimeError("Target did not report the verified image as permanent and pending")
            _LOGGER.info("Permanent upgrade accepted; requesting reboot")
            # A lost reset response is ambiguous: never repeatedly reset the newly booted app.
            try:
                response = await client.request(ResetWrite(), timeout_s=5)
            except (TimeoutError, SMPBadSequence):
                _LOGGER.warning("Reset response not received; checking whether the new image boots")
            else:
                if not success(response):
                    raise RuntimeError(f"Target rejected reboot: {response}")

        _LOGGER.info("Waiting up to 120s for the new active image at %s", address)
        deadline = asyncio.get_running_loop().time() + 120
        await asyncio.sleep(2)
        while asyncio.get_running_loop().time() < deadline:
            probe = SMPClient(SMPUDPTransport(mtu=256), address, timeout_s=3)
            try:
                await probe.connect()
                states = await image_states(probe, timeout_s=3)
                if active_image_matches(states):
                    _LOGGER.info("Firmware switch verified: new decompressed image is active")
                    return
            except (OSError, TimeoutError, SMPBadSequence):
                pass
            finally:
                with suppress(OSError, AttributeError):
                    await probe.disconnect()
            await asyncio.sleep(2)
        raise RuntimeError(
            "Permanent upgrade was requested, but the new active image could not be verified "
            "within 120s; check node RTT and keep it awake with continuous Thread reception"
        )
    finally:
        smp_logger.removeFilter(quiet_timeouts)


def run_thread_upload(config, address: str) -> bool:
    import asyncio

    from esphome.core import CORE, EsphomeError

    if "thread_ota_nrf52" not in config:
        raise EsphomeError("IPv6 upload requires thread_ota_nrf52 in the YAML configuration")
    expected_partitions = {
        "PM_MCUBOOT_PRIMARY_ADDRESS": 0x10000,
        "PM_MCUBOOT_PRIMARY_SIZE": 0x90000,
        "PM_MCUBOOT_SECONDARY_ADDRESS": 0xA0000,
        "PM_MCUBOOT_SECONDARY_SIZE": _SECONDARY_SLOT_SIZE,
    }
    built_partitions = _built_partitions()
    layout_matches = all(
        built_partitions.get(name) == value
        for name, value in expected_partitions.items()
    )
    if not layout_matches:
        raise EsphomeError("Built image uses an old or unknown flash layout; rebuild before IPv6 upload")
    firmware = CORE.relative_pioenvs_path(CORE.name, "zephyr", "app_update.bin").resolve()
    if not firmware.is_file():
        raise EsphomeError(f"Compressed update image not found: {firmware}. Run a build first.")
    if _thread_hostname(address):
        host = address.rstrip(".")
        address = asyncio.run(select_thread_ipv6(resolve_thread_ipv6(host)))
        _LOGGER.info("Thread OTA target: %s -> %s", host, address)
    try:
        asyncio.run(_upload_thread_image(address, firmware))
    except (OSError, ValueError, RuntimeError, TimeoutError) as err:
        raise EsphomeError(f"Thread OTA failed: {err}") from err
    return True


def _thread_log_request_type():
    """Custom, read-only MCUmgr group shared with thread_logs.cpp."""
    from typing import ClassVar, Literal

    from pydantic import Field
    from smp import error, header, message

    class LogResponse(message.ReadResponse):
        _GROUP_ID = 0xA652
        _COMMAND_ID = 0
        v: Literal[1]
        boot: int = Field(ge=1, le=0xFFFFFFFF)
        next: int = Field(ge=0, le=0xFFFFFFFFFFFFFFFF)
        lost: int = Field(ge=0, le=0xFFFFFFFFFFFFFFFF)
        data: bytes = Field(max_length=160)

    class LogErrorV1(error.ErrorV1):
        _GROUP_ID = 0xA652
        _COMMAND_ID = 0
        _OP = header.OP.READ_RSP

    class LogErrorV2(error.ErrorV2[error.MGMT_ERR]):
        _GROUP_ID = 0xA652
        _COMMAND_ID = 0
        _OP = header.OP.READ_RSP

    class LogRequest(message.ReadRequest):
        _GROUP_ID = 0xA652
        _COMMAND_ID = 0
        _Response: ClassVar[type] = LogResponse
        _ErrorV1: ClassVar[type] = LogErrorV1
        _ErrorV2: ClassVar[type] = LogErrorV2
        boot: int = Field(default=0, ge=0, le=0xFFFFFFFF)
        cursor: int = Field(default=0xFFFFFFFFFFFFFFFF, ge=0, le=0xFFFFFFFFFFFFFFFF)

    # Resolve function-local annotations when postponed annotations are enabled.
    LogResponse.model_rebuild(_types_namespace={"Literal": Literal})
    LogRequest.model_rebuild(_types_namespace={"ClassVar": ClassVar})
    return LogRequest


async def _stream_thread_logs(address: str, hostname: str | None = None) -> None:
    import asyncio
    import codecs

    from esphome.core import EsphomeError
    from smpclient import SMPClient
    from smpclient.exceptions import SMPBadSequence
    from smpclient.generics import success
    from smpclient.transport.udp import SMPUDPTransport

    request_type = _thread_log_request_type()
    boot = 0
    cursor = 0xFFFFFFFFFFFFFFFF
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    partial_line = ""
    reconnect = False
    quiet_timeouts = _QuietSMPTimeouts()
    smp_logger = logging.getLogger("smpclient")
    smp_logger.addFilter(quiet_timeouts)

    def print_line(line):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{timestamp}] {line.rstrip(chr(13))}", flush=True)

    try:
        while True:
            if reconnect and hostname:
                try:
                    candidates = await asyncio.to_thread(resolve_thread_ipv6, hostname)
                    address = await select_thread_ipv6(candidates)
                except EsphomeError as err:
                    _LOGGER.warning("Thread log discovery failed: %s; retrying", err)
                    await asyncio.sleep(2)
                    continue
            client = SMPClient(SMPUDPTransport(mtu=256), address, timeout_s=3)
            try:
                await client.connect()
                _LOGGER.info("Thread logs connected; replaying available buffer, then following live output")
                while True:
                    request = request_type(boot=boot, cursor=cursor)
                    for attempt in range(3):
                        try:
                            response = await client.request(request, timeout_s=3)
                            break
                        except (TimeoutError, SMPBadSequence):
                            if attempt == 2:
                                raise
                    if not success(response):
                        raise RuntimeError(
                            f"Thread log command rejected: {response}. "
                            "The device needs firmware with the updated thread_ota_nrf52 log backend."
                        )
                    restarted = boot != 0 and response.boot != boot
                    if restarted or response.lost:
                        decoder.reset()
                        partial_line = ""
                        if restarted:
                            _LOGGER.warning("Node restarted; replaying its new log buffer")
                        else:
                            _LOGGER.warning("Thread log buffer overran: %d bytes lost", response.lost)
                    if boot == response.boot and response.next != cursor + response.lost + len(response.data):
                        raise RuntimeError("Thread log response cursor is inconsistent")
                    boot, cursor = response.boot, response.next
                    partial_line += decoder.decode(response.data)
                    while "\n" in partial_line:
                        line, partial_line = partial_line.split("\n", 1)
                        print_line(line)
                    if len(partial_line) > 4096:
                        print_line(partial_line)
                        partial_line = ""
                    await asyncio.sleep(0.02 if response.data else 0.5)
            except (OSError, TimeoutError, SMPBadSequence) as err:
                _LOGGER.warning("Thread log connection interrupted (%s); retrying", type(err).__name__)
            finally:
                with suppress(OSError, AttributeError):
                    await client.disconnect()
            reconnect = True
            await asyncio.sleep(2)
    finally:
        smp_logger.removeFilter(quiet_timeouts)
        if partial_line:
            print_line(partial_line)


def run_thread_logs(config, device: str) -> bool:
    import asyncio

    from esphome.core import EsphomeError
    from smpclient.exceptions import SMPClientException

    if "thread_ota_nrf52" not in config:
        raise EsphomeError("Thread logs require thread_ota_nrf52 in the device firmware")
    hostname = device.rstrip(".") if _thread_hostname(device) else None
    address = device
    if hostname:
        address = asyncio.run(select_thread_ipv6(resolve_thread_ipv6(hostname)))
        _LOGGER.info("Thread log target: %s -> %s", hostname, address)
    _LOGGER.info("Starting Thread wireless logs. Press Ctrl+C to stop.")
    try:
        asyncio.run(_stream_thread_logs(address, hostname))
    except KeyboardInterrupt:
        _LOGGER.info("Thread logging stopped.")
    except (OSError, RuntimeError, ValueError, SMPClientException) as err:
        raise EsphomeError(f"Thread logging failed: {err}") from err
    return True


@contextmanager
def preserve_terminal():
    """pyOCD disables ECHO/ICANON; restore even after forced child termination."""
    saved = None
    if os.name == "posix":
        import termios
        try:
            fd = sys.stdin.fileno()
            if os.isatty(fd):
                saved = (fd, termios.tcgetattr(fd))
        except (AttributeError, ValueError, OSError):
            pass
    try:
        yield
    finally:
        if saved is not None:
            try:
                termios.tcsetattr(saved[0], termios.TCSANOW, saved[1])
            except OSError as err:
                _LOGGER.warning("Cannot restore terminal settings: %s", err)


@contextmanager
def cleanup_signals():
    """A second Ctrl+C must not interrupt probe/terminal cleanup."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def stop_rtt_process(process):
    if process.poll() is not None:
        return
    # A separate POSIX session prevents terminal Ctrl+C reaching the child
    # twice (once from the terminal and once from this parent).
    try:
        if os.name == "posix":
            process.send_signal(signal.SIGINT)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def stream_rtt(command: list[str], env: dict[str, str]) -> int:
    """Prefix complete output lines with host receive time, preserving ANSI text."""
    child_env = dict(env)
    # pyOCD is Python: avoid stdout block buffering when redirected to our pipe.
    child_env["PYTHONUNBUFFERED"] = "1"
    with preserve_terminal():
        process = None
        try:
            process = subprocess.Popen(
                command, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                start_new_session=(os.name == "posix"),
            )
            assert process.stdout is not None
            for line in process.stdout:
                timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"[{timestamp}] {line.rstrip(chr(10) + chr(13))}", flush=True)
            return process.wait()
        finally:
            with cleanup_signals():
                if process is not None:
                    stop_rtt_process(process)
                    if process.stdout is not None:
                        process.stdout.close()


def normalize_devices(argv: list[str]) -> list[str]:
    """Normalize only --device values; leave filenames and all other args intact."""
    result = list(argv)
    for i, arg in enumerate(result):
        if arg == "--device" and i + 1 < len(result):
            if result[i + 1].upper() in ("PYOCD", "RTT"):
                result[i + 1] = "PYOCD"
        elif arg.startswith("--device="):
            if arg.split("=", 1)[1].upper() in ("PYOCD", "RTT"):
                result[i] = "--device=PYOCD"
    return result


def get_rtt_env(nrf52) -> dict[str, str]:
    """Logs never compile: no ccache resolution/installation is needed."""
    if "ccache" in inspect.signature(nrf52.get_build_env).parameters:
        return nrf52.get_build_env(ccache=None)
    # ESPHome 2026.9.x has the no-argument interface.
    return nrf52.get_build_env()


def run_rtt(config, args) -> bool:
    """Handle the native platform show_logs contract, using the SDK environment."""
    from esphome.components import nrf52
    from esphome.core import CORE, EsphomeError

    # Native logs can use the validated config cache without running the schema.
    if "zephyr" not in CORE.data:
        platform_config = config.get(CORE.target_platform)
        if not platform_config:
            raise EsphomeError("Missing nRF52 platform configuration for RTT logs")
        nrf52.set_core_data(platform_config)
        nrf52.set_framework(platform_config)

    board = str(config.get("nrf52", {}).get("board", ""))
    if "nrf52840" not in board.lower():
        raise EsphomeError("This project's RTT profile currently supports nRF52840 boards")

    paths = nrf52.get_build_paths()
    env = get_rtt_env(nrf52)
    sdk_python = Path(paths["python_executable"])
    sdk_pyocd = sdk_python.parent / ("pyocd.exe" if sys.platform == "win32" else "pyocd")
    if sdk_python.is_file() and sdk_pyocd.is_file():
        executable = [str(sdk_python), "-m", "pyocd"]
    elif binary := shutil.which("pyocd", path=env.get("PATH")):
        executable = [binary]
    elif importlib.util.find_spec("pyocd") is not None:
        executable = [sys.executable, "-m", "pyocd"]
    else:
        raise EsphomeError(
            f"Cannot find pyOCD in {sdk_python.parent} or PATH. "
            "Set ESPHOME_SDK_NRF_PREFIX to the existing SDK directory."
        )

    command = [
        *executable, "rtt", "-t", "nrf52840", "-M", "attach",
        "-a", "0x20000000", "-s", "0x40000",
    ]
    _LOGGER.info("Starting SWD RTT channel 0. Press Ctrl+C to stop.")
    try:
        # Preserve the explicit native logs --reset option. Never reset by default.
        if getattr(args, "reset", False):
            reset = subprocess.run([*executable, "reset", "-t", "nrf52840"], env=env, check=False)
            if reset.returncode:
                raise EsphomeError(f"pyOCD reset exited with code {reset.returncode}")
        returncode = stream_rtt(command, env)
    except KeyboardInterrupt:
        _LOGGER.info("RTT logging stopped.")
        return True
    except OSError as err:
        raise EsphomeError(f"Cannot start pyOCD: {err}") from err
    if returncode not in (0, 130, -2):
        raise EsphomeError(
            f"pyOCD RTT exited with code {returncode}; check probe permissions "
            "and that RTT is enabled in the flashed firmware"
        )
    return True


@contextmanager
def rtt_hooks(cli):
    """Route native CLI upload/log commands through the selected SWD or Thread transport."""
    original_hook = cli.platform_hooks.get_platform_hook
    original_command = cli.ESPHOME_COMMAND
    original_choose_device = cli.choose_upload_log_host

    def choose_device(default, check_default, purpose):
        from esphome.core import CORE

        if (not default and CORE.target_platform == "nrf52"
                and purpose in (cli.Purpose.UPLOADING, cli.Purpose.LOGGING)):
            return [f"{CORE.name}.local"]
        return original_choose_device(default, check_default, purpose)

    def get_hook(platform, hook):
        original = original_hook(platform, hook)
        if platform != "nrf52":
            return original

        if hook == "upload_program":
            def upload_program(config, args, host):
                address = _thread_ipv6(host)
                if address is not None:
                    report_flash_usage(config)
                    return run_thread_upload(config, str(address))
                if _thread_hostname(host):
                    report_flash_usage(config)
                    return run_thread_upload(config, host)
                if host.upper() == "PYOCD":
                    report_flash_usage(config)
                return original(config, args, host) if original else False

            return upload_program

        if hook == "show_logs":
            def show_logs(config, args, devices):
                if devices and devices[0].upper() == "PYOCD":
                    return run_rtt(config, args)
                if (devices and "thread_ota_nrf52" in config
                        and (_thread_hostname(devices[0]) or _thread_ipv6(devices[0]) is not None)):
                    return run_thread_logs(config, devices[0])
                return original(config, args, devices) if original else False

            return show_logs

        return original

    cli.platform_hooks.get_platform_hook = get_hook
    cli.choose_upload_log_host = choose_device
    # Native multi-config commands spawn children. Keep those children in this
    # wrapper too, rather than silently losing RTT routing in `python -m esphome`.
    cli.ESPHOME_COMMAND = [sys.executable, str(Path(__file__).resolve())]
    try:
        yield
    finally:
        cli.platform_hooks.get_platform_hook = original_hook
        cli.ESPHOME_COMMAND = original_command
        cli.choose_upload_log_host = original_choose_device


def main(argv: list[str] | None = None) -> int:
    try:
        import esphome.__main__ as cli
    except ModuleNotFoundError as err:
        if err.name != "esphome":
            raise
        print("ESPHome is not installed in this Python environment. Activate the esphome venv.", file=sys.stderr)
        return 1

    original_argv = sys.argv
    arguments = list(sys.argv[1:] if argv is None else argv)
    sys.argv = [str(Path(__file__).resolve()), *normalize_devices(arguments)]
    try:
        with rtt_hooks(cli), thread_ota_build_hook():
            # Original parser, config/cache handling, commands and error handling.
            # run already calls show_logs after a successful upload; --no-logs and
            # upload failures retain their original behavior.
            return cli.main() or 0
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    sys.exit(main())

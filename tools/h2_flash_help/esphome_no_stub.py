"""Run ESPHome normally, adding --no-stub to ESP32-H2 serial uploads.

Usage (in the ESPHome Python environment):
    python esphome_no_stub.py run config.yaml
    python esphome_no_stub.py upload config.yaml --device COM5

Firmware selection, OTA, compilation and logs remain managed by ESPHome.
Only this process is patched; installed packages are not modified.
"""

from functools import wraps
from pathlib import Path
import sys


def serial_upload_args(cmd):
    """Adjust only esptool write commands explicitly targeting ESP32-H2."""
    args = list(cmd)
    if not args or Path(args[0]).stem not in ("esptool", "esptool.py"):
        return args
    if not any(command in args for command in ("write-flash", "write_flash")):
        return args
    if "--chip" not in args:
        return args
    chip_index = args.index("--chip") + 1
    if chip_index >= len(args) or args[chip_index].lower() != "esp32h2":
        return args
    if "--no-stub" not in args:
        args.insert(1, "--no-stub")
    return args


def main():
    import esphome.__main__ as cli

    # Cover both ESPHome's in-process default and ESPHOME_USE_SUBPROCESS.
    original_command = cli.run_external_command
    original_process = cli.run_external_process

    @wraps(original_command)
    def command(func, *cmd, **kwargs):
        return original_command(func, *serial_upload_args(cmd), **kwargs)

    @wraps(original_process)
    def process(*cmd, **kwargs):
        return original_process(*serial_upload_args(cmd), **kwargs)

    cli.run_external_command = command
    cli.run_external_process = process
    try:
        return cli.main()
    finally:
        cli.run_external_command = original_command
        cli.run_external_process = original_process


if __name__ == "__main__":
    sys.exit(main())

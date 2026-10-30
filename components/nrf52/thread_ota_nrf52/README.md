# Thread compressed MCUboot OTA

`thread_ota_nrf52` enables Zephyr MCUmgr over Thread UDP/IPv6 (port 1337) and
NCS 2.9.2's compressed MCUboot image support. It is currently restricted to
`adafruit_feather_nrf52840/nrf52840` and the project-local
`nrf52840-wireless-bt-mtd/esphome_nrf52.py` wrapper.

The static internal-flash layout keeps the board's existing storage area at
`0xF8000..0x100000`: 64 KiB MCUboot, 576 KiB primary, 352 KiB secondary,
32 KiB settings. The first installation and any partition-layout change require
SWD because the application's link address changes. Verify the generated partition report and image
sizes before flashing; this source has not been compiled in this change.

Use the existing project wrapper for both transports. `run` builds the signed
**compressed** `zephyr/app_update.bin` before upload; `upload` uses an existing
build:

```sh
python esphome_nrf52.py run config.yaml
python esphome_nrf52.py upload config.yaml
python esphome_nrf52.py run config.yaml --device <node-OMR-IPv6>
python esphome_nrf52.py upload config.yaml --device <node-OMR-IPv6>
python esphome_nrf52.py run config.yaml --device pyocd
```

For `run` and `upload`, omitting `--device` selects Thread OTA through
`<esphome.name>.local`. The wrapper queries mDNS directly and keeps routable
IPv6 AAAA records. If multiple IPv6 addresses are returned, a read-only MCUmgr
handshake selects a reachable endpoint. Failed discovery reports an error;
`--device <IPv6>` remains available for direct access. `run` compiles first;
`upload` uses the existing build. Explicit `--device pyocd` selects SWD.

The uploader checks the compressed MCUboot image, uploads to image
0's secondary slot, and verifies its hash before marking that exact image for
permanent upgrade (`ImageStatesWrite(hash=..., confirm=True)`). It then requests
one reset and waits up to 120 seconds for the active image hash to match the
compressed package's `DECOMP_SHA` metadata. MCUboot decompresses into the
primary slot; this overwrite-only mode has no rollback.

An individual upload or image-state request allows up to five attempts after
a lost UDP response. A lost reset response leads to boot verification rather
than repeated resets. Repeating `upload` with the same signed image resumes a
partial upload or reuses an already verified secondary image. If the requested
decompressed image is already active, no upload or reset is needed. A different
pending image is never overwritten.

Keep the MCU awake and Thread in continuous reception throughout upload,
boot verification, and wireless logging.

`run` follows the selected transport: `--device pyocd` flashes over SWD and
starts RTT; Thread OTA starts wireless logs after the new image is verified.
`--no-logs` exits after upload. `upload` alone does not start logs.

```sh
python esphome_nrf52.py logs config.yaml                 # mDNS + Thread logs
python esphome_nrf52.py logs config.yaml --device <IPv6> # direct Thread logs
python esphome_nrf52.py logs config.yaml --device pyocd  # RTT
```

Wireless logs use a read-only MCUmgr group (`0xA652`, command `0`, protocol
version `1`) on the same UDP port 1337. A Zephyr log backend captures the same
ESPHome printk bridge and native Zephyr output as RTT in a 4096-byte RAM ring.
Each read returns up to 160 bytes with a boot identifier, byte cursor, and
overwrite count. Reads are non-destructive, supporting retry and multiple
viewers. The host replays the available buffer, follows new lines, and retries
or reconnects without duplicating previously consumed bytes. On overflow it
reports lost bytes. With no viewer, no log traffic is sent. If `swd_rtt_nrf52`
is absent, this component installs its own ESPHome-to-Zephyr log bridge.

The firmware must include this log backend before wireless `logs` can work;
an OTA from an earlier upload-only firmware can install it. Buffer contents do
not survive a reboot. MCUboot runs before Thread and its logs cannot be
streamed by this application backend.

Immediately before either upload path, the wrapper prints MCUboot, signed
primary image, and compressed secondary package usage against the generated
Partition Manager sizes. Signed primary usage includes the MCUboot header and
signature, so it is slightly larger than the application's linker FLASH value.
Settings storage is shown as reserved space; its runtime contents are unknown
to the build.

The same wrapper adds the compressed-image sysbuild options and complete static
partition file after ESPHome copies Zephyr build inputs. ESPHome 2026.10's
generated `sysbuild.conf` currently contains only the MCUboot enable switch,
and its `Section` serializer cannot express Partition Manager `span` entries.
It also raises MCUboot's image-sector limit to 160; the 576 KiB primary slot
uses 144 flash sectors, exceeding MCUboot's default limit of 128.

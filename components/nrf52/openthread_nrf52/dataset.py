"""Dataset validation shared by this fork's schema and regression checks."""
import ipaddress

import esphome.config_validation as cv


def validate_tlv(value):
    text = cv.string_strict(value).lower()
    if not text or len(text) % 2 or any(c not in "0123456789abcdef" for c in text):
        raise cv.Invalid("tlv must be a nonempty, even-length hexadecimal string")
    raw = bytes.fromhex(text)
    if len(raw) > 254:
        raise cv.Invalid("tlv must fit 254 bytes")
    offset = 0
    while offset < len(raw):
        if offset + 2 > len(raw) or offset + 2 + raw[offset + 1] > len(raw):
            raise cv.Invalid("Truncated dataset TLV")
        offset += 2 + raw[offset + 1]
    return text


def dataset_tlv(config):
    """Encode in Python so FTD and MTD use the identical complete dataset.

    Integer YAML credentials are padded before encoding; IPv6 prefixes use
    network byte order. No credentials are placed in Kconfig or logs.
    """
    if "tlv" in config:
        return config["tlv"]
    fields = [
        (14, (1 << 16).to_bytes(8, "big")),  # Active timestamp: 1 second.
        (0, b"\0" + int(config.get("channel", 15)).to_bytes(2, "big")),
        (1, int(config.get("pan_id", 0x1234)).to_bytes(2, "big")),
        (2, bytes.fromhex(config.get("ext_pan_id", "dead00beef00cafe"))),
        (3, config.get("network_name", "OpenThread-ESP").encode("utf-8")),
        (4, bytes.fromhex(config.get("pskc", "00" * 16))),
        (5, bytes.fromhex(config["network_key"])),
        (7, ipaddress.IPv6Network(config.get("mesh_local_prefix", "fd00:db8::/64")).network_address.packed[:8]),
        (12, bytes.fromhex("02a0f7f8")),  # Security policy: 672 hours, Thread 1.3 flags.
        (53, bytes.fromhex("000407fff800")),  # Channel page 0, channels 11..26.
    ]
    return b"".join(bytes((kind, len(value))) + value for kind, value in fields).hex()


def fixed_hex(byte_length):
    def validate(value):
        if isinstance(value, bool):
            raise cv.Invalid("Expected a hexadecimal value, not a boolean")
        if isinstance(value, int):
            if not 0 <= value < 1 << (8 * byte_length):
                raise cv.Invalid(f"Expected a {byte_length}-byte unsigned value")
            return f"{value:0{byte_length * 2}x}"
        text = cv.string_strict(value).lower().removeprefix("0x")
        if len(text) != byte_length * 2 or any(c not in "0123456789abcdef" for c in text):
            raise cv.Invalid(f"Expected exactly {byte_length * 2} hexadecimal digits")
        return text

    return validate


def network_name(value):
    value = cv.string_strict(value)
    if not 1 <= len(value.encode("utf-8")) <= 16 or "\0" in value:
        raise cv.Invalid("Thread network_name must be 1..16 UTF-8 bytes without NUL")
    return value


def mesh_local_prefix(value):
    try:
        prefix = ipaddress.IPv6Network(cv.string_strict(value), strict=True)
    except ValueError as error:
        raise cv.Invalid(str(error)) from error
    if prefix.prefixlen != 64 or prefix.network_address.packed[0] != 0xfd:
        raise cv.Invalid("mesh_local_prefix must be an fd00::/8 ULA /64")
    return str(prefix)

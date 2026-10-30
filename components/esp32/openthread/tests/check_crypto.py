"""Validate final sdkconfig after pure in-memory codegen; never invoke a compiler."""

from pathlib import Path
import subprocess
import sys

from esphome.core import CORE
from esphome.config import load_config
from esphome.__main__ import generate_cpp_contents
from esphome.components import esp32


def check(filename):
    CORE.config_path = Path(__file__).with_name(filename)
    config = load_config({})
    if config.errors:
        raise RuntimeError("; ".join(str(error) for error in config.errors))
    CORE.config = config
    generate_cpp_contents(config)
    options = CORE.data[esp32.KEY_ESP32][esp32.KEY_SDKCONFIG_OPTIONS]
    expected_capacity = 12 if filename == "crypto_ip6_override.yaml" else 8
    capacity = options.get("CONFIG_LWIP_IPV6_NUM_ADDRESSES")
    assert int(getattr(capacity, "value", capacity)) == expected_capacity, capacity
    for name in (
        "CONFIG_MBEDTLS_ECP_C",
        "CONFIG_MBEDTLS_ECDH_C",
        "CONFIG_MBEDTLS_ECDSA_C",
        "CONFIG_MBEDTLS_ECP_DP_SECP256R1_ENABLED",
        "CONFIG_MBEDTLS_ECDSA_DETERMINISTIC",
        "CONFIG_MBEDTLS_CCM_C",
    ):
        assert options.get(name) is True, f"{name} was disabled: {options.get(name)!r}"
    # SRP/AES-CCM must work without bringing the TLS stack back into the build.
    assert options.get("CONFIG_MBEDTLS_TLS_DISABLED") is True
    print(f"{config['openthread']['device_type']}: config/codegen, crypto and IPv6 capacity={expected_capacity} OK")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        check(sys.argv[1])
    else:
        # Separate processes keep ESPHome's global validation/codegen state isolated.
        for filename in ("crypto.yaml", "crypto_mtd.yaml", "crypto_ip6_override.yaml"):
            subprocess.run([sys.executable, __file__, filename], check=True)

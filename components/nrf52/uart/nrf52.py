"""nRF52840 pinctrl and Zephyr driver options for peripheral UARTs."""

import esphome.codegen as cg
from esphome.components.zephyr import zephyr_add_overlay, zephyr_add_prj_conf
from esphome.const import CONF_NUMBER, CONF_RX_PIN, CONF_TX_PIN


def configure_uart(config, port):
    for key, value in {
        "SERIAL": True,
        "RING_BUFFER": True,
        "UART_USE_RUNTIME_CONFIGURE": True,
        "UART_INTERRUPT_DRIVEN": True,
        f"UART_{port}_INTERRUPT_DRIVEN": True,
    }.items():
        zephyr_add_prj_conf(key, value)

    groups = []
    for key, signal in ((CONF_TX_PIN, "UART_TX"), (CONF_RX_PIN, "UART_RX")):
        if key not in config:
            continue
        pin = config[key]
        number = pin[CONF_NUMBER]
        # uart_nrf52 idles high. Keep a disconnected RX stable unless the user
        # explicitly selects a pull-down; do not bias TX by default.
        bias = "bias-pull-up;" if key == CONF_RX_PIN else ""
        if pin.get("mode", {}).get("pullup"):
            bias = "bias-pull-up;"
        elif pin.get("mode", {}).get("pulldown"):
            bias = "bias-pull-down;"
        groups.append(f"group_{signal.lower()} {{ psels = <NRF_PSEL({signal}, {number // 32}, {number % 32})>; {bias} }};")
    psels = ">, <".join(
        f"NRF_PSEL({signal}, {config[key][CONF_NUMBER] // 32}, {config[key][CONF_NUMBER] % 32})"
        for key, signal in ((CONF_TX_PIN, "UART_TX"), (CONF_RX_PIN, "UART_RX")) if key in config
    )
    # A standard DTS speed avoids binding enum restrictions on arbitrary rates.
    # The requested rate (e.g. 256000) is applied with uart_configure in setup.
    zephyr_add_overlay(f"""
        #include <zephyr/dt-bindings/pinctrl/nrf-pinctrl.h>
        &pinctrl {{
            peripheral_uart{port}_default: peripheral_uart{port}_default {{
                {' '.join(groups)}
            }};
            peripheral_uart{port}_sleep: peripheral_uart{port}_sleep {{
                group1 {{ psels = <{psels}>; low-power-enable; }};
            }};
        }};
        &uart{port} {{
            compatible = "nordic,nrf-uarte";
            status = "okay";
            current-speed = <115200>;
            /delete-property/ hw-flow-control;
            pinctrl-0 = <&peripheral_uart{port}_default>;
            pinctrl-1 = <&peripheral_uart{port}_sleep>;
            pinctrl-names = "default", "sleep";
        }};
    """)
    return (
        cg.RawExpression(f"DEVICE_DT_GET(DT_NODELABEL(uart{port}))"),
        cg.RawExpression(f"reinterpret_cast<NRF_UARTE_Type *>(DT_REG_ADDR(DT_NODELABEL(uart{port})))"),
    )

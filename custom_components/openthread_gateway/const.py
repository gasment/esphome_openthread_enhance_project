"""Constants shared by the integration and the hardware-independent protocol."""
DOMAIN = "openthread_gateway"
VERSION = 3
SERVICE_TYPE = "_esphome-tgw._udp.local."
DEFAULT_PORT = 18513
MAX_DATAGRAM = 1232
MAX_NODES = 256
MAX_CANDIDATES = 128
MAX_ENTITIES = 256
MAX_PARTS = 128
MAX_MANIFEST = 65536
MAX_REVOKED = 1024
PLATFORMS = ["sensor", "binary_sensor", "event", "switch", "number", "select", "button", "text", "light", "fan", "cover", "climate", "lock", "valve"]
KIND_PLATFORM = {"numeric": "sensor", "text": "sensor", "binary": "binary_sensor", "input_text": "text",
                 **{kind: kind for kind in PLATFORMS if kind not in {"sensor", "binary_sensor", "text"}}}

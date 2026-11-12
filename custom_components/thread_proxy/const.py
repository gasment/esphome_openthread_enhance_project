"""Thread Proxy management constants."""
DOMAIN = "thread_proxy"
NAME = "Thread Proxy"
SERVICE_TYPE = "_thread-proxy._tcp.local."
COMPONENT = "thread_proxy_api"
PROTOCOL_VERSION = 1
DEFAULT_PORT = 6065
PLATFORMS = ["sensor", "binary_sensor"]
INFO_SERVICE = "thread_proxy.info"
STATUS_SERVICE = "thread_proxy.status"
PANEL_PATH = "thread-proxy"
STATES = ["connecting", "connected", "ready", "disconnected", "unsupported", "auth_error", "identity_mismatch", "duplicate_identity"]

"""Independent nRF52 entity adapters; NCS supplies the cJSON implementation."""
import esphome.config_validation as cv
from esphome.components.zephyr import zephyr_add_prj_conf

CODEOWNERS = []
DEPENDENCIES = ["nrf52"]
CONFLICTS_WITH = ["thread_entity_bridge"]
CONFIG_SCHEMA = cv.All(cv.Schema({}), cv.only_on_nrf52, cv.only_with_framework("zephyr"))


async def to_code(config):
    # NCS 2.9.2 provides cJSON.h and cJSON.c via nrf/modules/cjson.
    # Use its default malloc/free hooks, as in IDF; do not switch globally to
    # cJSON_os's k_malloc hooks or change another consumer's allocator.
    zephyr_add_prj_conf("CJSON_LIB", True)
    zephyr_add_prj_conf("NEWLIB_LIBC_FLOAT_PRINTF", True)

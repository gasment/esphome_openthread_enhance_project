"""Register a build-local OpenThread module with ESPHome's native nRF52 build."""
from functools import wraps
from pathlib import Path

from esphome.components import nrf52
from esphome.components.zephyr import add_extra_build_file
from esphome.core import CORE, EsphomeError

MODULE = "zephyr/esphome-openthread"
DATA_KEY = "openthread_nrf52_poll_search_module"


def register() -> None:
    component = Path(__file__).parent
    files = {
        "zephyr/module.yml": component / "poll_parent_search_module" / "module.yml",
        "esphome/CMakeLists.txt": component / "poll_parent_search_module" / "CMakeLists.txt",
        "esphome/add_module.cmake": component / "poll_parent_search_module" / "add_module.cmake",
        "esphome/prepare.py": component / "poll_parent_search_module" / "prepare.py",
        "esphome/openthread.patch": component / "patches" / "ncs-2.9.2-data-poll-parent-search.patch",
    }
    for destination, source in files.items():
        if not add_extra_build_file(f"{MODULE}/{destination}", source):
            raise EsphomeError(f"Duplicate Data Poll build file: {destination}")
    CORE.data[DATA_KEY] = True
    _register_cmake_extension()


def _register_cmake_extension() -> None:
    # ESPHome does not expose an app CMake preamble callback for the native nRF52
    # toolchain. Extend the generated build file in memory, before it is written.
    generate = getattr(nrf52, "_generate_cmake_lists", None)
    if generate is None:
        raise EsphomeError("This ESPHome version has no native nRF52 CMake generator for Data Poll parent search")
    if getattr(generate, "_openthread_poll_extension", False):
        return

    @wraps(generate)
    def generate_with_poll_module(*args, **kwargs):
        if not CORE.data.get(DATA_KEY):
            return generate(*args, **kwargs)
        write = nrf52.write_file_if_changed
        project = Path(CORE.relative_build_path("zephyr", "CMakeLists.txt"))

        def write_with_poll_module(path, contents, *write_args, **write_kwargs):
            if Path(path) == project:
                anchor = "find_package(Zephyr REQUIRED)"
                if contents.count(anchor) != 1:
                    raise EsphomeError("Cannot locate the Zephyr package declaration for Data Poll parent search")
                include = 'include("${CMAKE_CURRENT_LIST_DIR}/esphome-openthread/esphome/add_module.cmake")'
                contents = contents.replace(anchor, f"{include}\n{anchor}", 1)
            return write(path, contents, *write_args, **write_kwargs)

        nrf52.write_file_if_changed = write_with_poll_module
        try:
            return generate(*args, **kwargs)
        finally:
            nrf52.write_file_if_changed = write

    generate_with_poll_module._openthread_poll_extension = True
    nrf52._generate_cmake_lists = generate_with_poll_module

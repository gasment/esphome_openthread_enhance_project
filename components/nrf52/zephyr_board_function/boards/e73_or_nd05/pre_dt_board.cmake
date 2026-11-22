# SPDX-License-Identifier: Apache-2.0

# nRF52 SoC nodes intentionally share peripheral register blocks:
# - power@40000000 and clock@40000000
# - acl@4001e000 and flash-controller@4001e000
# Match Nordic's upstream board configurations and suppress only this warning.
list(APPEND EXTRA_DTC_FLAGS "-Wno-unique_unit_address_if_enabled")

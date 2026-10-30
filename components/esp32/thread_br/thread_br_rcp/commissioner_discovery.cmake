# Called only when the optional Commissioner is enabled. Patch a build-local
# copy and keep both the installed IDF and public OpenThread ABI unchanged.
set(discovery_original "${ot_dir}/openthread/src/core/thread/mle_ftd.cpp")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${discovery_original}")
file(READ "${discovery_original}" discovery_contents)
set(discovery_anchor "            mDiscoveryRequestCallback.Invoke(&info);")
string(FIND "${discovery_contents}" "${discovery_anchor}" discovery_found)
string(REGEX MATCHALL "mDiscoveryRequestCallback[.]Invoke" discovery_matches "${discovery_contents}")
list(LENGTH discovery_matches discovery_count)
if(discovery_found EQUAL -1 OR NOT discovery_count EQUAL 1)
    message(FATAL_ERROR "Unsupported OpenThread discovery callback layout; update the build-local Commissioner adapter")
endif()
set(discovery_replacement [=[            OffsetRange joinerAdvertisementRange;
            bool identified = false;
            if (info.mIsJoiner &&
                Tlv::FindTlvValueOffsetRange(aRxInfo.mMessage, Tlv::kDiscovery, joinerAdvertisementRange) == kErrorNone)
            {
                identified = thread_br_joiner_discovery_report(&info.mExtAddress, info.mVersion, &aRxInfo.mMessage,
                    joinerAdvertisementRange.GetOffset(), joinerAdvertisementRange.GetLength());
            }
            if (!identified)
            {
                mDiscoveryRequestCallback.Invoke(&info);
            }]=])
string(REPLACE "${discovery_anchor}" "${discovery_replacement}" discovery_contents "${discovery_contents}")
# The generated copy no longer lives beside mle.hpp. Resolve through the
# component's existing src/core include path instead of the original directory.
string(FIND "${discovery_contents}" "#include \"mle.hpp\"" discovery_include)
if(discovery_include EQUAL -1)
    message(FATAL_ERROR "Unsupported OpenThread MLE include layout")
endif()
string(REPLACE "#include \"mle.hpp\"" "#include \"thread/mle.hpp\"" discovery_contents "${discovery_contents}")
set(discovery_patched "${CMAKE_CURRENT_BINARY_DIR}/mle_ftd_commissioner.cpp")
file(WRITE "${discovery_patched}" "#include \"commissioner_discovery.h\"\n${discovery_contents}")
get_target_property(discovery_sources ${ot_lib} SOURCES)
set(discovery_source_found FALSE)
foreach(source IN LISTS discovery_sources)
    if(source MATCHES "(^|/)mle_ftd\\.cpp$")
        list(REMOVE_ITEM discovery_sources "${source}")
        set(discovery_source_found TRUE)
    endif()
endforeach()
if(NOT discovery_source_found)
    message(FATAL_ERROR "OpenThread mle_ftd.cpp was not found in the component source list")
endif()
set_property(TARGET ${ot_lib} PROPERTY SOURCES "${discovery_sources}")
target_sources(${ot_lib} PRIVATE "${discovery_patched}")

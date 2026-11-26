cmake_minimum_required(VERSION 3.16)
project(G2AOrigin VERSION 0.0.1 LANGUAGES CXX)

include(GNUInstallDirs)
include(CMakePackageConfigHelpers)

add_library(g2a_origin INTERFACE)
add_library(G2A::Origin ALIAS g2a_origin)

target_compile_features(g2a_origin INTERFACE cxx_std_17)
target_include_directories(g2a_origin INTERFACE
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
)

if(WIN32)
    target_link_libraries(g2a_origin INTERFACE ws2_32)
endif()

option(G2A_ORIGIN_BUILD_TESTS "Build C++17 G2A Origin SDK self-test" OFF)
if(G2A_ORIGIN_BUILD_TESTS)
    enable_testing()
    add_executable(g2a_origin_self_test tests/self_test.cpp)
    target_link_libraries(g2a_origin_self_test PRIVATE G2A::Origin)
    add_test(NAME g2a_origin_self_test COMMAND g2a_origin_self_test)
endif()

install(DIRECTORY include/g2a DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(TARGETS g2a_origin EXPORT G2AOriginTargets)
install(EXPORT G2AOriginTargets
    FILE G2AOriginTargets.cmake
    NAMESPACE G2A::
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/G2AOrigin
)

write_basic_package_version_file(
    "${CMAKE_CURRENT_BINARY_DIR}/G2AOriginConfigVersion.cmake"
    VERSION ${PROJECT_VERSION}
    COMPATIBILITY SameMajorVersion
    ARCH_INDEPENDENT
)

configure_package_config_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/cmake/G2AOriginConfig.cmake.in"
    "${CMAKE_CURRENT_BINARY_DIR}/G2AOriginConfig.cmake"
    INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/G2AOrigin
)

configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/cmake/g2a-origin.pc.in"
    "${CMAKE_CURRENT_BINARY_DIR}/g2a-origin.pc"
    @ONLY
)

install(FILES
    "${CMAKE_CURRENT_BINARY_DIR}/G2AOriginConfig.cmake"
    "${CMAKE_CURRENT_BINARY_DIR}/G2AOriginConfigVersion.cmake"
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/G2AOrigin
)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/g2a-origin.pc"
    DESTINATION ${CMAKE_INSTALL_DATADIR}/pkgconfig
)

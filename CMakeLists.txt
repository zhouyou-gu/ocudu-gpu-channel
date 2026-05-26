cmake_minimum_required(VERSION 3.22)

project(ocudu_gpu_channel VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(OCUDU_GPU_CHANNEL_BUILD_TESTS "Build unit tests" ON)
option(OCUDU_GPU_CHANNEL_ENABLE_CUDA "Enable CUDA backend when nvcc is available" ON)
set(OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES "120" CACHE STRING "CUDA architectures for ocudu-gpu-channel")

find_package(PkgConfig REQUIRED)
pkg_check_modules(ZMQ REQUIRED IMPORTED_TARGET libzmq)

set(THREADS_PREFER_PTHREAD_FLAG ON)
find_package(Threads REQUIRED)

include(CheckLanguage)
check_language(CUDA)
set(OCUDU_GPU_CHANNEL_HAS_CUDA 0)
if(OCUDU_GPU_CHANNEL_ENABLE_CUDA AND CMAKE_CUDA_COMPILER)
  enable_language(CUDA)
  set(CMAKE_CUDA_STANDARD 20)
  set(CMAKE_CUDA_STANDARD_REQUIRED ON)
  set(OCUDU_GPU_CHANNEL_HAS_CUDA 1)
endif()

add_library(ocudu_gpu_channel_lib
  src/backend.cpp
  src/broker.cpp
  src/config.cpp
  src/control_server.cpp
  src/cpu_backend.cpp
  src/latency.cpp
  src/mutable_params.cpp
  src/processing.cpp
  src/ring.cpp
)

# The GPU backend is the primary target; it is compiled in whenever the build
# has CUDA. The CPU backend (src/cpu_backend.cpp) is always built as the
# reference/fallback.
if(OCUDU_GPU_CHANNEL_HAS_CUDA)
  target_sources(ocudu_gpu_channel_lib PRIVATE
    src/cuda_backend.cu
    src/device_channel.cu
  )
  set_target_properties(ocudu_gpu_channel_lib PROPERTIES
    CUDA_ARCHITECTURES "${OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES}"
  )
endif()

target_include_directories(ocudu_gpu_channel_lib PUBLIC include)
target_link_libraries(ocudu_gpu_channel_lib PUBLIC PkgConfig::ZMQ Threads::Threads)
target_compile_definitions(ocudu_gpu_channel_lib PUBLIC
  OCUDU_GPU_CHANNEL_HAS_CUDA=${OCUDU_GPU_CHANNEL_HAS_CUDA}
)
# v3.2: surface the CUDA arch target to C++ so kernel_target_sm() can
# report it without re-parsing the cache. CMake's
# CUDA_ARCHITECTURES is a list ("120;90;..."); take the first entry as
# the canonical target for the strict-mode SM check.
if(OCUDU_GPU_CHANNEL_HAS_CUDA)
  list(GET OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES 0 _ocg_first_sm)
  target_compile_definitions(ocudu_gpu_channel_lib PUBLIC
    OCUDU_GPU_CHANNEL_KERNEL_SM=${_ocg_first_sm}
  )
endif()

if(MSVC)
  target_compile_options(ocudu_gpu_channel_lib PRIVATE $<$<COMPILE_LANGUAGE:CXX>:/W4>)
else()
  target_compile_options(ocudu_gpu_channel_lib PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-Wall -Wextra -Wpedantic>)
endif()

add_executable(ocudu-gpu-channel apps/ocudu_gpu_channel.cpp)
target_link_libraries(ocudu-gpu-channel PRIVATE ocudu_gpu_channel_lib)

add_executable(ocudu-gpu-channel-bench apps/ocudu_gpu_channel_bench.cpp)
target_link_libraries(ocudu-gpu-channel-bench PRIVATE ocudu_gpu_channel_lib)

add_executable(ocudu-zmq-source apps/ocudu_zmq_source.cpp)
target_link_libraries(ocudu-zmq-source PRIVATE ocudu_gpu_channel_lib)

add_executable(ocudu-zmq-sink apps/ocudu_zmq_sink.cpp)
target_link_libraries(ocudu-zmq-sink PRIVATE ocudu_gpu_channel_lib)

if(OCUDU_GPU_CHANNEL_BUILD_TESTS)
  enable_testing()

  add_executable(test_config tests/test_config.cpp)
  target_link_libraries(test_config PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME config COMMAND test_config)

  add_executable(test_mutable_params tests/test_mutable_params.cpp)
  target_link_libraries(test_mutable_params PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME mutable_params COMMAND test_mutable_params)

  add_executable(test_control_server tests/test_control_server.cpp)
  target_link_libraries(test_control_server PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME control_server COMMAND test_control_server)

  add_executable(test_runtime_update_parity tests/test_runtime_update_parity.cpp)
  target_link_libraries(test_runtime_update_parity PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME runtime_update_parity COMMAND test_runtime_update_parity)

  add_executable(test_hardware_probe tests/test_hardware_probe.cpp)
  target_link_libraries(test_hardware_probe PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME hardware_probe COMMAND test_hardware_probe)

  add_executable(test_processing tests/test_processing.cpp)
  target_link_libraries(test_processing PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME processing COMMAND test_processing)

  add_executable(test_ring tests/test_ring.cpp)
  target_link_libraries(test_ring PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME ring COMMAND test_ring)

  add_executable(test_broker tests/test_broker.cpp)
  target_link_libraries(test_broker PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME broker COMMAND test_broker)
endif()

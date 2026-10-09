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
  src/correlation.cpp
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
# report it without re-parsing the cache. CMake's CUDA_ARCHITECTURES is a
# list ("120;90;...") whose entries may carry -real / -virtual decoration
# ("80-real;90-virtual;..."); take the first entry and strip any decoration
# so the C++ macro is a bare integer (the lowest compiled SASS arch, i.e.
# the SM floor the strict-mode check compares the runtime device against).
if(OCUDU_GPU_CHANNEL_HAS_CUDA)
  list(GET OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES 0 _ocg_first_sm)
  string(REGEX MATCH "^[0-9]+" _ocg_first_sm "${_ocg_first_sm}")
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

# M4.6: the control plane had no client outside the unit tests, so the wire path
# itself had never been exercised end to end.
add_executable(ocudu-control-req apps/ocudu_control_req.cpp)
target_link_libraries(ocudu-control-req PRIVATE ocudu_gpu_channel_lib)

# M5.2: the multi-port test peer the mission's success criterion names. It owns
# BOTH ports in one process -- two independent single-port peers would not be
# one radio, which is the property the multi-port gate exists to check.
add_executable(ocudu-mimo-transport-peer apps/ocudu_mimo_transport_peer.cpp)
target_link_libraries(ocudu-mimo-transport-peer PRIVATE ocudu_gpu_channel_lib)

# Microbench for the runtime-mutation cost M (the D2H + stream-sync + H2D the
# current control path runs per dirty edge). CUDA-only; not a ctest -- runs on
# the GPU workstation. See benchmarks/bench_mutation_cost.cu.
if(OCUDU_GPU_CHANNEL_HAS_CUDA)
  add_executable(bench-mutation-cost benchmarks/bench_mutation_cost.cu)
  target_link_libraries(bench-mutation-cost PRIVATE ocudu_gpu_channel_lib)
  set_target_properties(bench-mutation-cost PROPERTIES
    CUDA_ARCHITECTURES "${OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES}"
  )
endif()

# R5a: a synthetic GPU tenant (back-to-back spin kernels) for the scheduling
# experiments of the robot-fight gate. See apps/ocudu_gpu_hog.cu.
if(OCUDU_GPU_CHANNEL_HAS_CUDA)
  add_executable(ocudu-gpu-hog apps/ocudu_gpu_hog.cu)
  set_target_properties(ocudu-gpu-hog PROPERTIES
    CUDA_ARCHITECTURES "${OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES}"
  )
endif()

if(OCUDU_GPU_CHANNEL_BUILD_TESTS)
  enable_testing()

  add_executable(test_config tests/core/test_config.cpp)
  target_link_libraries(test_config PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME config COMMAND test_config)

  add_executable(test_mutable_params tests/core/test_mutable_params.cpp)
  target_link_libraries(test_mutable_params PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME mutable_params COMMAND test_mutable_params)

  add_executable(test_control_server tests/core/test_control_server.cpp)
  target_link_libraries(test_control_server PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME control_server COMMAND test_control_server)

  add_executable(test_runtime_update_parity tests/core/test_runtime_update_parity.cpp)
  target_link_libraries(test_runtime_update_parity PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME runtime_update_parity COMMAND test_runtime_update_parity)

  add_executable(test_matrix_profile_history tests/core/test_matrix_profile_history.cpp)
  target_link_libraries(test_matrix_profile_history PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME matrix_profile_history COMMAND test_matrix_profile_history)

  add_executable(test_hardware_probe tests/core/test_hardware_probe.cpp)
  target_link_libraries(test_hardware_probe PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME hardware_probe COMMAND test_hardware_probe)

  add_executable(test_processing tests/core/test_processing.cpp)
  target_link_libraries(test_processing PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME processing COMMAND test_processing)

  add_executable(test_ring tests/core/test_ring.cpp)
  target_link_libraries(test_ring PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME ring COMMAND test_ring)

  add_executable(test_broker tests/core/test_broker.cpp)
  target_link_libraries(test_broker PRIVATE ocudu_gpu_channel_lib)
  add_test(NAME broker COMMAND test_broker)

  # The Sionna adapter conversion tests use only the Python standard library;
  # Sionna RT and pyzmq remain optional live-integration dependencies.
  find_package(Python3 COMPONENTS Interpreter QUIET)
  if(Python3_Interpreter_FOUND)
    add_test(
      NAME sionna_rt_adapter
      COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/sionna_bridge/test_sionna_rt_adapter.py
    )
    add_test(
      NAME sionna_web_ui
      COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/dashboard/test_sionna_web_ui.py
    )
    add_test(
      NAME native_sionna_rank1_renderer
      COMMAND ${Python3_EXECUTABLE}
              ${CMAKE_CURRENT_SOURCE_DIR}/scripts/native/render-sionna-rank1-configs.py
              --self-test
    )
  endif()
endif()

cmake_minimum_required(VERSION 3.16)
project(usrp_zmq_bridge CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(UHD REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(ZMQ REQUIRED libzmq)
find_package(Threads REQUIRED)

add_executable(usrp_zmq_bridge usrp_zmq_bridge.cpp)
target_include_directories(usrp_zmq_bridge PRIVATE ${UHD_INCLUDE_DIRS} ${ZMQ_INCLUDE_DIRS})
target_link_libraries(usrp_zmq_bridge PRIVATE ${UHD_LIBRARIES} ${ZMQ_LIBRARIES} Threads::Threads)

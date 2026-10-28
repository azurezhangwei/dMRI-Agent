cmake_minimum_required(VERSION 3.20.6...3.22.6 FATAL_ERROR)

project(DMRIAgent)

set(EXTENSION_HOMEPAGE "https://github.com/azurezhangwei/dMRI-Agent")
set(EXTENSION_CONTRIBUTORS "Fan’s Lab, Wei Zhang, Chengjin Li, Guanlin Huang")
set(EXTENSION_DESCRIPTION "3D Slicer frontend for dMRI-Agent through the shared HTTP/SSE service and a loopback scene API.")
set(EXTENSION_ICONURL "https://www.slicer.org/")
set(EXTENSION_SCREENSHOTURLS "NA")
set(EXTENSION_DEPENDS "NA")

find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

add_subdirectory(DMRIAgentSlicer)

include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})

set(MODULE_NAME DMRIAgentSlicer)

set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  )

set(MODULE_PYTHON_RESOURCES
  DMRIAgentLib/__init__.py
  DMRIAgentLib/service_client.py
  DMRIAgentLib/scene_assets.py
  DMRIAgentLib/runtime_bootstrap.py
  DMRIAgentLib/scene_server.py
  DMRIAgentLib/session_lifecycle.py
  DMRIAgentLib/service_integration.py
  DMRIAgentLib/activity_summary.py
  DMRIAgentLib/markdown_render.py
  )

slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )

if(BUILD_TESTING)
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)
endif()

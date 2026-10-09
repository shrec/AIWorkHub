from aiworkhub import __version__
from aiworkhub import server


def test_initialize_options_report_package_version() -> None:
    options = server.mcp._mcp_server.create_initialization_options()
    assert options.server_version == __version__

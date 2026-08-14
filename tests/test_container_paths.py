from backend.sandbox.container_paths import container_path_parts


def test_container_path_parts_posix() -> None:
    assert container_path_parts("/usr/local/bin/pip3") == ("/usr/local/bin", "pip3")
    assert container_path_parts("/tools.txt") == ("/", "tools.txt")

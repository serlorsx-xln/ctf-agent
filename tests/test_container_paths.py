from backend.sandbox.container import DockerSandbox
from backend.sandbox.container_paths import container_path_parts
from backend.tools.core import _humanize_read_error


def test_container_path_parts_posix() -> None:
    assert container_path_parts("/usr/local/bin/pip3") == ("/usr/local/bin", "pip3")
    assert container_path_parts("/tools.txt") == ("/", "tools.txt")


class _Status404(Exception):
    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.status = 404


def test_file_404_is_not_a_gone_container() -> None:
    gone = DockerSandbox._is_container_gone_error
    missing = _Status404(
        "404, message='Could not find the file /challenge/workspace/solve.py "
        "in container abcdef123456'"
    )
    assert gone(missing) is False
    assert gone(FileNotFoundError("No file found at /challenge/workspace/solve.py")) is False
    assert gone(RuntimeError("No such container: abcdef")) is True
    assert gone(_Status404("404 Client Error for container inspect")) is True


def test_read_file_error_hides_container_id() -> None:
    raw = _Status404(
        "404, message='Could not find the file /challenge/workspace/solve.py "
        "in container abcdef123456'"
    )
    msg = _humanize_read_error("/challenge/workspace/solve.py", raw)
    assert msg == "File not found: /challenge/workspace/solve.py"
    assert "abcdef" not in msg
    assert "404" not in msg

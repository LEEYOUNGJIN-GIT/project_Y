"""외부 프로세스 실행. 항상 인자 배열, shell=True 금지. Ctrl+C 시 자식 트리를 종료한다."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass
class ProcResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ProcessFailed(RuntimeError):
    def __init__(self, result: ProcResult, context: str = ""):
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        super().__init__(f"{context or '외부 명령 실패'} (종료 코드 {result.returncode}): "
                         f"{Path(result.args[0]).name}\n{tail}")
        self.result = result


def _popen_kwargs(low_priority: bool) -> dict:
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if low_priority:
            flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
        return {"creationflags": flags}
    kw: dict = {"start_new_session": True}
    if low_priority:
        kw["preexec_fn"] = lambda: os.nice(5)
    return kw


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(  # noqa: S603 - 고정 인자
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True, check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def run(args: Sequence[str | Path], *, input_text: str | None = None, timeout: float | None = None,
        low_priority: bool = False, env: dict | None = None, cwd: Path | None = None,
        check: bool = False, context: str = "") -> ProcResult:
    argv = [str(a) for a in args]
    proc = subprocess.Popen(  # noqa: S603 - shell=False, 인자 배열
        argv, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
        **_popen_kwargs(low_priority),
    )
    try:
        out, err = proc.communicate(
            input=input_text.encode("utf-8") if input_text is not None else None, timeout=timeout
        )
    except KeyboardInterrupt:
        kill_tree(proc)
        raise
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        res = ProcResult(argv, -9, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
        raise ProcessFailed(res, f"시간 초과({timeout}s)") from None
    res = ProcResult(argv, proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
    if check and not res.ok:
        raise ProcessFailed(res, context)
    return res


def is_windows() -> bool:
    return sys.platform.startswith("win")

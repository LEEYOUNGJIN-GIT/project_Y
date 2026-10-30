"""kaggle 2.x CLI의 `kernels push|status|output`만 흉내 내는 시험용 스크립트 (출력 형식은 kaggle 소스에서 확인).

FAKE_KAGGLE_STATUS: 상태 문자열(기본 KernelWorkerStatus.COMPLETE). FAKE_KAGGLE_TRACKS: 다운로드 때 복사할 디렉터리.
"""
import json
import os
import shutil
import sys
from pathlib import Path


def main(argv) -> int:
    if argv[:2] == ["kernels", "push"]:
        folder = Path(argv[argv.index("-p") + 1])
        meta = json.loads((folder / "kernel-metadata.json").read_text(encoding="utf-8"))
        if len(meta["title"]) < 5 or "/" not in meta["id"] or not (folder / meta["code_file"]).is_file():
            print("Title must be at least five characters", file=sys.stderr)
            return 1
        if not meta["is_private"] or not meta["enable_gpu"] or not meta["enable_internet"]:
            print("unexpected metadata", file=sys.stderr)
            return 1
        Path(os.environ["FAKE_KAGGLE_LOG"]).write_text(json.dumps({"argv": argv, "meta": meta}), encoding="utf-8")
        print("Kernel version 1 successfully pushed.  Please check progress at https://www.kaggle.com/code/" + meta["id"])
        return 0
    if argv[:2] == ["kernels", "status"]:
        st = os.environ.get("FAKE_KAGGLE_STATUS", "KernelWorkerStatus.COMPLETE")
        print(f'{argv[2]} has status "{st}"')
        if "ERROR" in st:
            print('Failure message: "boom"')
        return 0
    if argv[:2] == ["kernels", "output"]:
        dst = Path(argv[argv.index("-p") + 1])
        src = Path(os.environ["FAKE_KAGGLE_TRACKS"])
        shutil.copytree(src, dst / "tracks", dirs_exist_ok=True)
        return 0
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

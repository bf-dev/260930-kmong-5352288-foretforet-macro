# -*- coding: utf-8 -*-
"""빌드된 --onedir 폴더를 고객에게 보낼 ZIP 하나로 묶는다 (Kmong 고객 5352288).

    python ci/package_zip.py dist/foretforet-macro out

ZIP 안에는 최상위 폴더가 하나 있고(foretforet-macro-<ver>/) 그 안에
exe / _internal / 사용안내가 들어간다. 압축을 아무 데나 풀어도 파일이
흩어지지 않게 하기 위해서다.

고객이 더블클릭해야 하는 파일(exe)과 최상위 폴더 이름은 ASCII 로 고정한다.
윈도우 탐색기가 ZIP 안의 한글 이름을 UTF-8 로 못 읽는 경우가 아직 있고, 하필
그게 실행 파일이면 그대로 사고이기 때문이다. 안내문은 이름이 깨져도 열리기만
하면 되므로 한글 이름을 쓴다.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from foretforet import config  # noqa: E402

README_NAME = "사용안내.txt"


def build(dist_dir: Path, out_dir: Path) -> Path:
    exe = dist_dir / f"{config.APP_SLUG}.exe"
    if not exe.is_file():
        raise SystemExit(f"exe not found: {exe}")

    top = f"{config.APP_SLUG}-{config.APP_VERSION}"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{top}.zip"
    if zip_path.exists():
        zip_path.unlink()

    files = sorted(p for p in dist_dir.rglob("*") if p.is_file())
    # ZIP_DEFLATED 로 압축한다. 압축 자체는 디펜더 판정과 무관하다(실행 시점에
    # 푸는 동작이 없는 것이 핵심이다). 전송 크기만 줄인다.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            zf.write(p, f"{top}/{p.relative_to(dist_dir).as_posix()}")
        readme = ROOT / "_읽어주세요.txt"
        if readme.is_file():
            zf.writestr(f"{top}/{README_NAME}",
                        readme.read_text(encoding="utf-8"))

    print(f"packaged {zip_path} ({zip_path.stat().st_size:,} bytes, "
          f"{len(files) + 1} entries)")
    return zip_path


if __name__ == "__main__":
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist/foretforet-macro")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "out")
    build(dist.resolve(), out.resolve())

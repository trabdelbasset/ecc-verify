import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _reset_config():
    from ecc_verify import config
    config.set_current(None)
    yield
    config.set_current(None)


@pytest.fixture
def fake_ecc(tmp_path):
    """A stand-in `ecc` executable: `version --json` and `pdk show --json` answer; anything else exits 0."""
    root = tmp_path / "pdkroot"
    (root / "IP/STD_cell/libA").mkdir(parents=True)
    p = tmp_path / "bin" / "ecc"
    p.parent.mkdir()
    p.write_text(f"""#!/bin/sh
case "$*" in
  "version --json") echo '{{"ecc":"9.9.9","build_commit":"abcdef0123456789","build_date":"20260101","tools":{{}}}}' ;;
  "pdk show --json") echo '[{{"pdk":"show","name":"ics55","root":"{root}","source":"fake"}}]' ;;
  *) ;;
esac
""")
    p.chmod(0o755)
    return p, root

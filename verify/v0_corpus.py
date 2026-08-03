"""V0 gate — confirms the Phase 0 private bundle is present and sane before
anything else runs. Run: uv run python verify/v0_corpus.py"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> bool:
    ok = True

    style_md = ROOT / "STYLE.md"
    if not style_md.exists():
        print("FAIL: STYLE.md missing — get the private bundle from Trevor (SETUP.md Phase 0)")
        ok = False
    elif len(style_md.read_text()) < 2000:
        print("FAIL: STYLE.md exists but looks too short to be real")
        ok = False
    else:
        print(f"OK: STYLE.md present ({len(style_md.read_text())} chars)")

    corpus_db = ROOT / "corpus" / "corpus.sqlite"
    if not corpus_db.exists():
        print("FAIL: corpus/corpus.sqlite missing — get the private bundle from Trevor")
        ok = False
    else:
        conn = sqlite3.connect(corpus_db)
        n = conn.execute("SELECT count(*) FROM posts WHERE author='Scott J. Hyman'").fetchone()[0]
        conn.close()
        if n < 1000:
            print(f"FAIL: corpus.sqlite has only {n} Scott Hyman posts, expected ~2,939")
            ok = False
        else:
            print(f"OK: corpus.sqlite has {n} Scott Hyman posts")

    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

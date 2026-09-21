"""What `extract` says the papers hold, pinned so that a layout change cannot pass unnoticed.

`text.txt` is the ground truth every quote is checked against, so a change in what the layout
produces changes what the validator accepts. Twelve mutations of `pdf_text.py` were found that
change a real paper's `text.txt` -- two of them stop extraction from working at all -- while the
whole test suite stayed green, because the tests that read a real paper assert a handful of
sentences and the rest run on synthetic pages.

    python3 scripts/text_digests.py --out scripts/tests/text_digests.json

Regenerate only when a layout change is intended, and say in the commit what moved. The digests
are per page, so a mismatch names the page rather than only the paper.

The output of `pdftotext -layout` is its own version's, so the file records the version it was
made with and the test skips rather than fails under another one. A wrong verdict from a
different toolchain would be worse than no verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PAPERS = SCRIPTS.parent / "evals" / "papers"


def pdftotext_version() -> str | None:
    """The version string, or None where pdftotext cannot be run."""
    try:
        done = subprocess.run(["pdftotext", "-v"], capture_output=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"pdftotext version (\S+)", (done.stdout or "") + (done.stderr or ""))
    return found.group(1) if found else None


def digest_of(pdf: Path) -> dict:
    """Extract the paper and describe what came out, page by page."""
    with tempfile.TemporaryDirectory() as tmp:
        done = subprocess.run([sys.executable, str(SCRIPTS / "cea_claims.py"), "extract",
                               str(pdf), "--out", tmp], capture_output=True, encoding="utf-8",
                              errors="replace", timeout=900)
        found = sorted(Path(tmp).rglob("text.txt"))
        if not found:
            raise RuntimeError(f"extract wrote no text.txt for {pdf.name}: "
                               f"{(done.stdout or '') + (done.stderr or '')}")
        text = found[0].read_text(encoding="utf-8")
        # What the agent is told, with the directories taken out so the pin holds on any
        # machine. The page count, the figure list and where the references were removed are
        # what the next step acts on, and a change in them is a change in what gets recorded.
        said = (done.stdout or "").replace(tmp, "<out>")
        said = re.sub(r"/\S+/(?=[\w.-]+\.pdf)", "", said)
    pages = re.split(r"^=== page (\d+) ===$", text, flags=re.M)[1:]
    per_page = {}
    for number, body in zip(pages[::2], pages[1::2]):
        per_page[number] = {
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "lines": len(body.splitlines()),
            "words": len(body.split()),
        }
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "pages": len(per_page), "chars": len(text), "per_page": per_page,
            "said": said.splitlines()}


def collect() -> dict:
    version = pdftotext_version()
    if version is None:
        raise RuntimeError("pdftotext is not installed, so there is nothing to pin")
    papers = sorted(PAPERS.glob("*.pdf"))
    if not papers:
        raise RuntimeError(f"no papers in {PAPERS}, which are third-party and not committed")
    return {"pdftotext": version,
            "papers": {p.stem: digest_of(p) for p in papers}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the digests here")
    args = ap.parse_args(argv)
    now = collect()
    if args.out:
        Path(args.out).write_text(json.dumps(now, indent=1, sort_keys=True) + "\n",
                                  encoding="utf-8")
        print(f"CEA_DIGESTS: {len(now['papers'])} paper(s) written to {args.out} "
              f"(pdftotext {now['pdftotext']})")
    else:
        print(json.dumps(now, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

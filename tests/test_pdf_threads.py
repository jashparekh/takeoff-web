"""pdfium crashes the process if two threads use it at once; pdf.py serialises every call."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.pipeline import pdf

SRC = sorted((Path(__file__).parents[1] / 'app/data/worklounge/source').glob('*.pdf'))


def test_many_threads_at_once():
    with ThreadPoolExecutor(8) as ex:
        infos = list(ex.map(lambda f: pdf.inspect(str(f), 100), SRC * 4))
        renders = list(ex.map(lambda f: pdf.render_page(str(f), 1), SRC * 2))
    assert len({i[0].page_hash for i in infos}) == len(SRC)
    assert all(r.display_dpi == 300 for r in renders)

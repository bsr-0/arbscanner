"""Build the static GitHub Pages dashboard artifacts."""

from dataclasses import dataclass
from pathlib import Path
from shutil import copyfile

from arbscanner.config import PAGES_DATA_PATH, PAGES_DIST_DIR, PAGES_INDEX_PATH, SITE_TEMPLATE_PATH
from arbscanner.export import export_dashboard_data


@dataclass
class BuildResult:
    """Paths written by a static site build."""

    index_path: Path
    data_path: Path


def build_pages_site(
    *,
    hours: int = 24,
    min_edge: float = 0.0,
    limit: int = 100,
    output_dir: Path | None = None,
    template_path: Path | None = None,
) -> BuildResult:
    """Build the static GitHub Pages site into ``output_dir``.

    Copies the checked-in HTML template and regenerates ``data.json`` from the
    opportunities database and matched-pairs cache.
    """
    output_dir = output_dir or PAGES_DIST_DIR
    template_path = template_path or SITE_TEMPLATE_PATH

    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / PAGES_INDEX_PATH.name
    data_path = output_dir / PAGES_DATA_PATH.name

    copyfile(template_path, index_path)
    export_dashboard_data(
        hours=hours,
        min_edge=min_edge,
        limit=limit,
        output_path=data_path,
    )
    return BuildResult(index_path=index_path, data_path=data_path)

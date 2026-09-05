from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError, select_autoescape

from utils.scrape.soup import SoupScraper


ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data.yaml"
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
DIST_DIR = ROOT / "dist"

log = logging.getLogger(__name__)


class BuildError(Exception):
    pass


def load_data(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as file_handle:
            data = yaml.safe_load(file_handle)
    except yaml.YAMLError as error:
        raise BuildError(f"Could not parse {path.name}: {error}") from error
    except OSError as error:
        raise BuildError(f"Could not read {path}: {error}") from error

    if not isinstance(data, dict):
        raise BuildError(f"{path.name} must contain a YAML mapping at the top level.")
    return data


def fallback_site_name(url: str) -> str:
    return urllib.parse.urlparse(url).netloc or url


def pypi_project_name(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "pypi.org":
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] == "project":
        return path_parts[1]
    return None


def pypi_metadata(project_name: str) -> dict | None:
    url = f"https://pypi.org/pypi/{project_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            info = json.load(response).get("info", {})
    except (json.JSONDecodeError, urllib.error.URLError, TimeoutError, OSError) as error:
        log.warning("Could not fetch PyPI metadata for %s: %s", project_name, error)
        return None

    return {
        "title": info.get("name") or project_name,
        "description": info.get("summary") or "",
        "site_name": "PyPI",
        "link": info.get("project_url") or f"https://pypi.org/project/{project_name}/",
    }


def normalize_post(post: dict) -> dict:
    normalized = post.copy()
    link = normalized.get("link")
    if not link:
        raise BuildError("Every post must define a link.")

    normalized.setdefault("title", link)
    normalized.setdefault("description", "")
    normalized.setdefault("image", "")
    normalized.setdefault("site_name", fallback_site_name(link))
    return normalized


def enrich_post(post: dict) -> dict:
    enriched = normalize_post(post)
    link = enriched["link"]

    if project_name := pypi_project_name(link):
        if metadata := pypi_metadata(project_name):
            for key, value in metadata.items():
                if not post.get(key):
                    enriched[key] = value
            return enriched

    try:
        if enriched.get("captcha", False):
            try:
                from utils.scrape.microlink import MicrolinkScraper

                scraper = MicrolinkScraper(link)
            except Exception as error:
                log.warning("Microlink metadata failed for %s: %s", link, error)
                scraper = SoupScraper(link)
        else:
            scraper = SoupScraper(link)
    except Exception as error:
        log.warning("Could not fetch metadata for %s: %s", link, error)
        return enriched

    if not post.get("title"):
        enriched["title"] = scraper.get_og_title() or link
    if not post.get("locale"):
        enriched["locale"] = scraper.get_og_locale()
    if not post.get("description"):
        enriched["description"] = scraper.get_og_description() or ""
    if not post.get("image"):
        enriched["image"] = scraper.get_og_image() or ""
    if not post.get("site_name"):
        enriched["site_name"] = scraper.get_og_site_name() or fallback_site_name(link)
    enriched["link"] = scraper.get_og_url() or link

    return enriched


def enrich_posts(data: dict, skip_metadata: bool) -> dict:
    posts = data.get("posts", [])
    if not isinstance(posts, list):
        raise BuildError("posts must be a list.")

    if skip_metadata:
        data = data.copy()
        data["posts"] = [normalize_post(post) for post in posts]
        return data

    data = data.copy()
    data["posts"] = [enrich_post(post) for post in posts]
    return data


def render(data: dict) -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "xml"]),
    )
    try:
        return environment.get_template("index.html").render(**data)
    except TemplateError as error:
        raise BuildError(f"Could not render templates: {error}") from error


def write_dist(html: str, data: dict) -> None:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True)

    (DIST_DIR / "index.html").write_text(html, encoding="utf-8")

    if STATIC_DIR.exists():
        shutil.copytree(STATIC_DIR, DIST_DIR / "static")

    custom_domain = data.get("deployment", {}).get("custom_domain")
    if custom_domain:
        (DIST_DIR / "CNAME").write_text(f"{custom_domain}\n", encoding="utf-8")


def build(skip_metadata: bool = False) -> None:
    data = load_data(DATA_FILE)
    data = enrich_posts(data, skip_metadata=skip_metadata)
    html = render(data)
    write_dist(html, data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the static personal website.")
    parser.add_argument(
        "--skip-post-metadata",
        action="store_true",
        help="Render posts only from values already present in data.yaml.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    try:
        build(skip_metadata=args.skip_post_metadata)
    except BuildError as error:
        print(f"Build failed: {error}", file=sys.stderr)
        return 1
    print(f"Generated {DIST_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
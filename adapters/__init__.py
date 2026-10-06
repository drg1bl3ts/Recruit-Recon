from .base import Adapter, Job
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter
from .workable import WorkableAdapter
from .workday import WorkdayAdapter
from .ashby import AshbyAdapter
from .paylocity import PaylocityAdapter
from .html_scraper import HtmlScraperAdapter
from .icims import ICIMSAdapter
from .static import StaticAdapter
from .amazon import AmazonAdapter
from .eightfold import EightfoldAdapter
from .google import GoogleAdapter

REGISTRY = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "workable": WorkableAdapter,
    "workday": WorkdayAdapter,
    "ashby": AshbyAdapter,
    "paylocity": PaylocityAdapter,
    "html_scraper": HtmlScraperAdapter,
    "icims": ICIMSAdapter,
    "static": StaticAdapter,
    "amazon": AmazonAdapter,
    "eightfold": EightfoldAdapter,
    "google": GoogleAdapter,
}


def get_adapter(name: str) -> type[Adapter]:
    if name not in REGISTRY:
        raise KeyError(f"unknown adapter: {name}. available: {list(REGISTRY)}")
    return REGISTRY[name]


__all__ = ["Adapter", "Job", "get_adapter", "REGISTRY"]

"""
Static adapter — for shops with no usable JSON API or where the application
path is direct email.

Configured in config.yaml under each company's `config:` block:
    static_roles: [list of role titles]
    url:          - public reference URL (will be verified)
    apply_url:    - what to open when the user clicks "Apply"
    note:         - context shown on the card

Verification still runs against `url` so we can detect when (e.g.) a careers
page returns 404 because the company moved domains.
"""

from typing import Iterable

from .base import Adapter, Job


class StaticAdapter(Adapter):
    name = "static"

    @property
    def is_static(self) -> bool:
        return True

    def fetch(self) -> Iterable[Job]:
        roles = self.config.get("static_roles", [])
        url = self.config.get("url", "")
        apply_url = self.config.get("apply_url", url)
        note = self.config.get("note", "")

        for idx, title in enumerate(roles, 1):
            yield Job(
                id=f"static-{idx}",
                title=title,
                location="(see source)",
                url=url,
                apply_url=apply_url,
                description=note,
                raw={"static": True, "note": note},
            )

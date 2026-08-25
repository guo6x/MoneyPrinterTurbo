"""Canonical product branding for AIDrama Studio.

The product layer deliberately keeps branding in one small, dependency-free
module.  Internal modules may continue to use upstream names where that is
useful for integration, but user-facing pages should read from :data:`BRAND`.
The logo path is replaceable through ``AIDRAMA_LOGO_PATH`` without changing
page code.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class BrandConfig:
    """Public product metadata used by the AIDrama UI and launchers."""

    product_name: str
    short_name: str
    tagline: str
    version: str
    logo_path: Path
    icon_path: Path
    company_name: str = ""

    @property
    def logo_exists(self) -> bool:
        return self.logo_path.is_file()

    def as_public_dict(self) -> dict[str, str | bool]:
        """Return JSON/UI-safe metadata without exposing environment values."""

        values = asdict(self)
        values["logo_path"] = str(self.logo_path)
        values["icon_path"] = str(self.icon_path)
        values["logo_exists"] = self.logo_exists
        return values


_PACKAGE_ROOT = Path(__file__).resolve().parent
_DEFAULT_LOGO = _PACKAGE_ROOT / "assets" / "brand-mark.svg"


def get_brand_config() -> BrandConfig:
    """Build the canonical brand config from safe, non-secret overrides."""

    override_logo = os.getenv("AIDRAMA_LOGO_PATH", "").strip()
    logo_path = Path(override_logo).expanduser() if override_logo else _DEFAULT_LOGO
    return BrandConfig(
        product_name="AIDrama Studio",
        short_name="AIDrama",
        tagline="AI 短剧全链路制作工作台",
        version=os.getenv("AIDRAMA_VERSION", "1.0.0").strip() or "1.0.0",
        logo_path=logo_path,
        icon_path=logo_path,
        company_name=os.getenv("AIDRAMA_COMPANY_NAME", "").strip(),
    )


BRAND = get_brand_config()

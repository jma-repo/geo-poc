"""The catalog is the single source of truth.

Every page, feed, structured-data block and generated answer is derived from it,
so AI engines see one consistent set of facts everywhere they look.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

Availability = Literal["in_stock", "out_of_stock", "pre_order", "backorder", "unknown"]


class Claim(BaseModel):
    """A verifiable statement. Statistics with a source are among the strongest citation signals."""
    text: str
    source_url: HttpUrl | None = None


class FAQ(BaseModel):
    q: str
    a: str


class Competitor(BaseModel):
    name: str
    aliases: list[str] = []
    domain: str | None = None


class Product(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    name: str
    slug: str = Field(pattern=r"^[a-z0-9-]+$")
    url: HttpUrl
    image_url: HttpUrl
    summary: str = Field(description="Answer-first one-liner: what it is and who it is for.")
    description: str
    price: float = Field(gt=0)
    currency: str = Field(default="CAD", pattern=r"^[A-Z]{3}$")
    availability: Availability = "in_stock"
    gtin: str | None = None
    mpn: str | None = None
    category: str
    use_cases: list[str] = []
    specs: dict[str, str] = {}
    claims: list[Claim] = []
    faqs: list[FAQ] = []
    rating_value: float | None = Field(default=None, ge=1, le=5)
    review_count: int | None = Field(default=None, ge=0)
    updated: date

    @model_validator(mode="after")
    def _rating_needs_reviews(self):
        # Never publish a rating without real reviews behind it.
        if (self.rating_value is None) != (not self.review_count):
            raise ValueError(f"{self.id}: rating_value and review_count must be set together")
        return self


class Brand(BaseModel):
    name: str
    aliases: list[str] = []
    legal_name: str | None = None
    site_url: HttpUrl
    logo_url: HttpUrl | None = None
    description: str
    city: str | None = None
    region: str | None = None
    country: str = Field(default="CA", pattern=r"^[A-Z]{2}$")
    contact_email: str | None = None
    same_as: list[HttpUrl] = Field(default=[], description="Official profiles: socials, marketplaces, Wikipedia.")
    return_days: int | None = Field(default=None, ge=0)
    ships_to: list[str] = ["CA"]


class Catalog(BaseModel):
    brand: Brand
    competitors: list[Competitor] = []
    products: list[Product]
    allow_ai_training: bool = Field(
        default=True,
        description="Allow model-training crawlers. Being in training data helps models know you "
                    "even when they do not search; set false if you prefer not to.")

    @field_validator("products")
    @classmethod
    def _unique(cls, v: list[Product]):
        for attr in ("id", "slug"):
            seen = [getattr(p, attr) for p in v]
            dup = {x for x in seen if seen.count(x) > 1}
            if dup:
                raise ValueError(f"duplicate product {attr}: {sorted(dup)}")
        return v

    @property
    def domain(self) -> str:
        return self.brand.site_url.host.removeprefix("www.")

    def facts_text(self) -> str:
        """Every fact we are willing to state, as plain text. Used to ground and check generated copy."""
        b = self.brand
        lines = [f"Brand: {b.name}. {b.description}"]
        if b.return_days is not None:
            lines.append(f"Returns: {b.return_days} days.")
        lines.append(f"Ships to: {', '.join(b.ships_to)}.")
        for p in self.products:
            lines += [f"\n## {p.name} (id {p.id})", p.summary, p.description,
                      f"Price: {p.price:.2f} {p.currency}. Availability: {p.availability}.",
                      f"Category: {p.category}. Use cases: {'; '.join(p.use_cases)}."]
            lines += [f"{k}: {v}" for k, v in p.specs.items()]
            lines += [f"Claim: {c.text}" + (f" (source: {c.source_url})" if c.source_url else "") for c in p.claims]
            lines += [f"FAQ: {f.q} — {f.a}" for f in p.faqs]
            if p.review_count:
                lines.append(f"Rating: {p.rating_value} from {p.review_count} reviews.")
        if self.competitors:
            lines.append("\nCompetitors (no facts about them are known): "
                         + ", ".join(c.name for c in self.competitors))
        return "\n".join(lines)

    def tracked_brands(self) -> dict[str, list[str]]:
        """Brand name -> aliases, ours first. Used for mention detection."""
        out = {self.brand.name: [self.brand.name, *self.brand.aliases]}
        for c in self.competitors:
            out[c.name] = [c.name, *c.aliases]
        return out


def load(path: str | Path) -> Catalog:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Catalog.model_validate(data)


_NUM = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)*(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    """Numbers as normalized strings: '1,299.00' -> '1299', '4.50' -> '4.5'."""
    out = set()
    for m in _NUM.findall(text):
        n = m.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n.lstrip("-") or "0")
    return out

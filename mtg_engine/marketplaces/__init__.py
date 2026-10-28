from ..config import Settings
from .base import Marketplace, RawListing
from .milanuncios import Milanuncios
from .wallapop import Wallapop

REGISTRY: dict[str, type[Marketplace]] = {
    Wallapop.name: Wallapop,
    Milanuncios.name: Milanuncios,
}


def get_marketplace(name: str, settings: Settings) -> Marketplace:
    return REGISTRY[name](settings, settings.marketplaces[name])


__all__ = ["Marketplace", "RawListing", "REGISTRY", "get_marketplace"]

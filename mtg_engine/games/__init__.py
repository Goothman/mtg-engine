from ..config import Settings
from .base import CatalogCard, GameProvider
from .mtg import MtgProvider
from .pokemon import PokemonProvider

REGISTRY: dict[str, type[GameProvider]] = {
    MtgProvider.name: MtgProvider,
    PokemonProvider.name: PokemonProvider,
}


def get_provider(name: str, settings: Settings) -> GameProvider:
    return REGISTRY[name](settings, settings.games[name])


__all__ = ["CatalogCard", "GameProvider", "REGISTRY", "get_provider"]

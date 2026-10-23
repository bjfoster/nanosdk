"""PokéAPI: follow-the-next-URL pagination, async fan-out, and a polars DataFrame.

Run:  pip install "nanoapi[polars]"
      python examples/pokeapi.py
"""

import asyncio
from itertools import islice
from typing import Any

import httpx
import polars as pl
from pydantic import BaseModel

import nanoapi

# --- models -----------------------------------------------------------------


class NamedResource(BaseModel):
    name: str
    url: str


class ResourceList(BaseModel):
    count: int
    next: str | None
    results: list[NamedResource]


class ListParams(BaseModel):
    limit: int = 20
    offset: int = 0


class Stat(BaseModel):
    base_stat: int
    stat: NamedResource


class TypeSlot(BaseModel):
    slot: int
    type: NamedResource


class Pokemon(BaseModel):
    id: int
    name: str
    height: int  # decimetres
    weight: int  # hectograms
    stats: list[Stat]
    types: list[TypeSlot]


class ByName(BaseModel):
    name: str


# --- a custom pagination strategy -------------------------------------------


def follow_next(page: nanoapi.Page[ListParams, ResourceList, NamedResource]) -> httpx.URL | None:
    """PokéAPI hands us the next page's full URL in the body. Returning an
    ``httpx.URL`` tells nanoapi to re-send the request to exactly that URL."""
    return httpx.URL(page.data.next) if page.data.next else None


# --- client -----------------------------------------------------------------


class PokeRoutes(nanoapi.Routes):
    list_pokemon = nanoapi.get("/pokemon", request=ListParams, response=ResourceList).paginate(
        follow_next, items=lambda body: body.results
    )
    get_pokemon = nanoapi.get("/pokemon/{name}", request=ByName, response=Pokemon)


BASE_URL = "https://pokeapi.co/api/v2"


class PokeAPI(PokeRoutes, nanoapi.Client):
    def __init__(self) -> None:
        super().__init__(BASE_URL, retry=nanoapi.Retry())


class AsyncPokeAPI(PokeRoutes, nanoapi.AsyncClient):
    def __init__(self) -> None:
        super().__init__(BASE_URL, retry=nanoapi.Retry(), limits=httpx.Limits(max_connections=20))


def row(mon: Pokemon) -> dict[str, Any]:
    """Flatten the nested model into one DataFrame row."""
    stats = {s.stat.name.replace("-", "_"): s.base_stat for s in mon.stats}
    return {
        "id": mon.id,
        "name": mon.name,
        "primary_type": mon.types[0].type.name,
        "height_m": mon.height / 10,
        "weight_kg": mon.weight / 10,
        **stats,
        "total": sum(stats.values()),
    }


async def fetch_all(names: list[str]) -> list[Pokemon]:
    async with AsyncPokeAPI() as api:
        return await asyncio.gather(*(api.get_pokemon(name=name) for name in names))


def main() -> None:
    # Sync + lazy: 151 items at 50 per page = 4 requests, then we stop.
    with PokeAPI() as api:
        print(f"{api.list_pokemon(limit=1).count} pokémon in the API")
        names = [ref.name for ref in islice(api.list_pokemon.iter(limit=50), 151)]

    # Async fan-out: 151 detail requests, 20 connections at a time.
    mons = asyncio.run(fetch_all(names))

    df = nanoapi.frames.polars([row(mon) for mon in mons])
    print(df.sort("total", descending=True).select("name", "primary_type", "total").head(5))
    print(
        df.group_by("primary_type")
        .agg(pl.len().alias("count"), pl.col("total").mean().round(1).alias("avg_total"))
        .sort("count", descending=True)
        .head(5)
    )


if __name__ == "__main__":
    main()

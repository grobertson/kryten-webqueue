import pytest

from kryten_webqueue.catalog.sync import CatalogSync


class _EmptyResponse:
    status_code = 200
    url = "https://cms.example/api/v1/manage_media?page_size=50"
    text = '{"results": []}'

    def json(self):
        return {"results": [], "next": None}


class _EmptyClient:
    async def get(self, *_args, **_kwargs):
        return _EmptyResponse()

    async def aclose(self):
        pass


class _FakeDb:
    def __init__(self, catalog_count=0):
        self.deleted = False
        self.finished = []
        self.catalog_count = catalog_count

    async def start_sync_log(self):
        return 1

    async def browse_count(self, **_kwargs):
        return self.catalog_count

    async def delete_stale_catalog_items(self, _started_at):
        self.deleted = True
        return 9999

    async def finish_sync_log(self, _log_id, stats, status):
        self.finished.append((stats.copy(), status))


@pytest.mark.asyncio
async def test_empty_sync_response_preserves_existing_catalog():
    db = _FakeDb()
    sync = CatalogSync(mediacms_url="https://cms.example", mediacms_token="x", db=db)
    await sync._client.aclose()
    sync._client = _EmptyClient()

    async def no_prefetch():
        pass

    sync._prefetch_all_facets = no_prefetch
    await sync.sync()

    assert not db.deleted
    assert db.finished == [
        ({"seen": 0, "new": 0, "updated": 0, "errors": 0, "deleted": 0}, "error")
    ]


class _PartialResponse:
    status_code = 200
    url = "https://cms.example/api/v1/manage_media?page_size=50"
    text = '{"results": [{"friendly_token": "only-item"}]}'

    def json(self):
        return {"results": [{"friendly_token": "only-item"}], "next": None}


class _PartialClient(_EmptyClient):
    async def get(self, *_args, **_kwargs):
        return _PartialResponse()


@pytest.mark.asyncio
async def test_partial_sync_response_refuses_large_stale_prune():
    db = _FakeDb(catalog_count=1_000)
    sync = CatalogSync(mediacms_url="https://cms.example", mediacms_token="x", db=db)
    await sync._client.aclose()
    sync._client = _PartialClient()

    async def no_prefetch():
        pass

    async def no_process(_media, stats):
        stats["updated"] += 1

    sync._prefetch_all_facets = no_prefetch
    sync._process_item = no_process
    await sync.sync()

    assert not db.deleted
    assert db.finished == [
        ({"seen": 1, "new": 0, "updated": 1, "errors": 1, "deleted": 0}, "error")
    ]

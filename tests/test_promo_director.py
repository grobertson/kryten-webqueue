"""PromoDirector behaviour (Feature 2).

Covers cadence (item + minute), weighted type selection, no_repeat / sequential
clip ordering, Feature-Presentation and Viewer's-Choice lead-ins, stacked
ordering, the synchronous Viewer's-Choice pay hook, lead-in cancel cleanup, and
the immutable-event no-op.
"""

from datetime import datetime, timedelta, UTC

from kryten_webqueue.config import PromoConfig, PromoTypeConfig, GeneralPromoConfig
from kryten_webqueue.promos.director import PromoDirector, remove_lead_in_for


class _FakeApiGate:
    def __init__(self, now_playing=None):
        self._np = now_playing
        self._uid = 1000
        self.adds = []
        self.moves = []
        self.deleted = []

    async def get_now_playing(self):
        return self._np

    async def playlist_add(self, media_type, media_id, position="end", temp=True):
        self._uid += 1
        self.adds.append({"uid": self._uid, "media_id": media_id, "type": media_type})
        return {"success": True, "uid": self._uid}

    async def playlist_move(self, uid, position):
        self.moves.append((uid, position))
        return {"success": True}

    async def playlist_delete(self, uid):
        self.deleted.append(uid)
        return {"success": True}


class _FakeShadow:
    def __init__(self, items, now_playing=None):
        self._items = [dict(it) for it in items]
        self.now_playing = now_playing
        self._reindex()

    def _reindex(self):
        for i, it in enumerate(self._items):
            it["position"] = i

    @property
    def items(self):
        return [dict(it) for it in self._items]

    async def insert_at(self, item, position):
        item = dict(item)
        item["position"] = position
        self._items.insert(position, item)
        self._reindex()

    async def remove(self, uid):
        self._items = [it for it in self._items if it["uid"] != uid]
        self._reindex()


class _FakeDb:
    def __init__(self, pools=None, event_lock=False):
        self._pools = pools or {}
        self._event_lock = event_lock

    async def is_event_lock_active(self):
        return self._event_lock

    async def get_promo_pool_items(self, promo_type):
        return list(self._pools.get(promo_type, []))


def _content(uid, *, is_pay=False, duration=300, is_promo=False, promo_type=None, lead_in_for_uid=None):
    return {
        "uid": uid, "title": f"Item {uid}", "is_pay": is_pay, "duration_sec": duration,
        "is_promo": is_promo, "promo_type": promo_type, "lead_in_for_uid": lead_in_for_uid,
        "media_type": "cm", "media_id": f"m{uid}",
    }


def _clip(mid, dur=15):
    return {"media_type": "cm", "media_id": mid, "title": f"Clip {mid}", "duration_sec": dur}


def _config(**over):
    base = dict(
        enabled=True,
        movie_threshold_seconds=3600.0,
        general=GeneralPromoConfig(every_n_items=4, every_m_minutes=20.0, no_repeat=True),
        types={
            "channel_identity": PromoTypeConfig(enabled=True, order="random", weight=3),
            "event": PromoTypeConfig(enabled=True, order="random", weight=2),
            "mod_shoutout": PromoTypeConfig(enabled=True, order="sequential", weight=1),
            "feature_presentation": PromoTypeConfig(enabled=True, order="random", weight=1),
            "viewers_choice": PromoTypeConfig(enabled=True, order="random", weight=1),
        },
    )
    base.update(over)
    return PromoConfig(**base)


def _director(api, shadow, db, cfg=None, *, now=None):
    d = PromoDirector(api_gate=api, db=db, shadow=shadow, config=cfg or _config(),
                      add_delay_sec=0.0, add_max_retries=0)
    if now is not None:
        d._now = lambda: now
    return d


def _promo_items(shadow):
    return [it for it in shadow.items if it.get("is_promo")]


# --- Lead-ins ---------------------------------------------------------------

async def test_feature_presentation_leadin_before_movie():
    shadow = _FakeShadow([_content(10), _content(20, duration=3600)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={"feature_presentation": [_clip("fp1")]})
    d = _director(api, shadow, db)

    await d.on_poll()

    promos = _promo_items(shadow)
    assert len(promos) == 1
    assert promos[0]["promo_type"] == "feature_presentation"
    assert promos[0]["lead_in_for_uid"] == 20
    # Placed immediately before the movie.
    order = [it["uid"] for it in shadow.items]
    assert order.index(promos[0]["uid"]) == order.index(20) - 1


async def test_viewers_choice_leadin_before_paid():
    shadow = _FakeShadow([_content(10), _content(20, is_pay=True, duration=300)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={"viewers_choice": [_clip("vc1")]})
    d = _director(api, shadow, db)

    await d.on_poll()

    promos = _promo_items(shadow)
    assert len(promos) == 1
    assert promos[0]["promo_type"] == "viewers_choice"
    assert promos[0]["lead_in_for_uid"] == 20


async def test_paid_movie_gets_viewers_choice_not_feature():
    # A long (movie-length) paid item is "paid" first -> VC only.
    shadow = _FakeShadow([_content(10), _content(20, is_pay=True, duration=7200)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={"viewers_choice": [_clip("vc1")], "feature_presentation": [_clip("fp1")]})
    d = _director(api, shadow, db)

    await d.on_poll()

    promos = _promo_items(shadow)
    assert len(promos) == 1
    assert promos[0]["promo_type"] == "viewers_choice"


async def test_no_leadin_for_short_free_item():
    shadow = _FakeShadow([_content(10), _content(20, duration=300)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={"feature_presentation": [_clip("fp1")], "viewers_choice": [_clip("vc1")]})
    d = _director(api, shadow, db)

    await d.on_poll()
    assert _promo_items(shadow) == []


async def test_leadin_idempotent_across_polls():
    shadow = _FakeShadow([_content(10), _content(20, duration=3600)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={"feature_presentation": [_clip("fp1")]})
    d = _director(api, shadow, db)

    await d.on_poll()
    await d.on_poll()
    await d.on_poll()
    assert len(_promo_items(shadow)) == 1


# --- General cadence --------------------------------------------------------

async def test_general_cadence_fires_after_n_items():
    shadow = _FakeShadow([_content(10), _content(20)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={"channel_identity": [_clip("c1")]})
    cfg = _config(types={
        "channel_identity": PromoTypeConfig(enabled=True, order="sequential", weight=1),
        "event": PromoTypeConfig(enabled=False, weight=0),
        "mod_shoutout": PromoTypeConfig(enabled=False, weight=0),
        "feature_presentation": PromoTypeConfig(enabled=False),
        "viewers_choice": PromoTypeConfig(enabled=False),
    })
    d = _director(api, shadow, db, cfg)
    d._content_since_last_general = 4  # threshold reached

    await d.on_poll()
    promos = _promo_items(shadow)
    assert len(promos) == 1
    assert promos[0]["promo_type"] == "channel_identity"
    assert promos[0]["lead_in_for_uid"] is None
    assert d._content_since_last_general == 0

    # Same slot on the next poll must not double-insert.
    await d.on_poll()
    assert len(_promo_items(shadow)) == 1


async def test_general_cadence_fires_after_m_minutes():
    shadow = _FakeShadow([_content(10), _content(20)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={"channel_identity": [_clip("c1")]})
    cfg = _config(general=GeneralPromoConfig(every_n_items=99, every_m_minutes=20.0, no_repeat=True),
                  types={
                      "channel_identity": PromoTypeConfig(enabled=True, order="sequential", weight=1),
                      "event": PromoTypeConfig(enabled=False, weight=0),
                      "mod_shoutout": PromoTypeConfig(enabled=False, weight=0),
                      "feature_presentation": PromoTypeConfig(enabled=False),
                      "viewers_choice": PromoTypeConfig(enabled=False),
                  })
    d = _director(api, shadow, db, cfg, now=datetime.now(UTC))
    # Item cadence not reached, but last general was long ago.
    d._content_since_last_general = 0
    d._last_general_at = datetime.now(UTC) - timedelta(minutes=25)

    await d.on_poll()
    assert len(_promo_items(shadow)) == 1


async def test_stacked_order_general_then_leadin_then_content():
    shadow = _FakeShadow([_content(10), _content(20, duration=3600)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={
        "channel_identity": [_clip("c1")],
        "feature_presentation": [_clip("fp1")],
    })
    cfg = _config(types={
        "channel_identity": PromoTypeConfig(enabled=True, order="sequential", weight=1),
        "event": PromoTypeConfig(enabled=False, weight=0),
        "mod_shoutout": PromoTypeConfig(enabled=False, weight=0),
        "feature_presentation": PromoTypeConfig(enabled=True, order="random", weight=1),
        "viewers_choice": PromoTypeConfig(enabled=False),
    })
    d = _director(api, shadow, db, cfg)
    d._content_since_last_general = 4

    await d.on_poll()

    order = [it for it in shadow.items]
    uids = [it["uid"] for it in order]
    # Expect [10, general, FP, 20].
    assert uids[0] == 10
    assert uids[-1] == 20
    general = next(it for it in order if it.get("promo_type") == "channel_identity")
    fp = next(it for it in order if it.get("promo_type") == "feature_presentation")
    assert uids.index(general["uid"]) < uids.index(fp["uid"]) < uids.index(20)


# --- Selection --------------------------------------------------------------

def test_select_clip_sequential_rotates_and_resumes():
    api = _FakeApiGate()
    shadow = _FakeShadow([], now_playing=None)
    db = _FakeDb()
    cfg = _config(types={
        "mod_shoutout": PromoTypeConfig(enabled=True, order="sequential", weight=1),
        "channel_identity": PromoTypeConfig(enabled=True),
        "event": PromoTypeConfig(enabled=True),
        "feature_presentation": PromoTypeConfig(enabled=True),
        "viewers_choice": PromoTypeConfig(enabled=True),
    })
    d = _director(api, shadow, db, cfg)
    pool = [_clip("a"), _clip("b"), _clip("c")]
    picks = [d._select_clip("mod_shoutout", pool)["media_id"] for _ in range(4)]
    assert picks == ["a", "b", "c", "a"]


def test_select_clip_no_repeat_random():
    api = _FakeApiGate()
    shadow = _FakeShadow([], now_playing=None)
    db = _FakeDb()
    d = _director(api, shadow, db)
    pool = [_clip("a"), _clip("b")]
    last = None
    for _ in range(20):
        mid = d._select_clip("channel_identity", pool)["media_id"]
        assert mid != last
        last = mid


async def test_weighted_general_type_selection_approximates_weights():
    api = _FakeApiGate()
    shadow = _FakeShadow([], now_playing=None)
    db = _FakeDb(pools={
        "channel_identity": [_clip("c1")],
        "event": [_clip("e1")],
        "mod_shoutout": [_clip("m1")],
    })
    d = _director(api, shadow, db)  # weights 3 / 2 / 1
    counts = {"channel_identity": 0, "event": 0, "mod_shoutout": 0}
    for _ in range(3000):
        t = await d._pick_general_type()
        counts[t] += 1
    # channel_identity (w3) should clearly exceed event (w2) which exceeds mod (w1).
    assert counts["channel_identity"] > counts["event"] > counts["mod_shoutout"]


# --- No-op / cleanup --------------------------------------------------------

async def test_noop_during_immutable_event():
    shadow = _FakeShadow([_content(10), _content(20, duration=3600)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={"feature_presentation": [_clip("fp1")]}, event_lock=True)
    d = _director(api, shadow, db)

    await d.on_poll()
    assert _promo_items(shadow) == []


async def test_disabled_director_noop():
    shadow = _FakeShadow([_content(10), _content(20, duration=3600)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={"feature_presentation": [_clip("fp1")]})
    d = _director(api, shadow, db, _config(enabled=False))

    await d.on_poll()
    assert _promo_items(shadow) == []


async def test_empty_pool_skips_insertion():
    shadow = _FakeShadow([_content(10), _content(20, duration=3600)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={})  # FP pool empty
    d = _director(api, shadow, db)

    await d.on_poll()
    assert _promo_items(shadow) == []


async def test_insert_viewers_choice_hook():
    # Paid item already placed after now-playing; VC must land between them.
    shadow = _FakeShadow([_content(10), _content(20, is_pay=True, duration=300)], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})
    db = _FakeDb(pools={"viewers_choice": [_clip("vc1")]})
    d = _director(api, shadow, db)

    uid = await d.insert_viewers_choice(20)
    assert uid is not None
    promos = _promo_items(shadow)
    assert len(promos) == 1 and promos[0]["lead_in_for_uid"] == 20
    order = [it["uid"] for it in shadow.items]
    assert order == [10, promos[0]["uid"], 20]

    # Idempotent.
    assert await d.insert_viewers_choice(20) is None
    assert len(_promo_items(shadow)) == 1


async def test_remove_lead_in_for_cleanup():
    shadow = _FakeShadow([
        _content(10),
        _content(99, is_promo=True, promo_type="viewers_choice", lead_in_for_uid=20),
        _content(20, is_pay=True),
    ], now_playing={"uid": 10})
    api = _FakeApiGate(now_playing={"uid": 10})

    removed = await remove_lead_in_for(api_gate=api, shadow=shadow, uid=20)
    assert removed == 1
    assert 99 in api.deleted
    assert all(it["uid"] != 99 for it in shadow.items)

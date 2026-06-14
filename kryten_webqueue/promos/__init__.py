"""Promo insertion subsystem.

Curated promo playlists (saved playlists tagged with a ``promo_type``) are
inserted between mutable content by the :class:`PromoDirector`.
"""

# The five recognised promo types. Types 1-3 are "general" promos inserted on a
# cadence between content; types 4-5 are "lead-ins" attached immediately before
# a specific upcoming item (a mutable-playlist movie, or a pay-to-play item).
GENERAL_PROMO_TYPES = ("channel_identity", "event", "mod_shoutout")
LEAD_IN_PROMO_TYPES = ("feature_presentation", "viewers_choice")
PROMO_TYPES = GENERAL_PROMO_TYPES + LEAD_IN_PROMO_TYPES

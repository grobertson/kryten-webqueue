"""Shared MediaCMS helpers for the vendored cmsutils tools and the Hide action.

Per the spec (OQ-6), the admin Hide/Unhide tag write should ultimately share
one MediaCMS edit client with the enrich tools. This module re-exports the
async ``MediaCMSClient`` (used by the Hide UI) so future refactors can route
the enrich tools' read-modify-write tag edits through the same place.

The enrich tools (``enrichtitles``/``enrichmeta``/``enrichtv``) currently use
their own synchronous ``requests``-based update helpers; those remain in their
respective modules to keep re-vendoring mechanical.
"""

from ...catalog.mediacms import MediaCMSClient

__all__ = ["MediaCMSClient"]

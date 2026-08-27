"""Vendored from d:\\Devel\\yt-pipe (youtube_to_mediacms.py), vendored 2026-06-09."""


class RetryableUploadError(Exception):
    """A MediaCMS upload failed transiently (connection broken / IncompleteRead).

    Raised only from the upload stage after an on-the-spot recovery attempt
    fails, so the fetch-queue drain can re-queue the item rather than marking it
    permanently failed. Lives here (not in ``downloader``) so importers can catch
    it without pulling in the heavy ``yt_dlp`` optional dependency.
    """

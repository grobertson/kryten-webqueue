#!/usr/bin/env python3
# VENDORED from d:\Devel\yt-pipe\youtube_to_mediacms.py on 2026-06-09.
# Adapted for in-process use by kryten-webqueue jobs: a headless
# run(params, *, config, progress) entry point is appended at the bottom; the
# original CLI main()/argparse path is retained but unused by the service.
# Keep adapters thin so re-vendoring from upstream stays mechanical.
"""
Media Downloader to MediaCMS Uploader

A script to download videos from any yt-dlp supported site, sanitize filenames,
and upload them to MediaCMS with proper metadata and tags.

Supported sites: YouTube, Vimeo, TikTok, Twitter, Instagram, Twitch, and 1000+ others.

Usage:
    python youtube_to_mediacms.py --url "https://www.youtube.com/watch?v=VIDEO_ID"
                                  --api-url "https://your-mediacms-instance.com/api/v1"
                                  --api-token "your-api-token"
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import yt_dlp
from slugify import slugify


# yt-dlp needs an external JavaScript runtime to solve YouTube's JS challenges.
# Only "deno" is enabled by default; many hosts only have Node installed, which
# triggers a "No supported JavaScript runtime could be found" warning and missing
# formats. Enable both (priority order deno > node) so the highest-priority
# available runtime is used. Applied centrally via a thin YoutubeDL subclass so
# every extraction/download call in this module gets it without editing each
# ydl_opts dict. ``setdefault`` means an explicit per-call value still wins.
#
# yt-dlp expects ``js_runtimes`` as a dict of ``{runtime: {config}}`` (an empty
# dict means default config for that runtime); passing a list raises
# "Invalid js_runtimes format, expected a dict of {runtime: {config}}".
_JS_RUNTIMES = {"deno": {}, "node": {}}


class _YoutubeDLWithJSRuntimes(yt_dlp.YoutubeDL):
    def __init__(self, params=None, *args, **kwargs):  # noqa: D107
        merged = dict(params or {})
        merged.setdefault("js_runtimes", dict(_JS_RUNTIMES))
        super().__init__(merged, *args, **kwargs)


# Route all `yt_dlp.YoutubeDL(...)` calls in this module through the wrapper.
yt_dlp.YoutubeDL = _YoutubeDLWithJSRuntimes



def clean_youtube_url(url: str) -> str:
    """
    Clean YouTube URLs by removing Mix/Radio playlist parameters.
    
    YouTube Mix playlists (RD prefix) are infinite/dynamic and will cause yt-dlp to hang.
    This function strips those parameters while preserving the video ID.
    
    Args:
        url: Original URL
        
    Returns:
        Cleaned URL with problematic playlist parameters removed
    """
    try:
        parsed = urlparse(url)
        
        # Only process YouTube URLs
        if 'youtube.com' not in parsed.netloc and 'youtu.be' not in parsed.netloc:
            return url
        
        query_params = parse_qs(parsed.query)
        
        # Check if this has a video ID and a Mix/Radio playlist
        if 'v' in query_params and 'list' in query_params:
            playlist_id = query_params.get('list', [''])[0]
            
            # RD = Radio/Mix, RDMM = My Mix, RDAMVM = Artist Mix, etc.
            if playlist_id.startswith('RD'):
                # Rebuild URL with just the video ID
                video_id = query_params['v'][0]
                # Preserve other safe parameters like t (timestamp)
                safe_params = [f"v={video_id}"]
                if 't' in query_params:
                    safe_params.append(f"t={query_params['t'][0]}")
                
                new_query = '&'.join(safe_params)
                clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{new_query}"
                
                # Try to print info (may fail on Windows with emoji issues, but that's OK)
                try:
                    print(f"[INFO] Detected YouTube Mix/Radio playlist (infinite) - stripping playlist parameter")
                    print(f"       Original: {url}")
                    print(f"       Cleaned:  {clean_url}")
                except Exception:
                    pass  # Printing failed, but we still return the cleaned URL
                
                return clean_url
        
        # Check for start_radio parameter (also indicates Mix playlist)
        if 'start_radio' in query_params and 'v' in query_params:
            video_id = query_params['v'][0]
            safe_params = [f"v={video_id}"]
            if 't' in query_params:
                safe_params.append(f"t={query_params['t'][0]}")
            
            new_query = '&'.join(safe_params)
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{new_query}"
            
            try:
                print(f"[INFO] Detected YouTube Radio start parameter - stripping playlist parameters")
                print(f"       Original: {url}")
                print(f"       Cleaned:  {clean_url}")
            except Exception:
                pass
            
            return clean_url
            
    except Exception:
        pass  # If parsing fails, return original URL
    
    return url


class MediaDownloaderToMediaCMS:
    """Main class to handle video downloading from any yt-dlp supported site and MediaCMS uploading."""
    
    def __init__(self, api_url: str, api_token: str, download_dir: Optional[str] = None, cookies_file: Optional[str] = None):
        """
        Initialize the media downloader to MediaCMS uploader.
        
        Args:
            api_url: Base URL for MediaCMS API (e.g., https://mediacms.example.com/api/v1)
            api_token: Authentication token for MediaCMS API
            download_dir: Directory to download videos to (defaults to ./tmp directory)
            cookies_file: Path to cookies file for authentication (optional)
        """
        self.api_url = api_url.rstrip('/')
        self.api_token = api_token
        self.cookies_file = cookies_file
        
        # Use local tmp directory by default instead of system temp
        if download_dir:
            self.download_dir = Path(download_dir)
        else:
            # Create tmp directory in current working directory
            self.download_dir = Path.cwd() / "tmp"
        
        # Ensure download directory exists
        self.download_dir.mkdir(exist_ok=True)
        
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Token {api_token}',
            'Content-Type': 'application/json'
        })
        
        # Track tags that don't exist in MediaCMS across all uploads
        # Maps tag name -> count of times it was requested but missing
        self.missing_tags_report: Dict[str, int] = {}
        
        # Track skipped duplicates for reporting
        self.skipped_duplicates: List[Dict] = []
        
        # Track metadata-enriched duplicates for reporting
        self.enriched_media: List[Dict] = []

    def _retry_request(self, request_func, max_retries: int = 3, initial_delay: float = 1.0, 
                       backoff_factor: float = 2.0, operation_name: str = "request") -> Optional[requests.Response]:
        """
        Retry a request function with exponential backoff.
        
        Args:
            request_func: Function that returns a requests.Response
            max_retries: Maximum number of retry attempts
            initial_delay: Initial delay in seconds between retries
            backoff_factor: Multiplier for delay after each retry
            operation_name: Description of operation for logging
            
        Returns:
            Response object or None on failure
        """
        last_exception = None
        delay = initial_delay
        
        for attempt in range(max_retries):
            try:
                response = request_func()
                if response.status_code < 500:  # Don't retry client errors (4xx)
                    return response
                # Server error (5xx) - will retry
                last_exception = Exception(f"Server error: {response.status_code}")
            except (requests.exceptions.ConnectionError, 
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as e:
                last_exception = e
            except Exception as e:
                # For other exceptions, don't retry
                print(f"[DEBUG] Non-retryable error in {operation_name}: {e}")
                return None
            
            if attempt < max_retries - 1:
                print(f"[DEBUG] {operation_name} failed (attempt {attempt + 1}/{max_retries}), retrying in {delay:.1f}s...")
                time.sleep(delay)
                delay *= backoff_factor
            else:
                print(f"[DEBUG] {operation_name} failed after {max_retries} attempts: {last_exception}")
        
        return None

    def normalize_tag(self, tag: str) -> str:
        """
        Normalize a tag the same way MediaCMS does for matching.
        MediaCMS removes spaces, hyphens, and converts to lowercase.
        
        Args:
            tag: Tag string to normalize
            
        Returns:
            Normalized tag string
        """
        if not tag:
            return ""
        # Remove spaces and hyphens, convert to lowercase
        # This matches MediaCMS tag normalization behavior
        normalized = tag.replace(' ', '').replace('-', '').lower()
        return normalized

    def is_valid_tag(self, tag: str) -> bool:
        """
        Check if a tag is valid and should be used.
        Filters out season/episode patterns like S01E14 which aren't valid MediaCMS tags.
        
        Args:
            tag: Tag string to validate
            
        Returns:
            True if tag is valid, False otherwise
        """
        if not tag or not isinstance(tag, str):
            return False
        
        # Filter out season/episode patterns: S01E14, s01e14, etc.
        if re.match(r'^[Ss]\d{1,2}[Ee]\d{1,2}$', tag.strip()):
            return False
        
        return True

    def extract_source_video_id(self, url: str) -> Optional[str]:
        """
        Extract a unique video ID from a source URL for duplicate detection.
        
        Args:
            url: Source video URL
            
        Returns:
            Video ID string, or None if not extractable
        """
        try:
            parsed = urlparse(url)
            
            # YouTube: ?v=VIDEO_ID
            if 'youtube.com' in parsed.netloc or 'youtu.be' in parsed.netloc:
                if 'youtu.be' in parsed.netloc:
                    return parsed.path.strip('/')
                params = parse_qs(parsed.query)
                if 'v' in params:
                    return params['v'][0]
            
            # Tubi: /movies/560057/... or /tv-shows/554628/...
            if 'tubitv.com' in parsed.netloc:
                match = re.search(r'/(?:movies|tv-shows|video)/(\d+)', parsed.path)
                if match:
                    return match.group(1)
            
            # Generic fallback: extract last numeric ID or path segment
            # Works for many sites (Vimeo, etc.)
            match = re.search(r'/(\d{4,})', parsed.path)
            if match:
                return match.group(1)
            
        except Exception:
            pass
        
        return None

    def check_media_already_imported(self, url: str, title: Optional[str] = None) -> Optional[Dict]:
        """
        Check if media from this URL has already been imported to MediaCMS.
        
        Searches MediaCMS for the original source URL in media descriptions
        (every upload includes 'Original URL: ...' in the description).
        Falls back to title matching if URL search fails.
        
        Args:
            url: Original source URL to check
            title: Optional video title to use as fallback search
            
        Returns:
            Dict with existing media info if found, None if not imported yet
        """
        try:
            # Strategy 1: Search by video ID (most reliable, handles URL variations)
            video_id = self.extract_source_video_id(url)
            if video_id:
                match = self._search_mediacms_for_url(video_id, url)
                if match:
                    return match
            
            # Strategy 2: Search by domain + path keywords
            parsed = urlparse(url)
            # Use the last meaningful path segment as a search term
            path_parts = [p for p in parsed.path.strip('/').split('/') if p]
            if path_parts:
                search_term = path_parts[-1].replace('-', ' ').replace('_', ' ')
                if len(search_term) > 3:  # Skip very short segments
                    match = self._search_mediacms_for_url(search_term, url)
                    if match:
                        return match
            
            # Strategy 3: Search by title (less reliable but catches reuploads)
            if title and len(title) > 5:
                match = self._search_mediacms_by_title(title)
                if match:
                    return match
            
        except Exception as e:
            print(f"[WARN] Error checking for duplicates: {e}")
        
        return None

    def _search_mediacms_for_url(self, search_term: str, original_url: str) -> Optional[Dict]:
        """
        Search MediaCMS and check if any results contain the original URL in their description.
        
        Args:
            search_term: Term to search for (video ID, slug, etc.)
            original_url: The exact original URL to verify in descriptions
            
        Returns:
            Dict with media info if found, None otherwise
        """
        try:
            response = self.session.get(
                f"{self.api_url}/search",
                params={'q': search_term},
                timeout=15
            )
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            results = data.get('results', []) if isinstance(data, dict) else data
            
            for item in results:
                description = item.get('description', '') or ''
                # Check if the original URL appears in the description
                if original_url in description:
                    return {
                        'friendly_token': item.get('friendly_token', ''),
                        'title': item.get('title', 'Unknown'),
                        'url': item.get('url', item.get('api_url', '')),
                    }
                # Also check URL without protocol (http vs https variations)
                url_without_proto = re.sub(r'^https?://', '', original_url)
                if url_without_proto in description:
                    return {
                        'friendly_token': item.get('friendly_token', ''),
                        'title': item.get('title', 'Unknown'),
                        'url': item.get('url', item.get('api_url', '')),
                    }
        except Exception as e:
            print(f"[DEBUG] Search error for term '{search_term}': {e}")
        
        return None

    def _search_mediacms_by_title(self, title: str) -> Optional[Dict]:
        """
        Search MediaCMS by exact title match.
        
        Args:
            title: Video title to search for
            
        Returns:
            Dict with media info if exact title match found, None otherwise
        """
        try:
            # Use first few significant words to search
            search_words = title.split()[:5]
            search_term = ' '.join(search_words)
            
            response = self.session.get(
                f"{self.api_url}/search",
                params={'q': search_term},
                timeout=15
            )
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            results = data.get('results', []) if isinstance(data, dict) else data
            
            for item in results:
                existing_title = item.get('title', '') or ''
                # Exact (case-insensitive) title match
                if existing_title.strip().lower() == title.strip().lower():
                    return {
                        'friendly_token': item.get('friendly_token', ''),
                        'title': existing_title,
                        'url': item.get('url', item.get('api_url', '')),
                    }
        except Exception as e:
            print(f"[DEBUG] Title search error: {e}")
        
        return None

    # ── Source discovery: find all media from a given origin ─────────────

    # Common source domain aliases
    SOURCE_DOMAIN_MAP = {
        'tubi':     'tubitv.com',
        'tubitv':   'tubitv.com',
        'youtube':  'youtube.com',
        'yt':       'youtube.com',
        'vimeo':    'vimeo.com',
        'tiktok':   'tiktok.com',
        'twitch':   'twitch.tv',
        'twitter':  'twitter.com',
        'x':        'x.com',
        'instagram':'instagram.com',
        'dailymotion': 'dailymotion.com',
    }

    def resolve_source_domain(self, source: str) -> str:
        """
        Resolve a user-friendly source name to a domain string.
        
        Args:
            source: Source name (e.g. 'tubi') or domain (e.g. 'tubitv.com')
            
        Returns:
            Domain string to search for in descriptions
        """
        return self.SOURCE_DOMAIN_MAP.get(source.lower().strip(), source.lower().strip())

    def find_media_by_source(self, source: str, verbose: bool = True) -> List[Dict]:
        """
        Find all media in MediaCMS that were originally imported from a given source.
        
        Paginates through the full media library and checks each item's description
        for 'Original URL: ...' lines containing the source domain. Also catches items
        where the source domain appears anywhere in the description (covers older imports
        that may not have the structured Original URL line).
        
        Args:
            source: Source name or domain (e.g. 'tubi', 'youtube', 'tubitv.com')
            verbose: Whether to print progress
            
        Returns:
            List of dicts with keys: friendly_token, title, original_url, description_length, has_cast
        """
        domain = self.resolve_source_domain(source)
        if verbose:
            print(f"\n🔍 Scanning MediaCMS library for media from '{source}' (domain: {domain})...")
        
        found_media = []
        page = 1
        total_scanned = 0
        
        while True:
            try:
                response = self.session.get(
                    f"{self.api_url}/media",
                    params={'page': page},
                    timeout=30
                )
                
                if response.status_code != 200:
                    if verbose:
                        print(f"  [DEBUG] Page {page} returned status {response.status_code} — stopping")
                    break
                
                data = response.json()
                results = data.get('results', [])
                total_count = data.get('count', '?')
                
                if not results:
                    break
                
                for item in results:
                    total_scanned += 1
                    description = item.get('description', '') or ''
                    
                    # Check if this media came from the target source
                    if domain not in description.lower():
                        continue
                    
                    # Extract the Original URL from the description
                    original_url = ''
                    for line in description.split('\n'):
                        if line.strip().startswith('Original URL:'):
                            original_url = line.strip().replace('Original URL:', '').strip()
                            break
                    
                    # Verify the domain is actually in the original URL
                    # (avoid false positives from domain appearing in other description text)
                    if original_url and domain not in original_url.lower():
                        # Domain was in description but not in Original URL — 
                        # still include it but note it's less certain
                        pass
                    
                    found_media.append({
                        'friendly_token': item.get('friendly_token', ''),
                        'title': item.get('title', 'Unknown'),
                        'original_url': original_url,
                        'description_length': len(description),
                        'has_cast': 'Cast:' in description or 'Cast & Crew:' in description,
                        'has_genres': 'Genres:' in description,
                        'has_original_url': bool(original_url),
                    })
                
                if verbose:
                    print(f"  📄 Page {page}: scanned {len(results)} items "
                          f"({total_scanned}/{total_count} total), "
                          f"found {len(found_media)} from {source} so far")
                
                # Check if there's a next page
                next_url = data.get('next')
                if not next_url:
                    break
                
                page += 1
                
            except Exception as e:
                if verbose:
                    print(f"  ⚠️  Error on page {page}: {e}")
                break
        
        if verbose:
            print(f"\n✅ Scan complete: found {len(found_media)} media items from '{source}' "
                  f"(scanned {total_scanned} total)")
        
        return found_media

    def print_source_media_report(self, source: str, media_list: List[Dict]) -> None:
        """
        Print a detailed report of media found from a given source.
        
        Args:
            source: Source name for display
            media_list: List from find_media_by_source()
        """
        domain = self.resolve_source_domain(source)
        
        if not media_list:
            print(f"\n📭 No media found from '{source}' in MediaCMS")
            return
        
        # Classify by metadata quality
        with_cast = [m for m in media_list if m['has_cast']]
        without_cast = [m for m in media_list if not m['has_cast']]
        with_genres = [m for m in media_list if m['has_genres']]
        without_original_url = [m for m in media_list if not m['has_original_url']]
        
        print(f"\n{'='*70}")
        print(f"📊 MEDIA FROM '{source.upper()}' — SOURCE REPORT")
        print(f"{'='*70}")
        print(f"Total items from {source}: {len(media_list)}")
        print(f"  With Cast & Crew metadata:    {len(with_cast)}")
        print(f"  Missing Cast & Crew metadata: {len(without_cast)}")
        print(f"  With Genres:                  {len(with_genres)}")
        print(f"  Missing Original URL line:    {len(without_original_url)}")
        
        # List items needing enrichment
        needs_enrichment = [m for m in media_list 
                          if not m['has_cast'] or not m['has_genres'] or not m['has_original_url']]
        
        if needs_enrichment:
            print(f"\n🔧 {len(needs_enrichment)} item(s) may benefit from metadata enrichment:")
            for i, m in enumerate(needs_enrichment, 1):
                missing = []
                if not m['has_cast']:
                    missing.append('cast')
                if not m['has_genres']:
                    missing.append('genres')
                if not m['has_original_url']:
                    missing.append('original URL')
                url_display = m['original_url'] or '(no URL recorded)'
                print(f"  {i:3}. {m['title']}")
                print(f"       ID: {m['friendly_token']}  |  Missing: {', '.join(missing)}")
                print(f"       URL: {url_display}")
        
        up_to_date = [m for m in media_list 
                     if m['has_cast'] and m['has_genres'] and m['has_original_url']]
        if up_to_date:
            print(f"\n✅ {len(up_to_date)} item(s) already have rich metadata:")
            for m in up_to_date:
                print(f"  • {m['title']}  (ID: {m['friendly_token']})")
        
        print(f"{'='*70}")
        
        # Output URLs file
        items_with_urls = [m for m in media_list if m['original_url']]
        if items_with_urls:
            output_file = f"{source.lower()}_media_urls.txt"
            try:
                with open(output_file, 'w', encoding='utf-8') as f:
                    for m in items_with_urls:
                        f.write(m['original_url'] + '\n')
                print(f"\n📝 Wrote {len(items_with_urls)} source URLs to: {output_file}")
                print(f"   Use with --input-file to batch-enrich: "
                      f"python youtube_to_mediacms.py --input-file {output_file} --api-url ... --api-token ...")
            except Exception as e:
                print(f"⚠️  Could not write URL file: {e}")

    def enrich_media_by_source(self, source: str, delay_seconds: int = 2, force: bool = False) -> List[Dict]:
        """
        Find all media from a given source and enrich any with outdated metadata.
        
        This is the all-in-one operation: find → assess → enrich, without needing
        to download any video files.
        
        Args:
            source: Source name or domain (e.g. 'tubi', 'youtube')
            delay_seconds: Seconds to wait between enrichment API calls
            force: If True, process ALL items regardless of apparent quality
            
        Returns:
            List of enrichment results
        """
        # Step 1: Find all media from this source
        media_list = self.find_media_by_source(source)
        
        if not media_list:
            print(f"\n📭 No media from '{source}' found — nothing to enrich")
            return []
        
        # Step 2: Identify items to process
        if force:
            # Force mode: process everything that has a source URL
            enrichable = [m for m in media_list if m['original_url']]
            no_url = len(media_list) - len(enrichable)
            
            print(f"\n📊 Enrichment plan for '{source}' (FORCE mode — all items):")
            print(f"  Total from {source}:         {len(media_list)}")
            print(f"  Will process (have URL):     {len(enrichable)}")
            if no_url > 0:
                print(f"  ⚠️  Skipping {no_url} items (no Original URL recorded)")
        else:
            needs_work = [m for m in media_list 
                         if not m['has_cast'] or not m['has_genres'] or not m['has_original_url']
                         or m['description_length'] < 500]
            
            enrichable = [m for m in needs_work if m['original_url']]
            
            already_rich = len(media_list) - len(needs_work)
            no_url = len(needs_work) - len(enrichable)
            
            print(f"\n📊 Enrichment plan for '{source}':")
            print(f"  Total from {source}:         {len(media_list)}")
            print(f"  Already rich metadata:       {already_rich}")
            print(f"  Need enrichment:             {len(needs_work)}")
            print(f"  Enrichable (have source URL):{len(enrichable)}")
            if no_url > 0:
                print(f"  ⚠️  Skipping {no_url} items (no Original URL recorded)")
        
        if not enrichable:
            print(f"\n✅ Nothing to enrich — all items are either up to date or missing source URLs")
            self.print_source_media_report(source, media_list)
            return []
        
        print(f"\n🔄 Starting enrichment of {len(enrichable)} items...\n")
        
        # Step 3: Enrich each item
        results = []
        enriched_count = 0
        skipped_count = 0
        error_count = 0
        
        for i, item in enumerate(enrichable, 1):
            print(f"[{i}/{len(enrichable)}] {item['title']}")
            print(f"  Source: {item['original_url']}")
            
            try:
                existing_match = {
                    'friendly_token': item['friendly_token'],
                    'title': item['title'],
                    'url': '',
                }
                
                result = self.enrich_existing_media(
                    url=item['original_url'],
                    existing_match=existing_match,
                )
                
                if result.get('enriched'):
                    enriched_count += 1
                    print(f"  ✨ Enriched!")
                else:
                    skipped_count += 1
                    print(f"  ⏭️  Already up to date")
                
                result['source_url'] = item['original_url']
                results.append(result)
                
            except Exception as e:
                error_count += 1
                print(f"  ❌ Error: {e}")
                results.append({
                    'friendly_token': item['friendly_token'],
                    'title': item['title'],
                    'enriched': False,
                    'error': str(e),
                    'source_url': item['original_url'],
                })
            
            # Rate limit
            if i < len(enrichable) and delay_seconds > 0:
                time.sleep(delay_seconds)
        
        # Print missing tags report from any enrichment that checked tags
        self.print_missing_tags_report()
        
        # Summary
        print(f"\n{'='*60}")
        print(f"ENRICHMENT COMPLETE — {source.upper()}")
        print(f"{'='*60}")
        print(f"Total processed:  {len(enrichable)}")
        print(f"Enriched:         {enriched_count}")
        print(f"Already current:  {skipped_count}")
        print(f"Errors:           {error_count}")
        
        if enriched_count > 0:
            print(f"\n✨ Enriched items:")
            for r in results:
                if r.get('enriched'):
                    print(f"  • {r.get('title')}  (ID: {r.get('friendly_token')})")
                    for imp in r.get('improvements', []):
                        print(f"    → {imp}")
        
        print(f"{'='*60}")
        
        return results

    # ── Metadata quality assessment & enrichment ─────────────────────────

    # Sections that indicate rich, script-generated descriptions
    QUALITY_MARKERS = [
        'Cast & Crew:',
        'Director(s):',
        'Cast:',
        'Genres:',
        'Content Rating:',
        'Release Year:',
        'Video Information:',
        'Original Description:',
        'Original URL:',
        'Language:',
        'Additional Tags (not yet in MediaCMS):',
        'Tags:',
    ]

    def fetch_existing_media_details(self, friendly_token: str, max_retries: int = 3) -> Optional[Dict]:
        """
        Fetch full details for an existing media item from MediaCMS.
        
        Uses the SingleMediaSerializer endpoint which returns title, description,
        tags_info, categories_info, and all other media fields.
        
        Args:
            friendly_token: The media's unique identifier in MediaCMS
            max_retries: Maximum number of retry attempts for failed requests
            
        Returns:
            Dict with full media details, or None on failure
        """
        def make_request():
            return self.session.get(
                f"{self.api_url}/media/{friendly_token}",
                timeout=15
            )
        
        response = self._retry_request(
            make_request,
            max_retries=max_retries,
            initial_delay=1.0,
            backoff_factor=2.0,
            operation_name=f"fetch media details for {friendly_token}"
        )
        
        if response and response.status_code == 200:
            return response.json()
        
        return None

    def assess_metadata_quality(self, description: str, tags: List[str] = None) -> Dict:
        """
        Score the quality/richness of a media item's metadata.
        
        Checks for the presence of structured sections that this script generates
        (Cast & Crew, Genres, Content Rating, etc.), description length, and tag count.
        
        Args:
            description: The media description text
            tags: List of tag titles currently applied
            
        Returns:
            Dict with quality metrics:
                score: int — total quality score
                sections_present: List[str] — which quality markers were found
                sections_missing: List[str] — which quality markers are absent
                description_length: int — character count of description
                tag_count: int — number of tags applied
                has_original_url: bool — whether Original URL is recorded
        """
        description = description or ''
        tags = tags or []
        
        sections_present = []
        sections_missing = []
        score = 0
        
        for marker in self.QUALITY_MARKERS:
            if marker in description:
                sections_present.append(marker)
                score += 10
            else:
                sections_missing.append(marker)
        
        # Description length contributes to score (longer = more metadata preserved)
        desc_len = len(description)
        score += min(desc_len // 100, 30)  # up to 30 points for length
        
        # Tags contribute
        tag_count = len(tags)
        score += min(tag_count * 3, 15)  # up to 15 points for tags
        
        has_original_url = 'Original URL:' in description
        
        return {
            'score': score,
            'sections_present': sections_present,
            'sections_missing': sections_missing,
            'description_length': desc_len,
            'tag_count': tag_count,
            'has_original_url': has_original_url,
        }

    def update_media_metadata(self, friendly_token: str, title: str = None, 
                              description: str = None, max_retries: int = 3) -> bool:
        """
        Update an existing media item's title and/or description via PUT.
        
        Args:
            friendly_token: MediaCMS media identifier
            title: New title (or None to leave unchanged)
            description: New description (or None to leave unchanged)
            max_retries: Maximum number of retry attempts for failed requests
            
        Returns:
            True if update succeeded
        """
        data = {}
        if title is not None:
            data['title'] = title
        if description is not None:
            data['description'] = description
        
        if not data:
            return True  # nothing to update
        
        # PUT requires multipart form (same parser as upload)
        upload_session = requests.Session()
        upload_session.headers.update({
            'Authorization': f'Token {self.api_token}'
        })
        
        def make_request():
            return upload_session.put(
                f"{self.api_url}/media/{friendly_token}",
                data=data,
                timeout=30
            )
        
        response = self._retry_request(
            make_request,
            max_retries=max_retries,
            initial_delay=1.0,
            backoff_factor=2.0,
            operation_name=f"update metadata for {friendly_token}"
        )
        
        if response and response.status_code in (200, 201):
            print(f"  ✅ Metadata updated successfully")
            return True
        elif response:
            print(f"  ⚠️  Metadata update failed (status {response.status_code}): {response.text[:200]}")
        else:
            print(f"  ⚠️  Metadata update failed after retries")
        
        return False

    def enrich_existing_media(self, url: str, existing_match: Dict,
                              playlist_info: Optional[Dict] = None,
                              playlist_index: Optional[int] = None) -> Dict:
        """
        Check an already-imported media item's metadata quality and update it
        if the script can now produce richer metadata than what's stored.
        
        This allows previously imported items (e.g. before Tubi cast scraping
        was added) to be brought up to the same standard as fresh imports
        without re-downloading the video file.
        
        Args:
            url: Original source URL
            existing_match: Dict with 'friendly_token', 'title', 'url' from duplicate check
            playlist_info: Optional playlist context
            playlist_index: Optional position in playlist
            
        Returns:
            Dict describing what happened:
                already_exists: True
                enriched: bool — whether metadata was updated
                friendly_token, title: identifiers
                improvements: list of what was improved (if any)
        """
        friendly_token = existing_match.get('friendly_token', '')
        existing_title = existing_match.get('title', 'Unknown')
        
        result = {
            'already_exists': True,
            'enriched': False,
            'friendly_token': friendly_token,
            'title': existing_title,
            'improvements': [],
        }
        
        # ── 1. Fetch full existing details from MediaCMS ────────────────
        print(f"  📋 Fetching existing metadata for '{existing_title}'...")
        existing_details = self.fetch_existing_media_details(friendly_token)
        if not existing_details:
            print(f"  ⚠️  Could not fetch existing details — skipping enrichment")
            return result
        
        existing_desc = existing_details.get('description', '') or ''
        existing_tags_info = existing_details.get('tags_info', '') or ''
        # tags_info comes back as comma-separated string
        existing_tag_list = [t.strip() for t in existing_tags_info.split(',') if t.strip()] \
            if isinstance(existing_tags_info, str) else []
        
        existing_quality = self.assess_metadata_quality(existing_desc, existing_tag_list)
        print(f"  📊 Existing quality score: {existing_quality['score']}  "
              f"(description: {existing_quality['description_length']} chars, "
              f"{len(existing_quality['sections_present'])} sections, "
              f"{existing_quality['tag_count']} tags)")
        
        # ── 2. Extract fresh metadata (no download needed) ──────────────
        print(f"  🔄 Extracting fresh metadata from source...")
        try:
            fresh_info = self.extract_video_info(url)
        except Exception as e:
            print(f"  ⚠️  Could not extract fresh metadata: {e}")
            return result
        
        # ── 3. Generate what we WOULD produce for a new import ──────────
        fresh_tags = self.generate_tags(fresh_info, playlist_info, url)
        fresh_existing_tags, fresh_missing_tags = self.check_tags_availability(fresh_tags)
        
        fresh_title = self.clean_title(
            fresh_info.get('title', 'Untitled Video'), fresh_info, url, playlist_info
        )
        fresh_desc = self.create_description(
            fresh_info, url, playlist_info, playlist_index,
            tags=fresh_tags, missing_tags=fresh_missing_tags
        )
        
        fresh_quality = self.assess_metadata_quality(fresh_desc, fresh_tags)
        print(f"  📊 Fresh quality score:    {fresh_quality['score']}  "
              f"(description: {fresh_quality['description_length']} chars, "
              f"{len(fresh_quality['sections_present'])} sections, "
              f"{len(fresh_tags)} tags)")
        
        # ── 4. Decide what to update ────────────────────────────────────
        improvements = []
        update_desc = False
        update_title = False
        add_tags = []
        
        # Check description quality
        new_sections = set(fresh_quality['sections_present']) - set(existing_quality['sections_present'])
        if new_sections:
            improvements.append(f"adds {len(new_sections)} new section(s): {', '.join(sorted(new_sections))}")
            update_desc = True
        
        if fresh_quality['description_length'] > existing_quality['description_length'] * 1.2:
            # Fresh description is >20% longer — likely contains more metadata
            improvements.append(
                f"description expanded ({existing_quality['description_length']} → "
                f"{fresh_quality['description_length']} chars)"
            )
            update_desc = True
        
        if not existing_quality['has_original_url'] and fresh_quality['has_original_url']:
            improvements.append("adds Original URL for future duplicate detection")
            update_desc = True
        
        # Check if title could be improved (cleaned)
        if fresh_title != existing_title and len(fresh_title) >= len(existing_title) * 0.8:
            # Only update title if new one is reasonably similar length (not truncated)
            improvements.append(f"title cleaned: '{existing_title}' → '{fresh_title}'")
            update_title = True
        
        # Check for new tags to add
        existing_tag_lower = {t.lower() for t in existing_tag_list}
        new_tags_to_add = [t for t in fresh_existing_tags if t.lower() not in existing_tag_lower]
        if new_tags_to_add:
            improvements.append(f"{len(new_tags_to_add)} new tag(s) to add: {', '.join(new_tags_to_add)}")
            add_tags = new_tags_to_add
        
        # ── 5. Apply updates if any improvements found ──────────────────
        if not improvements:
            print(f"  ✅ Metadata is already up to date — no enrichment needed")
            return result
        
        print(f"  🔧 Found {len(improvements)} improvement(s):")
        for imp in improvements:
            print(f"     • {imp}")
        
        result['improvements'] = improvements
        
        # Update description and/or title
        if update_desc or update_title:
            success = self.update_media_metadata(
                friendly_token,
                title=fresh_title if update_title else None,
                description=fresh_desc if update_desc else None
            )
            if success:
                result['enriched'] = True
                if update_title:
                    result['title'] = fresh_title
        
        # Add new tags
        if add_tags:
            tag_success = self.add_tags_to_media(friendly_token, add_tags)
            if tag_success:
                result['enriched'] = True
        
        if result['enriched']:
            print(f"  🎉 Media enriched successfully!")
            self.enriched_media.append({
                'url': url,
                'title': result['title'],
                'friendly_token': friendly_token,
                'improvements': improvements,
            })
        
        return result

    def sanitize_filename(self, filename: str) -> str:
        """
        Sanitize filename by removing/replacing invalid characters.
        
        Args:
            filename: Original filename
            
        Returns:
            Sanitized filename safe for filesystem
        """
        # Remove file extension if present
        name, ext = os.path.splitext(filename)
        
        # os.path.splitext misfires on titles containing dots (e.g. "L.A. Wars"),
        # treating everything after the first dot as the "extension". That leaves
        # unsafe characters (spaces, |, parentheses) unslugified, so the downloaded
        # file on disk no longer matches the expected name and is never found.
        # Only honor a genuine, short, alphanumeric extension; otherwise slugify
        # the whole string.
        if not re.match(r'^\.[A-Za-z0-9]{1,5}$', ext):
            name, ext = filename, ''
        
        # Use slugify to create a safe filename
        safe_name = slugify(name, max_length=200)
        
        # If slugify resulted in empty string, use a default name
        if not safe_name:
            safe_name = "video"
            
        return safe_name + ext

    def is_playlist_url(self, url: str) -> bool:
        """
        Check if the URL is a playlist URL using URL parsing (fast) and yt-dlp (fallback).
        
        Args:
            url: URL to check
            
        Returns:
            True if playlist URL, False otherwise
        """
        try:
            parsed = urlparse(url)
            query_params = parse_qs(parsed.query)
            
            # Check for YouTube URLs with both video ID and playlist
            # If 'v' parameter exists along with 'list', treat as single video
            # (User can use playlist-only URL if they want the playlist)
            if 'v' in query_params and 'list' in query_params:
                print("📝 URL contains both video ID and playlist - treating as single video")
                print("   To download playlist, use: https://www.youtube.com/playlist?list=PLAYLIST_ID")
                return False
            
            # Check for YouTube playlist-only URLs
            if 'list' in query_params and 'v' not in query_params:
                # Skip YouTube Mix/Radio playlists (RD prefix) - they're infinite
                playlist_id = query_params.get('list', [''])[0]
                if playlist_id.startswith('RD'):
                    print("⚠️  YouTube Mix/Radio playlists are dynamically generated and cannot be fully downloaded")
                    print("   Treating as single video instead")
                    return False
                return True
            
            # Check for playlist in path (e.g., /playlist)
            if '/playlist' in parsed.path:
                return True
            
            # For non-YouTube URLs, use yt-dlp with timeout
            # But avoid this for YouTube as it can hang on infinite playlists
            if 'youtube.com' not in parsed.netloc and 'youtu.be' not in parsed.netloc:
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': True,
                    'socket_timeout': 10,
                    'playlistend': 5,  # Only check first 5 items
                    'remote_components': ['ejs:github'],  # Enable JS challenge solver
                }
                if self.cookies_file:
                    ydl_opts['cookiefile'] = self.cookies_file
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    return 'entries' in info and len(info.get('entries', [])) > 1
            
            return False
        except:
            return False

    def extract_playlist_info(self, url: str) -> Dict:
        """
        Extract playlist information and video list from any playlist URL.
        
        Args:
            url: Playlist URL from any supported site
            
        Returns:
            Dictionary containing playlist and video information
        """
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,  # Only extract video IDs, not full info
            'socket_timeout': 30,
            'playlistend': 100,  # Limit to first 100 videos to prevent hanging
            'remote_components': ['ejs:github'],  # Enable JS challenge solver
        }
        if self.cookies_file:
            ydl_opts['cookiefile'] = self.cookies_file
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                # Check if we hit the limit
                entries = info.get('entries', [])
                if len(entries) >= 100:
                    print(f"⚠️  Playlist has 100+ videos, limiting to first 100")
                
                return info
        except Exception as e:
            raise Exception(f"Failed to extract playlist info: {str(e)}")

    def is_tubi_url(self, url: str) -> bool:
        """Check if the URL is a Tubi TV URL."""
        return 'tubitv.com' in url.lower()

    def fetch_tubi_extended_metadata(self, url: str) -> Dict:
        """
        Fetch extended metadata from Tubi page that yt-dlp doesn't extract.
        
        Tubi's window.__data JSON contains rich metadata (cast, directors, genres, etc.)
        that the yt-dlp TubiTv extractor doesn't capture. This method scrapes that data.
        
        Args:
            url: Tubi video URL
            
        Returns:
            Dictionary with extended metadata (cast, directors, genres, rating, etc.)
        """
        extended = {}
        
        try:
            print(f"[INFO] Fetching extended Tubi metadata...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }
            
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"[WARN] Failed to fetch Tubi page (status {response.status_code})")
                return extended
            
            webpage = response.text
            
            # Extract the window.__data JSON blob
            data_match = re.search(r'window\.__data\s*=\s*({.+?});\s*(?:window\.|</script>)', webpage, re.DOTALL)
            if not data_match:
                # Try alternative pattern
                data_match = re.search(r'window\.__data\s*=\s*({.+?})\s*;', webpage, re.DOTALL)
            
            if not data_match:
                print(f"[WARN] Could not find window.__data in Tubi page")
                return extended
            
            try:
                # The JSON may use JS syntax, try to parse it
                raw_json = data_match.group(1)
                # Handle some common JS-to-JSON issues
                raw_json = re.sub(r'undefined', 'null', raw_json)
                data = json.loads(raw_json)
            except json.JSONDecodeError:
                # Try with yt-dlp's js_to_json helper
                try:
                    from yt_dlp.utils import js_to_json as ytdl_js_to_json
                    data = json.loads(ytdl_js_to_json(raw_json))
                except Exception:
                    print(f"[WARN] Could not parse Tubi JSON data")
                    return extended
            
            # Extract the video data - navigate to video.byId.{video_id}
            video_by_id = data.get('video', {}).get('byId', {})
            
            # Try to find the video data entry
            video_data = None
            for vid_id, vid_info in video_by_id.items():
                if isinstance(vid_info, dict):
                    video_data = vid_info
                    break
            
            if not video_data:
                print(f"[WARN] Could not find video data in Tubi JSON")
                return extended
            
            # Extract cast/actors
            actors = video_data.get('actors') or video_data.get('cast') or []
            if isinstance(actors, str):
                actors = [a.strip() for a in actors.split(',') if a.strip()]
            elif isinstance(actors, list):
                actors = [str(a).strip() for a in actors if a]
            if actors:
                extended['cast'] = actors
                print(f"[INFO] Found {len(actors)} cast members from Tubi")
            
            # Extract directors
            directors = video_data.get('directors') or video_data.get('director') or []
            if isinstance(directors, str):
                directors = [d.strip() for d in directors.split(',') if d.strip()]
            elif isinstance(directors, list):
                directors = [str(d).strip() for d in directors if d]
            if directors:
                extended['directors'] = directors
                print(f"[INFO] Found {len(directors)} director(s) from Tubi")
            
            # Extract genres/tags
            genres = video_data.get('tags') or video_data.get('genres') or []
            if isinstance(genres, str):
                genres = [g.strip() for g in genres.split(',') if g.strip()]
            elif isinstance(genres, list):
                genres = [str(g).strip() for g in genres if g]
            if genres:
                extended['genres'] = genres
                print(f"[INFO] Found genres from Tubi: {genres}")
            
            # Extract content rating
            rating = video_data.get('rating') or video_data.get('content_rating') or video_data.get('parentalRating')
            if rating:
                extended['content_rating'] = str(rating).strip()
                print(f"[INFO] Found content rating from Tubi: {rating}")
            
            # Extract language
            lang = video_data.get('lang') or video_data.get('language')
            if lang:
                extended['language'] = str(lang).strip()
            
            # Extract full description (may be longer than what yt-dlp returns)
            full_desc = video_data.get('description') or video_data.get('detailed_description')
            if full_desc and isinstance(full_desc, str):
                extended['full_description'] = full_desc.strip()
            
            # Extract series-level info if available
            series_title = video_data.get('series_name') or video_data.get('series_title')
            if series_title:
                extended['series_title'] = str(series_title).strip()
            
            # Extract year if not already available
            year = video_data.get('year') or video_data.get('release_year')
            if year:
                extended['release_year'] = year
                
            # Also try to extract from the top-level 'epg' or other sections
            # Some Tubi pages store extra metadata in different locations
            for section_key in ['contentInfo', 'movieInfo', 'showInfo']:
                section = data.get(section_key, {})
                if isinstance(section, dict):
                    if not extended.get('cast') and section.get('actors'):
                        actors = section['actors']
                        if isinstance(actors, str):
                            actors = [a.strip() for a in actors.split(',') if a.strip()]
                        if actors:
                            extended['cast'] = actors
                    if not extended.get('directors') and section.get('directors'):
                        directors = section['directors']
                        if isinstance(directors, str):
                            directors = [d.strip() for d in directors.split(',') if d.strip()]
                        if directors:
                            extended['directors'] = directors
            
            print(f"[INFO] Extended Tubi metadata: {list(extended.keys())}")
            
        except Exception as e:
            print(f"[WARN] Error fetching extended Tubi metadata: {e}")
        
        return extended

    def enrich_info_with_extended_metadata(self, info: Dict, url: str) -> Dict:
        """
        Enrich yt-dlp info dict with extended metadata from site-specific scrapers.
        
        Currently supports Tubi for cast, directors, genres, etc.
        
        Args:
            info: Video information dictionary from yt-dlp
            url: Original video URL
            
        Returns:
            Enriched info dictionary (modified in place and returned)
        """
        if self.is_tubi_url(url):
            tubi_meta = self.fetch_tubi_extended_metadata(url)
            
            # Merge extended metadata into info dict without overwriting existing values
            if tubi_meta.get('cast') and not info.get('cast'):
                info['cast'] = tubi_meta['cast']
            if tubi_meta.get('directors') and not info.get('directors'):
                info['directors'] = tubi_meta['directors']
            if tubi_meta.get('genres') and not info.get('genres'):
                info['genres'] = tubi_meta['genres']
            if tubi_meta.get('content_rating') and not info.get('content_rating'):
                info['content_rating'] = tubi_meta['content_rating']
            if tubi_meta.get('language') and not info.get('language'):
                info['language'] = tubi_meta['language']
            if tubi_meta.get('release_year') and not info.get('release_year'):
                info['release_year'] = tubi_meta['release_year']
            if tubi_meta.get('series_title') and not info.get('series'):
                info['series'] = tubi_meta['series_title']
            # Use full description if it's longer than what yt-dlp extracted
            if tubi_meta.get('full_description'):
                existing_desc = info.get('description', '')
                if len(tubi_meta['full_description']) > len(existing_desc or ''):
                    info['description'] = tubi_meta['full_description']
        
        return info

    def extract_video_info(self, url: str) -> Dict:
        """
        Extract video information from any supported URL without downloading.
        
        Args:
            url: Video URL from any supported site
            
        Returns:
            Dictionary containing video information
        """
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,  # Prevent playlist enumeration
            'remote_components': ['ejs:github'],  # Enable JS challenge solver
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                # Enrich with extended metadata from site-specific scrapers
                info = self.enrich_info_with_extended_metadata(info, url)
                
                return info
        except Exception as e:
            raise Exception(f"Failed to extract video info: {str(e)}")
    
    def get_all_available_formats(self, url: str, quality: str = 'best') -> List[str]:
        """
        Get all available formats for a URL in order of preference.
        
        Args:
            url: Video URL
            quality: 'best', 'good' (<=1080p), or 'medium' (<=720p)
            
        Returns:
            List of format selectors to try, ordered by preference
        """
        # Map named tiers to height caps; anything else is treated as 'best'
        height_cap = {'medium': 720, 'good': 1080}.get(quality)
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,  # Prevent playlist enumeration
                'remote_components': ['ejs:github'],  # Enable JS challenge solver
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                formats = info.get('formats', [])
                
            if not formats:
                return ['best', 'worst']
            
            format_selectors = []
            
            # Check if we have separate audio and video streams
            has_audio_only = any(f.get('acodec') != 'none' and f.get('vcodec') == 'none' for f in formats)
            has_video_only = any(f.get('vcodec') != 'none' and f.get('acodec') == 'none' for f in formats)
            
            if has_audio_only and has_video_only:
                # Site uses separate audio/video streams - try specific combinations
                video_formats = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') == 'none']
                audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
                
                # Filter by height cap if a quality tier was requested
                if height_cap:
                    preferred_vf = [f for f in video_formats if (f.get('height') or 0) <= height_cap]
                    fallback_vf  = [f for f in video_formats if (f.get('height') or 0) >  height_cap]
                    # Sort preferred ascending so we pick the best within the cap first
                    preferred_vf.sort(key=lambda x: (x.get('height', 0), x.get('tbr', 0)), reverse=True)
                    fallback_vf.sort(key=lambda x:  (x.get('height', 0), x.get('tbr', 0)), reverse=False)
                    video_formats = preferred_vf + fallback_vf
                else:
                    # Sort video formats by quality (height, then bitrate) — best first
                    video_formats.sort(key=lambda x: (x.get('height', 0), x.get('tbr', 0)), reverse=True)
                
                # Try combinations of video + audio
                best_audio = audio_formats[0]['format_id'] if audio_formats else 'bestaudio'
                
                for vf in video_formats:
                    vid_id = vf['format_id']
                    format_selectors.append(f"{vid_id}+{best_audio}")
                
                # Fallback combinations
                if height_cap:
                    format_selectors.extend([
                        f'best[height<={height_cap}]+bestaudio',
                        f'best[height<={height_cap}]',
                        'best[height<=1080]+bestaudio',
                        'best+bestaudio',
                        'worst+bestaudio',
                    ])
                else:
                    format_selectors.extend([
                        'best+bestaudio',
                        'best[height<=1080]+bestaudio',
                        'best[height<=720]+bestaudio',
                        'worst+bestaudio',
                    ])
            
            # Add individual format IDs as fallbacks
            # Sort formats by preference (combined streams first, then by quality)
            combined_formats = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') != 'none']
            video_only_formats = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') == 'none']
            audio_only_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
            
            # Sort by quality — respect height cap if set
            if height_cap:
                preferred_cf = [f for f in combined_formats if (f.get('height') or 0) <= height_cap]
                fallback_cf  = [f for f in combined_formats if (f.get('height') or 0) >  height_cap]
                preferred_vof = [f for f in video_only_formats if (f.get('height') or 0) <= height_cap]
                fallback_vof  = [f for f in video_only_formats if (f.get('height') or 0) >  height_cap]
                for lst in (preferred_cf, fallback_cf, preferred_vof, fallback_vof):
                    lst.sort(key=lambda x: (x.get('height', 0), x.get('tbr', 0)), reverse=True)
                combined_formats     = preferred_cf  + fallback_cf
                video_only_formats   = preferred_vof + fallback_vof
            else:
                combined_formats.sort(key=lambda x: (x.get('height', 0), x.get('tbr', 0)), reverse=True)
                video_only_formats.sort(key=lambda x: (x.get('height', 0), x.get('tbr', 0)), reverse=True)
            
            # Add individual format IDs
            for fmt in combined_formats:
                format_selectors.append(fmt['format_id'])
            
            for fmt in video_only_formats:
                format_selectors.append(fmt['format_id'])
            
            # Generic fallbacks
            if height_cap:
                format_selectors.extend([
                    f'best[ext=mp4][height<={height_cap}]',
                    f'best[height<={height_cap}]',
                    'best[ext=mp4][height<=1080]',
                    'best[height<=1080]',
                    'best[ext=mp4]',
                    'best',
                    'worst',
                ])
            else:
                format_selectors.extend([
                    'best[ext=mp4]',
                    'best[height<=1080]',
                    'best[height<=720]',
                    'best[height<=480]',
                    'best',
                    'worst',
                ])
            
            # Remove duplicates while preserving order
            seen = set()
            unique_formats = []
            for fmt in format_selectors:
                if fmt not in seen:
                    seen.add(fmt)
                    unique_formats.append(fmt)
            
            return unique_formats
            
        except Exception as e:
            print(f"⚠️  Error getting formats: {e}")
            return ['best', 'worst']
        """
        Determine the best available format for download.
        
        Args:
            url: Video URL
            preferred_quality: Preferred quality setting
            
        Returns:
            Format selector string for yt-dlp
        """
        try:
            # Get available formats
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'listformats': True,
                'noplaylist': True,  # Prevent playlist enumeration
                'remote_components': ['ejs:github'],  # Enable JS challenge solver
            }
            if self.cookies_file:
                ydl_opts['cookiefile'] = self.cookies_file
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                formats = info.get('formats', [])
                
            if not formats:
                return preferred_quality
            
            # Check if we have separate audio and video streams (like Tubi)
            has_audio_only = any(f.get('acodec') != 'none' and f.get('vcodec') == 'none' for f in formats)
            has_video_only = any(f.get('vcodec') != 'none' and f.get('acodec') == 'none' for f in formats)
            
            if has_audio_only and has_video_only:
                # Site uses separate audio/video streams (like Tubi)
                print("Detected separate audio/video streams - using specific format IDs")
                if preferred_quality == 'best':
                    # For Tubi, try to combine the best video with best audio
                    return 'hlsv6-1358+hlsv6-default-audio-group-stream_0/best'
                elif preferred_quality == 'worst':
                    return 'hlsv6-735+hlsv6-default-audio-group-stream_0/worst'
                else:
                    # Fallback to generic format
                    return f'{preferred_quality}+bestaudio/{preferred_quality}'
            
            # Standard format selection for sites with combined streams
            format_selectors = []
            
            if preferred_quality == 'best':
                # Prefer mp4 with good quality
                format_selectors = [
                    'best[ext=mp4][height<=1080]',
                    'best[ext=mp4]', 
                    'best[height<=1080]',
                    'best'
                ]
            elif preferred_quality == 'worst':
                format_selectors = [
                    'worst[ext=mp4]',
                    'worst'
                ]
            else:
                # Custom format specified
                return preferred_quality
            
            # Try each format selector until one works
            for fmt in format_selectors:
                try:
                    test_opts = {
                        'quiet': True,
                        'no_warnings': True,
                        'format': fmt,
                        'simulate': True,
                        'noplaylist': True,  # Prevent playlist enumeration
                        'remote_components': ['ejs:github'],  # Enable JS challenge solver
                    }
                    if self.cookies_file:
                        test_opts['cookiefile'] = self.cookies_file
                    with yt_dlp.YoutubeDL(test_opts) as ydl:
                        ydl.extract_info(url, download=False)
                        return fmt
                except:
                    continue
            
            # If nothing worked, fall back to original preference
            return preferred_quality
            
        except Exception:
            # If format detection fails, use the original preference
            return preferred_quality

    def download_video(self, url: str, quality: str = 'best', max_retries: int = 3) -> Tuple[str, Dict]:
        """
        Download video from any supported site with comprehensive format fallback.
        
        Args:
            url: Video URL from any supported site
            quality: Video quality preference ('best', 'worst', or format specifier)
            max_retries: Maximum number of retry attempts per format
            
        Returns:
            Tuple of (downloaded_file_path, video_info)
        """
        # First extract info to get metadata
        info = self.extract_video_info(url)
        
        # Create sanitized filename
        original_title = info.get('title', 'Unknown')
        uploader = info.get('uploader', 'Unknown')
        video_id = info.get('id', 'unknown')
        
        # Create filename: title_by_uploader_id
        filename_base = f"{original_title}_by_{uploader}_{video_id}"
        safe_filename = self.sanitize_filename(filename_base)
        
        # Get ALL available formats to try
        available_formats = self.get_all_available_formats(url, quality=quality)
        print(f"📋 Found {len(available_formats)} format options to try")
        
        # Setup base download options
        output_path = self.download_dir / f"{safe_filename}.%(ext)s"
        
        # Base configurations with different robustness levels
        base_configs = [
            # Config 1: Standard settings
            {
                'outtmpl': str(output_path),
                'quiet': True,
                'no_warnings': False,
                'ignoreerrors': False,
                'merge_output_format': 'mp4',
                'retries': 2,
                'socket_timeout': 30,
                'remote_components': ['ejs:github'],  # Enable JS challenge solver
            },
            # Config 2: More permissive
            {
                'outtmpl': str(output_path),
                'quiet': True,
                'ignoreerrors': True,
                'no_check_certificate': True,
                'merge_output_format': 'mp4',
                'retries': 3,
                'socket_timeout': 60,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'remote_components': ['ejs:github'],  # Enable JS challenge solver
            },
            # Config 3: Very permissive fallback
            {
                'outtmpl': str(output_path),
                'quiet': False,
                'ignoreerrors': True,
                'no_check_certificate': True,
                'skip_unavailable_fragments': True,
                'retries': 5,
                'fragment_retries': 3,
                'socket_timeout': 120,
                'http_chunk_size': 1024*1024,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'referer': url,
                'remote_components': ['ejs:github'],  # Enable JS challenge solver
            }
        ]
        
        # Add cookies to all configs if available
        if self.cookies_file:
            for config in base_configs:
                config['cookiefile'] = self.cookies_file
        
        total_attempts = len(available_formats) * len(base_configs)
        attempt_count = 0
        last_error = None
        
        print(f"🎯 Will try up to {total_attempts} combinations (formats × configs)")
        
        # Try each format with each configuration
        for format_idx, format_selector in enumerate(available_formats, 1):
            print(f"\n📺 Trying format {format_idx}/{len(available_formats)}: {format_selector}")
            
            for config_idx, base_config in enumerate(base_configs, 1):
                attempt_count += 1
                
                # Combine format with configuration
                ydl_opts = base_config.copy()
                ydl_opts['format'] = format_selector
                
                print(f"   🔄 Attempt {attempt_count}/{total_attempts} (Config {config_idx}/{len(base_configs)})")
                
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                        
                    # Find the downloaded file
                    downloaded_files = list(self.download_dir.glob(f"{safe_filename}.*"))
                    if downloaded_files:
                        downloaded_file = downloaded_files[0]
                        print(f"✅ SUCCESS! Downloaded: {downloaded_file}")
                        
                        # Clean up any temporary fragments
                        self._cleanup_temp_fragments(safe_filename)
                        
                        return str(downloaded_file), info
                        
                except Exception as e:
                    last_error = e
                    error_msg = str(e)
                    
                    # Show concise error for common issues
                    if "Requested format is not available" in error_msg:
                        print(f"   ❌ Format not available")
                    elif "Postprocessing" in error_msg:
                        print(f"   ❌ Merge failed")
                    elif "Video unavailable" in error_msg:
                        print(f"   ❌ Video unavailable")
                    else:
                        print(f"   ❌ Failed: {error_msg[:100]}...")
                    
                    # Clean up any partial downloads
                    self._cleanup_temp_fragments(safe_filename)
                    
                    # Brief pause between attempts
                    if attempt_count < total_attempts:
                        time.sleep(1)
        
        # If we get here, all formats and configs failed
        raise Exception(f"❌ ALL {total_attempts} download attempts failed! Last error: {str(last_error)}")

    def get_best_format(self, url: str, preferred_quality: str = 'best') -> str:
        """
        Get the best format based on user preference (kept for compatibility).
        
        Args:
            url: Video URL
            preferred_quality: Preferred quality setting
            
        Returns:
            Format selector string for yt-dlp
        """
        # This method is now simplified since download_video handles comprehensive fallback
        formats = self.get_all_available_formats(url)
        return formats[0] if formats else preferred_quality
        """
        Download video from any supported site with retry logic for difficult sites.
        
        Args:
            url: Video URL from any supported site
            quality: Video quality preference ('best', 'worst', or format specifier)
            max_retries: Maximum number of retry attempts
            
        Returns:
            Tuple of (downloaded_file_path, video_info)
        """
        # First extract info to get metadata
        info = self.extract_video_info(url)
        
        # Create sanitized filename
        original_title = info.get('title', 'Unknown')
        uploader = info.get('uploader', 'Unknown')
        video_id = info.get('id', 'unknown')
        
        # Create filename: title_by_uploader_id
        filename_base = f"{original_title}_by_{uploader}_{video_id}"
        safe_filename = self.sanitize_filename(filename_base)
        
    def _cleanup_temp_fragments(self, base_filename: str):
        """
        Clean up temporary fragment files and partial downloads.
        
        Args:
            base_filename: Base filename to search for related temp files
        """
        try:
            # Clean up common temporary file patterns
            patterns_to_clean = [
                f"{base_filename}.f*",  # Fragment files
                f"{base_filename}.part*",  # Partial downloads
                f"{base_filename}.ytdl*",  # yt-dlp temp files
                f"{base_filename}.temp*",  # General temp files
                f"*.f{base_filename.split('_')[-1]}*",  # Fragment files with format ID
            ]
            
            for pattern in patterns_to_clean:
                for temp_file in self.download_dir.glob(pattern):
                    try:
                        temp_file.unlink()
                        print(f"🧹 Cleaned up temp file: {temp_file.name}")
                    except Exception as e:
                        print(f"⚠️  Could not remove temp file {temp_file.name}: {e}")
                        
        except Exception as e:
            print(f"⚠️  Error during temp file cleanup: {e}")

    def cleanup_download_directory(self, keep_final_files: bool = False):
        """
        Clean up the entire download directory.
        
        Args:
            keep_final_files: If True, only remove temp files, keep final videos
        """
        try:
            if not keep_final_files:
                # Remove all files
                for file_path in self.download_dir.iterdir():
                    if file_path.is_file():
                        try:
                            file_path.unlink()
                            print(f"🧹 Cleaned up: {file_path.name}")
                        except Exception as e:
                            print(f"⚠️  Could not remove {file_path.name}: {e}")
            else:
                # Only remove temporary files, keep final videos
                temp_patterns = ["*.f*", "*.part*", "*.ytdl*", "*.temp*"]
                for pattern in temp_patterns:
                    for temp_file in self.download_dir.glob(pattern):
                        try:
                            temp_file.unlink()
                            print(f"🧹 Cleaned up temp file: {temp_file.name}")
                        except Exception as e:
                            print(f"⚠️  Could not remove temp file {temp_file.name}: {e}")
                            
            # Try to remove directory if empty and it's the default tmp dir
            if (self.download_dir.name == "tmp" and 
                self.download_dir.parent == Path.cwd() and
                not any(self.download_dir.iterdir())):
                try:
                    self.download_dir.rmdir()
                    print("🧹 Removed empty tmp directory")
                except Exception as e:
                    print(f"⚠️  Could not remove tmp directory: {e}")
                    
        except Exception as e:
            print(f"⚠️  Error during directory cleanup: {e}")

    def sanitize_tag(self, tag: str) -> str:
        """
        Sanitize a tag by removing special characters and normalizing.
        
        Args:
            tag: Original tag string
            
        Returns:
            Sanitized tag
        """
        if not tag:
            return ""
        
        # Convert to lowercase and remove special characters
        sanitized = re.sub(r'[^\w\s]', '', tag.lower())
        # Remove extra whitespace and replace spaces with empty string
        sanitized = re.sub(r'\s+', '', sanitized.strip())
        return sanitized

    def generate_tags(self, info: Dict, playlist_info: Optional[Dict] = None, url: str = "") -> List[str]:
        """
        Generate relevant tags from video information.
        
        Args:
            info: Video information dictionary from yt-dlp
            playlist_info: Optional playlist information
            url: Original URL for additional context
            
        Returns:
            List of tags
        """
        tags = []
        
        # Add series name as primary tag for TV shows
        series = self.extract_series_name(info, url, playlist_info)
        if series:
            clean_series = self.clean_series_title(series)
            print(f"[DEBUG] Detected series: '{clean_series}'")
            # Add both the cleaned series name and sanitized version
            tags.append(clean_series)
            sanitized_series = self.sanitize_tag(clean_series)
            if sanitized_series and sanitized_series != clean_series.lower():
                tags.append(sanitized_series)
        else:
            # More detailed debug info
            playlist_url = playlist_info.get('webpage_url', 'None') if playlist_info else 'None'
            print(f"[DEBUG] No series detected:")
            print(f"         Title: '{info.get('title', '')}'")
            print(f"         URL: '{url}'")
            print(f"         Playlist URL: '{playlist_url}'")
        
        # Add season info if available
        # Note: season_number is always numeric (e.g. 1), while season can be a string like "Season 1"
        if info.get('season_number'):
            season_tag = f"Season {info['season_number']}"
            tags.append(season_tag)
        elif info.get('season'):
            season_val = str(info['season'])
            # Avoid "Season Season 1" - check if it already starts with "Season"
            if season_val.lower().startswith('season'):
                season_tag = season_val
            else:
                season_tag = f"Season {season_val}"
            tags.append(season_tag)
        
        # Add playlist information as tags
        if playlist_info:
            if playlist_info.get('title'):
                # Add playlist title as tag
                playlist_tag = f"playlist-{playlist_info['title']}"
                tags.append(playlist_tag)
            
            if playlist_info.get('uploader'):
                tags.append(f"playlist-by-{playlist_info['uploader']}")
        
        # Add uploader as tag
        if info.get('uploader'):
            tags.append(info['uploader'])
            
        # Add channel name if different from uploader
        if info.get('channel') and info['channel'] != info.get('uploader'):
            tags.append(info['channel'])
            
        # Add categories if available
        if info.get('categories'):
            tags.extend(info['categories'])
        
        # Add genres if available (from extended metadata, e.g. Tubi)
        if info.get('genres'):
            for genre in info['genres']:
                if genre and isinstance(genre, str) and genre not in tags:
                    tags.append(genre)
            
        # Add tags from video if available
        if info.get('tags'):
            # Limit to reasonable number of tags
            tags.extend(info['tags'][:8])  # Reduced to make room for series tags
            
        # Clean and deduplicate tags
        cleaned_tags = []
        seen_tags = set()
        
        for tag in tags:
            if tag and isinstance(tag, str):
                # Clean tag - preserve periods and hyphens, remove other special chars
                clean_tag = re.sub(r'[^\w\s.\-]', '', tag.strip())
                # Filter out invalid tags (like S01E14 patterns)
                if clean_tag and self.is_valid_tag(clean_tag):
                    # Use normalized form for deduplication to match MediaCMS behavior
                    normalized = self.normalize_tag(clean_tag)
                    if normalized not in seen_tags:
                        cleaned_tags.append(clean_tag)
                        seen_tags.add(normalized)
        
        print(f"[DEBUG] Generated {len(cleaned_tags)} tags: {cleaned_tags}")
        return cleaned_tags[:15]  # Limit to 15 tags

    def extract_series_name(self, info: Dict, url: str = "", playlist_info: Optional[Dict] = None) -> str:
        """
        Extract series name from video info with fallback methods.
        
        Args:
            info: Video information dictionary from yt-dlp
            url: Original URL for additional context
            playlist_info: Optional playlist information
            
        Returns:
            Series name or empty string if not found
        """
        # First, try the direct series field
        if info.get('series'):
            return info['series']
        
        # Fallback 0.5: Use playlist title (most reliable for series processed via process_playlist)
        if playlist_info and playlist_info.get('title'):
            playlist_title = playlist_info['title'].strip()
            # Playlist title IS the series name for TV show playlists
            if playlist_title and playlist_title != 'Unknown Playlist':
                return playlist_title
        
        # Fallback 1: Extract from title patterns
        title = info.get('title', '')
        
        # Common TV series title patterns
        series_patterns = [
            r'^(.+?)\s*-\s*S\d+E\d+',  # "Series Name - S01E01"
            r'^(.+?)\s*-\s*S\d+:\s*E\d+',  # "Series Name - S01: E01" (Tubi format)
            r'^(.+?)\s*[Ss]eason\s*\d+',  # "Series Name Season 1"
            r'^(.+?)\s*[Ee]pisode\s*\d+',  # "Series Name Episode 1"
            r'^(.+?)\s*-\s*Episode\s*\d+',  # "Series Name - Episode 1"
            r'^(.+?)\s*\|\s*[Ss]\d+[Ee]\d+',  # "Series Name | S1E1"
        ]
        
        for pattern in series_patterns:
            match = re.match(pattern, title)
            if match:
                series_name = match.group(1).strip()
                if series_name:
                    return series_name
        
        # Fallback 2: Extract from original playlist URL (for Tubi series)
        # Check if we have playlist info with the original series URL
        if playlist_info and playlist_info.get('webpage_url'):
            original_url = playlist_info['webpage_url']
            url_patterns = [
                r'/series/\d+/([^/?]+)',  # Tubi series URLs like /series/300015693/the-i-t-crowd
                r'/show/([^/?]+)',         # Other show URLs
                r'/series/([^/?]+)',       # Generic series URLs
            ]
            
            for pattern in url_patterns:
                match = re.search(pattern, original_url)
                if match:
                    series_slug = match.group(1)
                    # Convert URL slug to readable title
                    series_name = series_slug.replace('-', ' ').replace('_', ' ')
                    # Title case it
                    series_name = ' '.join(word.capitalize() for word in series_name.split())
                    return series_name
        
        # Fallback 3: Extract from individual video URL (less reliable for Tubi)
        if url:
            url_patterns = [
                r'/series/\d+/([^/?]+)',  # Tubi series URLs
                r'/show/([^/?]+)',         # Other show URLs
                r'/series/([^/?]+)',       # Generic series URLs
            ]
            
            for pattern in url_patterns:
                match = re.search(pattern, url)
                if match:
                    series_slug = match.group(1)
                    # Convert URL slug to readable title
                    series_name = series_slug.replace('-', ' ').replace('_', ' ')
                    # Title case it
                    series_name = ' '.join(word.capitalize() for word in series_name.split())
                    return series_name
        
        return ""

    def clean_series_title(self, series_title: str) -> str:
        """
        Clean and format series title for better presentation.
        
        Args:
            series_title: Original series title
            
        Returns:
            Cleaned and properly formatted series title
        """
        if not series_title:
            return ""
        
        # Clean up the title
        title = series_title.strip()
        
        # Handle common formatting issues
        title = re.sub(r'\s+', ' ', title)  # Normalize whitespace
        
        # Fix common title formatting issues
        # Handle "The I T Crowd" -> "The I.T. Crowd"
        title = re.sub(r'\bI\s+T\b', 'I.T.', title, flags=re.IGNORECASE)
        
        # Handle other common abbreviations that should have periods
        title = re.sub(r'\bU\s+S\b', 'U.S.', title, flags=re.IGNORECASE)
        title = re.sub(r'\bU\s+K\b', 'U.K.', title, flags=re.IGNORECASE)
        title = re.sub(r'\bL\s+A\b', 'L.A.', title, flags=re.IGNORECASE)
        title = re.sub(r'\bN\s+Y\b', 'N.Y.', title, flags=re.IGNORECASE)
        
        return title

    def clean_title(self, title: str, info: Dict = None, url: str = "", playlist_info: Optional[Dict] = None) -> str:
        """
        Clean video title for better presentation, with special handling for TV series.
        
        Args:
            title: Original video title
            info: Optional video info dict for context
            url: Original URL for additional context
            playlist_info: Optional playlist information
            
        Returns:
            Cleaned title
        """
        if not title:
            return "Untitled Video"
        
        # For TV series, try to construct a better title
        if info:
            # Try to extract series name with fallbacks
            series = self.extract_series_name(info, url, playlist_info)
            season_number = info.get('season_number')
            episode_number = info.get('episode_number')
            episode = info.get('episode')
            
            if series:
                # Clean the series title
                clean_series = self.clean_series_title(series)
                
                # Build episode title
                episode_parts = []
                
                if season_number and episode_number:
                    episode_parts.append(f"S{season_number:02d}E{episode_number:02d}")
                
                if episode and episode.strip():
                    clean_episode = episode.strip()
                    # Remove series name from episode title if it's there
                    if clean_series.lower() in clean_episode.lower():
                        clean_episode = re.sub(re.escape(clean_series), '', clean_episode, flags=re.IGNORECASE).strip()
                        clean_episode = re.sub(r'^[\s\-\:]+', '', clean_episode)  # Remove leading separators
                    episode_parts.append(clean_episode)
                
                if episode_parts:
                    if len(episode_parts) == 1:
                        return f"{clean_series} - {episode_parts[0]}"
                    else:
                        return f"{clean_series} - {' - '.join(episode_parts)}"
                else:
                    return clean_series
        
        # Fallback to original cleaning logic
        # Remove common prefixes/suffixes
        title = re.sub(r'^\[.*?\]\s*', '', title)  # Remove [brackets] at start
        title = re.sub(r'\s*\[.*?\]$', '', title)  # Remove [brackets] at end
        title = re.sub(r'^\s*\d+\.\s*', '', title)  # Remove numbered list format
        
        # Clean up extra whitespace
        title = re.sub(r'\s+', ' ', title.strip())
        
        # Add release year if available (for movies, not TV episodes)
        if info and not info.get('season_number'):
            release_year = info.get('release_year') or info.get('year')
            if release_year:
                # Check if year is not already in title
                year_str = str(release_year)
                if year_str not in title:
                    title = f"{title} ({year_str})"
        
        return title

    def get_existing_tags(self) -> List[str]:
        """
        Get list of ALL existing tag titles from MediaCMS, paginating through
        every page of results so nothing is missed.
        
        Returns:
            List of existing tag titles
        """
        all_tags: List[dict] = []
        url = f"{self.api_url}/tags"
        page = 1
        
        try:
            while url:
                response = self.session.get(url)
                if response.status_code != 200:
                    print(f"[DEBUG] Failed to get existing tags page {page} (status {response.status_code})")
                    break
                
                data = response.json()
                
                # Handle paginated response (dict with 'results' + 'next')
                if isinstance(data, dict) and 'results' in data:
                    all_tags.extend(data['results'])
                    url = data.get('next')  # None when no more pages
                    page += 1
                # Handle non-paginated response (plain list)
                elif isinstance(data, list):
                    all_tags.extend(data)
                    url = None  # No pagination to follow
                else:
                    url = None
            
            existing_titles = [tag.get('title', '') for tag in all_tags if isinstance(tag, dict)]
            print(f"[DEBUG] Found {len(existing_titles)} existing tags ({page - 1} page(s))")
            return existing_titles
            
        except Exception as e:
            print(f"[DEBUG] Error getting existing tags: {str(e)}")
            return []

    def find_matching_existing_tags(self, desired_tags: List[str]) -> List[str]:
        """
        Find which of the desired tags already exist in MediaCMS.
        Uses normalized comparison (removes spaces/hyphens) to match MediaCMS behavior.
        
        Args:
            desired_tags: List of tag titles we want to use
            
        Returns:
            List of existing tags that match our desired tags
        """
        if not desired_tags:
            return []
        
        existing_tags = self.get_existing_tags()
        # Create mapping of normalized tag -> original tag for matching
        existing_normalized_map = {self.normalize_tag(tag): tag for tag in existing_tags}
        
        matched_tags = []
        
        for desired_tag in desired_tags:
            desired_tag = desired_tag.strip()
            if not desired_tag:
                continue
            
            # Normalize the desired tag for comparison (removes spaces, hyphens, lowercase)
            # This matches how MediaCMS normalizes tags internally
            normalized_desired = self.normalize_tag(desired_tag)
            
            # Check if normalized tag exists
            if normalized_desired in existing_normalized_map:
                existing_tag = existing_normalized_map[normalized_desired]
                matched_tags.append(existing_tag)
                print(f"[DEBUG] Found existing tag: '{existing_tag}' (matches '{desired_tag}', normalized: '{normalized_desired}')")
            else:
                print(f"[DEBUG] Tag doesn't exist: '{desired_tag}' (normalized: '{normalized_desired}') - skipping")
        
        return matched_tags

    def check_tags_availability(self, tags: List[str]) -> tuple:
        """
        Check which tags exist in MediaCMS and which are missing.
        
        Args:
            tags: List of desired tag titles
            
        Returns:
            Tuple of (existing_tags, missing_tags)
        """
        if not tags:
            return [], []
        
        print(f"[DEBUG] Checking which of {len(tags)} desired tags exist: {tags}")
        existing_matching_tags = self.find_matching_existing_tags(tags)
        missing_tags = [tag for tag in tags if tag.lower() not in [et.lower() for et in existing_matching_tags]]
        
        if missing_tags:
            print(f"⚠️  {len(missing_tags)} tags don't exist in MediaCMS: {missing_tags}")
            # Track missing tags in cumulative report
            for tag in missing_tags:
                self.missing_tags_report[tag] = self.missing_tags_report.get(tag, 0) + 1
        
        return existing_matching_tags, missing_tags

    def add_tags_to_media(self, media_id: str, existing_tags: List[str], max_retries: int = 3) -> bool:
        """
        Add pre-validated existing tags to uploaded media using MediaCMS bulk actions API.
        
        Args:
            media_id: MediaCMS media ID (friendly_token)
            existing_tags: List of tag titles that are confirmed to exist in MediaCMS
            max_retries: Maximum number of retry attempts for failed requests
            
        Returns:
            True if tags were added, False if failed
        """
        if not existing_tags:
            print(f"[DEBUG] No existing tags to add")
            return True
        
        # Use bulk actions API to add existing tags
        bulk_data = {
            "media_ids": [media_id],
            "action": "add_tags",
            "tag_titles": existing_tags
        }
        
        print(f"[DEBUG] Adding {len(existing_tags)} existing tags: {existing_tags}")
        
        def make_request():
            return self.session.post(
                f"{self.api_url}/media/user/bulk_actions/",
                json=bulk_data
            )
        
        response = self._retry_request(
            make_request,
            max_retries=max_retries,
            initial_delay=1.0,
            backoff_factor=2.0,
            operation_name=f"add tags to media {media_id}"
        )
        
        if response:
            print(f"[DEBUG] Bulk action response: {response.status_code}")
            if response.status_code != 200:
                print(f"[DEBUG] Response text: {response.text}")
                # Try to parse error details
                try:
                    error_data = response.json()
                    print(f"[DEBUG] Error details: {error_data}")
                except:
                    pass
        
        if response and response.status_code == 200:
            print(f"✅ Successfully added {len(existing_tags)} tags to media {media_id}")
            return True
        elif response:
            print(f"⚠️  Failed to add tags (status {response.status_code}): {response.text}")
        else:
            print(f"⚠️  Failed to add tags after retries")
        
        return False

    def print_missing_tags_report(self):
        """
        Print a summary report of all tags that were requested but don't exist in MediaCMS.
        Call this after processing a batch or playlist to see which tags need to be created.
        """
        if not self.missing_tags_report:
            print("\n✅ All requested tags were found in MediaCMS - no missing tags!")
            return
        
        print("\n" + "=" * 60)
        print("📋 MISSING TAGS REPORT - Tags not found in MediaCMS")
        print("=" * 60)
        print(f"The following {len(self.missing_tags_report)} tags were requested but don't exist in MediaCMS.")
        print("These tags were embedded as text in each video's description instead.")
        print("Create them in MediaCMS admin to enable proper tag-based filtering:\n")
        
        # Sort by frequency (most requested first)
        sorted_tags = sorted(self.missing_tags_report.items(), key=lambda x: x[1], reverse=True)
        for tag, count in sorted_tags:
            print(f"  • {tag}  (requested {count}x)")
        
        print("\n💡 To create these tags, go to MediaCMS Admin > Tags > Add Tag")
        print("=" * 60)

    def create_description(self, info: Dict, original_url: str, playlist_info: Optional[Dict] = None, 
                         playlist_index: Optional[int] = None, tags: Optional[List[str]] = None,
                         missing_tags: Optional[List[str]] = None) -> str:
        """
        Create a description for the MediaCMS upload.
        
        Includes full cast lists, directors, genres, content ratings, and other
        extended metadata where available (especially from Tubi and similar sites).
        Tags that couldn't be saved via the API (because they don't exist in MediaCMS)
        are embedded as searchable text in the description.
        
        Args:
            info: Video information dictionary
            original_url: Original URL from the source site
            playlist_info: Optional playlist information
            playlist_index: Optional position in playlist
            tags: Optional list of all desired tags
            missing_tags: Optional list of tags that don't exist in MediaCMS
            
        Returns:
            Formatted description
        """
        description_parts = []
        
        # Add playlist information if available
        if playlist_info:
            playlist_parts = []
            if playlist_info.get('title'):
                playlist_parts.append(f"Playlist: {playlist_info['title']}")
            if playlist_index is not None:
                total_videos = len(playlist_info.get('entries', []))
                playlist_parts.append(f"Video {playlist_index + 1} of {total_videos}")
            if playlist_info.get('uploader'):
                playlist_parts.append(f"Playlist Creator: {playlist_info['uploader']}")
            if playlist_parts:
                description_parts.append("Playlist Information:\n" + "\n".join(playlist_parts))
        
        # Add original description if available (no truncation - preserve full metadata)
        if info.get('description'):
            orig_desc = info['description'].strip()
            description_parts.append(f"Original Description:\n{orig_desc}")
            
        # Add metadata
        metadata_parts = []
        
        # Add series information if available (with fallback extraction)
        series = self.extract_series_name(info, original_url, playlist_info)
        if series:
            clean_series = self.clean_series_title(series)
            metadata_parts.append(f"Series: {clean_series}")
        if info.get('season_number'):
            metadata_parts.append(f"Season: {info['season_number']}")
        if info.get('episode_number'):
            metadata_parts.append(f"Episode: {info['episode_number']}")
        if info.get('episode') and info['episode'].strip():
            metadata_parts.append(f"Episode Title: {info['episode'].strip()}")
        
        if info.get('uploader'):
            metadata_parts.append(f"Original Creator: {info['uploader']}")
        if info.get('upload_date'):
            upload_date = info['upload_date']
            formatted_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
            metadata_parts.append(f"Original Upload Date: {formatted_date}")
        if info.get('release_year'):
            metadata_parts.append(f"Release Year: {info['release_year']}")
        if info.get('view_count'):
            metadata_parts.append(f"View Count: {info['view_count']:,}")
        if info.get('duration'):
            duration_int = int(info['duration'])
            duration_mins = duration_int // 60
            duration_secs = duration_int % 60
            metadata_parts.append(f"Duration: {duration_mins}:{duration_secs:02d}")
        if info.get('content_rating'):
            metadata_parts.append(f"Content Rating: {info['content_rating']}")
        if info.get('age_limit') and info['age_limit'] > 0:
            metadata_parts.append(f"Age Limit: {info['age_limit']}+")
        if info.get('language'):
            metadata_parts.append(f"Language: {info['language']}")
            
        if metadata_parts:
            description_parts.append("Video Information:\n" + "\n".join(metadata_parts))
        
        # Add cast/crew section if available (critical for Tubi and similar sources)
        cast_crew_parts = []
        
        # Cast list - from yt-dlp 'cast' field or extended metadata
        cast = info.get('cast') or info.get('actors') or []
        if isinstance(cast, str):
            cast = [c.strip() for c in cast.split(',') if c.strip()]
        if cast:
            cast_crew_parts.append(f"Cast: {', '.join(str(c) for c in cast)}")
        
        # Directors
        directors = info.get('directors') or info.get('director') or info.get('creator') or []
        if isinstance(directors, str):
            directors = [d.strip() for d in directors.split(',') if d.strip()]
        elif not isinstance(directors, list):
            directors = [str(directors)] if directors else []
        if directors:
            cast_crew_parts.append(f"Director(s): {', '.join(str(d) for d in directors)}")
        
        # Writers/creators (if separate from directors)
        creators = info.get('creators') or []
        if isinstance(creators, str):
            creators = [c.strip() for c in creators.split(',') if c.strip()]
        if creators:
            cast_crew_parts.append(f"Writer(s): {', '.join(str(c) for c in creators)}")
        
        if cast_crew_parts:
            description_parts.append("Cast & Crew:\n" + "\n".join(cast_crew_parts))
        
        # Add genres if available
        genres = info.get('genres') or info.get('genre') or []
        if isinstance(genres, str):
            genres = [g.strip() for g in genres.split(',') if g.strip()]
        categories = info.get('categories') or []
        if isinstance(categories, str):
            categories = [c.strip() for c in categories.split(',') if c.strip()]
        all_genres = list(dict.fromkeys(genres + categories))  # Dedupe preserving order
        if all_genres:
            description_parts.append(f"Genres: {', '.join(all_genres)}")
        
        # Add tags to description as searchable text
        if tags:
            description_parts.append("Tags: " + ", ".join(tags))
        
        # Add missing tags prominently so they're searchable in the description
        # These are tags that couldn't be saved via the MediaCMS API
        if missing_tags:
            description_parts.append(
                "Additional Tags (not yet in MediaCMS):\n" + ", ".join(missing_tags)
            )
            
        # Add original URL
        description_parts.append(f"Original URL: {original_url}")
        
        return "\n\n".join(description_parts)

    def upload_to_mediacms(self, file_path: str, info: Dict, original_url: str, 
                         playlist_info: Optional[Dict] = None, playlist_index: Optional[int] = None) -> Dict:
        """
        Upload video file to MediaCMS.
        
        Args:
            file_path: Path to the video file
            info: Video information from yt-dlp
            original_url: Original URL from the source site
            playlist_info: Optional playlist information
            playlist_index: Optional position in playlist
            
        Returns:
            Response from MediaCMS API
        """
        # Prepare metadata
        title = self.clean_title(info.get('title', 'Untitled Video'), info, original_url, playlist_info)
        if len(title) > 100:
            title = title[:97] + '...'
        
        # Add playlist context to title if available
        #if playlist_info and playlist_index is not None:
        #    total_videos = len(playlist_info.get('entries', []))
        #    title = f"[{playlist_index + 1}/{total_videos}] {title}"
        
        tags = self.generate_tags(info, playlist_info, original_url)
        
        # Check which tags exist in MediaCMS BEFORE building description,
        # so missing tags can be embedded as searchable text in the description
        existing_tags, missing_tags = self.check_tags_availability(tags)
        
        if missing_tags:
            print(f"📝 {len(missing_tags)} tags will be embedded in description: {missing_tags}")
        if existing_tags:
            print(f"✅ {len(existing_tags)} tags will be saved via API: {existing_tags}")
        
        description = self.create_description(
            info, original_url, playlist_info, playlist_index, 
            tags=tags, missing_tags=missing_tags
        )
        
        # Prepare file for upload
        file_name = os.path.basename(file_path)
        
        try:
            # First, upload the file
            with open(file_path, 'rb') as video_file:
                files = {'media_file': (file_name, video_file, 'video/mp4')}
                data = {
                    'title': title,
                    'description': description,
                    'is_public': 'true',  # Make public by default
                }
                
                print(f"[DEBUG] Uploading with title: '{title}'")
                print(f"[DEBUG] Tags: {len(existing_tags)} via API, {len(missing_tags)} in description")
                
                # Remove Content-Type header for multipart upload
                upload_session = requests.Session()
                upload_session.headers.update({
                    'Authorization': f'Token {self.api_token}'
                })
                
                response = upload_session.post(
                    f"{self.api_url}/media/",
                    files=files,
                    data=data
                )
                
            if response.status_code == 201:
                result = response.json()
                
                # Add existing tags after successful upload
                if existing_tags:
                    media_id = result.get('friendly_token')
                    if media_id:
                        self.add_tags_to_media(media_id, existing_tags)
                    else:
                        print(f"⚠️  Could not get media ID for tag addition")
                
                return result
            else:
                raise Exception(f"Upload failed with status {response.status_code}: {response.text}")
                
        except Exception as e:
            raise Exception(f"Failed to upload to MediaCMS: {str(e)}")

    def process_playlist(self, url: str, quality: str = 'best', cleanup: bool = True, 
                               delay_seconds: int = 5, max_videos: Optional[int] = None) -> List[Dict]:
        """
        Process an entire playlist from any supported site.
        
        Args:
            url: Playlist URL from any supported site
            quality: Video quality preference
            cleanup: Whether to delete downloaded files after upload
            delay_seconds: Seconds to wait between video uploads
            max_videos: Maximum number of videos to process (None for all)
            
        Returns:
            List of MediaCMS response data for each video
        """
        print(f"Processing playlist: {url}")
        
        # Extract playlist information
        print("Extracting playlist information...")
        playlist_info = self.extract_playlist_info(url)
        
        playlist_title = playlist_info.get('title', 'Unknown Playlist')
        playlist_entries = playlist_info.get('entries', [])
        total_videos = len(playlist_entries)
        
        if max_videos:
            playlist_entries = playlist_entries[:max_videos]
            total_videos = min(total_videos, max_videos)
        
        print(f"Found playlist: '{playlist_title}' with {total_videos} videos")
        
        results = []
        for i, entry in enumerate(playlist_entries):
            video_url = entry.get('url') or f"https://www.youtube.com/watch?v={entry['id']}"
            video_title = entry.get('title', f'Video {i+1}')
            
            print(f"\nProcessing video {i+1}/{total_videos}: {video_title}")
            
            try:
                result = self.process_video(
                    url=video_url,
                    quality=quality,
                    cleanup=cleanup,
                    playlist_info=playlist_info,
                    playlist_index=i
                )
                
                if result.get('already_exists'):
                    enriched = result.get('enriched', False)
                    results.append({
                        'success': True,
                        'skipped': not enriched,
                        'enriched': enriched,
                        'playlist_index': i + 1,
                        'video_title': result.get('title', video_title),
                        'video_url': video_url,
                        'existing_token': result.get('friendly_token'),
                        'improvements': result.get('improvements', []),
                    })
                    if enriched:
                        print(f"✨ Enriched metadata: {video_title}")
                    else:
                        print(f"⏭️  Skipped (already up to date): {video_title}")
                else:
                    results.append({
                        'success': True,
                        'skipped': False,
                        'playlist_index': i + 1,
                        'video_title': video_title,
                        'video_url': video_url,
                        'mediacms_response': result
                    })
                    print(f"✅ Successfully uploaded: {video_title}")
                
            except Exception as e:
                print(f"❌ Failed to process video {i+1}: {str(e)}")
                results.append({
                    'success': False,
                    'skipped': False,
                    'playlist_index': i + 1,
                    'video_title': video_title,
                    'video_url': video_url,
                    'error': str(e)
                })
            
            # Wait between uploads to avoid rate limiting
            if i < len(playlist_entries) - 1 and delay_seconds > 0:
                print(f"Waiting {delay_seconds} seconds before next video...")
                time.sleep(delay_seconds)
        
        # Print missing tags report at the end of playlist processing
        self.print_missing_tags_report()
        
        return results

    def process_video(self, url: str, quality: str = 'best', cleanup: bool = True, 
                            playlist_info: Optional[Dict] = None, playlist_index: Optional[int] = None) -> Dict:
        """
        Complete process: download video from any supported site and upload to MediaCMS.
        
        Args:
            url: Video URL from any supported site
            quality: Video quality preference
            cleanup: Whether to delete downloaded file after upload
            playlist_info: Optional playlist information
            playlist_index: Optional position in playlist
            
        Returns:
            MediaCMS response data
        """
        if not playlist_info:
            print(f"Processing video: {url}")
        
        # Check if this media has already been imported to MediaCMS
        print(f"🔍 Checking if already imported...")
        existing = self.check_media_already_imported(url)
        if existing:
            existing_title = existing.get('title', 'Unknown')
            existing_token = existing.get('friendly_token', 'Unknown')
            print(f"📌 Found existing import: '{existing_title}' (ID: {existing_token})")
            print(f"  🔎 Checking metadata quality...")
            
            # Enrich metadata if the existing item is below current standards
            enrich_result = self.enrich_existing_media(
                url, existing, playlist_info, playlist_index
            )
            
            if enrich_result.get('enriched'):
                print(f"✨ Metadata enriched for: '{enrich_result.get('title')}'")
            else:
                print(f"⏭️  Already up to date — skipping: '{existing_title}'")
                self.skipped_duplicates.append({
                    'url': url,
                    'existing_title': existing_title,
                    'existing_token': existing_token,
                })
            
            return enrich_result
        print(f"✅ Not found in MediaCMS - proceeding with import")
        
        # Download video
        if not playlist_info:
            print("Downloading video...")
        file_path, info = self.download_video(url, quality)
        if not playlist_info:
            print(f"Downloaded: {file_path}")
        
        try:
            # Upload to MediaCMS
            if not playlist_info:
                print("Uploading to MediaCMS...")
            result = self.upload_to_mediacms(file_path, info, url, playlist_info, playlist_index)
            if not playlist_info:
                print(f"Upload successful! Media ID: {result.get('friendly_token', 'Unknown')}")
            
            return result
            
        finally:
            # Cleanup downloaded file if requested
            if cleanup and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    if not playlist_info:
                        print(f"🧹 Cleaned up downloaded file: {file_path}")
                except Exception as e:
                    if not playlist_info:
                        print(f"⚠️  Warning: Could not remove downloaded file {file_path}: {e}")
            
            # Clean up any remaining temporary fragments
            if cleanup:
                safe_filename = Path(file_path).stem
                self._cleanup_temp_fragments(safe_filename)


def validate_supported_url(url: str) -> Tuple[bool, str]:
    """
    Validate if the provided URL is supported by yt-dlp.
    
    Args:
        url: URL to validate
        
    Returns:
        Tuple of (is_supported, error_message)
    """
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'retries': 1,
            'noplaylist': True,  # Don't enumerate playlists during validation
            'extract_flat': 'in_playlist',  # Fast playlist detection without full extraction
            'playlistend': 1,  # Safety limit in case noplaylist is ignored
            'remote_components': ['ejs:github'],  # Enable JS challenge solver
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Try to extract info without downloading
            info = ydl.extract_info(url, download=False)
            if info:
                return True, ""
            else:
                return False, "No video information could be extracted"
    except Exception as e:
        error_msg = str(e)
        if "Unsupported URL" in error_msg:
            return False, f"URL not supported by yt-dlp: {error_msg}"
        elif "Video unavailable" in error_msg:
            return False, f"Video is unavailable: {error_msg}"
        elif "Private video" in error_msg:
            return False, f"Video is private: {error_msg}"
        elif "This video requires payment" in error_msg:
            return False, f"Video requires payment: {error_msg}"
        else:
            return False, f"Error accessing URL: {error_msg}"


def process_batch_file(file_path: str, api_url: str, api_token: str, quality: str = 'best', 
                      cleanup: bool = True, delay_seconds: int = 5, download_dir: str = None, cookies_file: str = None) -> List[Dict]:
    """
    Process a batch file containing URLs (one per line).
    
    Args:
        file_path: Path to text file with URLs
        api_url: MediaCMS API URL
        api_token: MediaCMS API token
        quality: Video quality preference
        cleanup: Whether to delete files after upload
        delay_seconds: Seconds to wait between videos
        download_dir: Optional download directory
        cookies_file: Path to cookies file for authentication
        
    Returns:
        List of processing results
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Input file not found: {file_path}")
    
    # Read URLs from file
    with open(file_path, 'r', encoding='utf-8') as f:
        urls = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
    
    if not urls:
        raise ValueError("No valid URLs found in input file")
    
    print(f"📁 Found {len(urls)} URLs to process from: {file_path}")
    print("="*60)
    
    # Initialize uploader
    uploader = MediaDownloaderToMediaCMS(
        api_url=api_url,
        api_token=api_token,
        download_dir=download_dir,
        cookies_file=cookies_file
    )
    
    results = []
    successful = 0
    failed = 0
    skipped = 0
    enriched_count = 0
    
    for i, url in enumerate(urls, 1):
        print(f"\n📺 Processing {i}/{len(urls)}: {url}")
        print("-" * 50)
        
        try:
            # Clean the URL (handle YouTube Mix playlists etc.)
            cleaned_url = clean_youtube_url(url)
            
            # Validate URL
            is_supported, error_msg = validate_supported_url(cleaned_url)
            if not is_supported:
                result = {
                    'index': i,
                    'url': url,
                    'success': False,
                    'error': f"URL validation failed: {error_msg}",
                    'title': 'Unknown'
                }
                results.append(result)
                failed += 1
                print(f"❌ FAILED: {error_msg}")
                continue
            
            # Check if it's a playlist
            if uploader.is_playlist_url(cleaned_url):
                print("🎵 Detected playlist - processing as playlist")
                # Process as playlist but limit to reasonable number
                playlist_results = uploader.process_playlist(
                    url=cleaned_url,
                    quality=quality,
                    cleanup=cleanup,
                    delay_seconds=delay_seconds,
                    max_videos=10  # Limit playlist items to avoid overwhelming
                )
                
                # Add playlist results to main results
                for pr in playlist_results:
                    pr['batch_index'] = i
                    pr['batch_url'] = url
                    results.append(pr)
                    if pr.get('enriched'):
                        enriched_count += 1
                    elif pr.get('skipped'):
                        skipped += 1
                    elif pr['success']:
                        successful += 1
                    else:
                        failed += 1
                        
            else:
                # Process single video
                print("🎬 Processing single video")
                result = uploader.process_video(
                    url=cleaned_url,
                    quality=quality,
                    cleanup=cleanup
                )
                
                if result.get('already_exists'):
                    enriched = result.get('enriched', False)
                    batch_result = {
                        'index': i,
                        'url': url,
                        'success': True,
                        'skipped': not enriched,
                        'enriched': enriched,
                        'title': result.get('title', 'Unknown'),
                        'mediacms_id': result.get('friendly_token', 'Unknown'),
                        'improvements': result.get('improvements', []),
                    }
                    results.append(batch_result)
                    if enriched:
                        enriched_count += 1
                        print(f"✨ ENRICHED: {batch_result['title']} (ID: {batch_result['mediacms_id']})")
                    else:
                        skipped += 1
                        print(f"⏭️  SKIPPED (up to date): {batch_result['title']} (ID: {batch_result['mediacms_id']})")
                else:
                    batch_result = {
                        'index': i,
                        'url': url,
                        'success': True,
                        'skipped': False,
                        'title': result.get('title', 'Unknown'),
                        'mediacms_id': result.get('friendly_token', 'Unknown'),
                        'mediacms_response': result
                    }
                    results.append(batch_result)
                    successful += 1
                    print(f"✅ SUCCESS: {batch_result['title']} (ID: {batch_result['mediacms_id']})")
                
        except Exception as e:
            error_msg = str(e)
            result = {
                'index': i,
                'url': url,
                'success': False,
                'error': error_msg,
                'title': 'Unknown'
            }
            results.append(result)
            failed += 1
            print(f"❌ FAILED: {error_msg}")
        
        # Delay between videos (except for the last one)
        if i < len(urls) and delay_seconds > 0:
            print(f"⏳ Waiting {delay_seconds} seconds before next video...")
            time.sleep(delay_seconds)
    
    # Final cleanup
    uploader.cleanup_download_directory(keep_final_files=not cleanup)
    
    # Print missing tags report
    uploader.print_missing_tags_report()
    
    # Print summary
    print("\n" + "="*60)
    print("BATCH PROCESSING COMPLETE!")
    print("="*60)
    print(f"Total URLs: {len(urls)}")
    print(f"Uploaded (new):      {successful}")
    print(f"Enriched (updated):  {enriched_count}")
    print(f"Skipped (unchanged): {skipped}")
    print(f"Failed:              {failed}")
    
    if enriched_count > 0:
        print(f"\n✨ Enriched (metadata updated):")
        for result in results:
            if result.get('enriched'):
                mid = result.get('mediacms_id', result.get('existing_token', '?'))
                improvements = result.get('improvements', [])
                print(f"  {result.get('index', '?')}. {result.get('title', '?')} (ID: {mid})")
                for imp in improvements:
                    print(f"     • {imp}")
    
    if skipped > 0:
        print(f"\n⏭️  Skipped (already up to date):")
        for result in results:
            if result.get('skipped'):
                mid = result.get('mediacms_id', result.get('existing_token', '?'))
                print(f"  {result.get('index', '?')}. {result.get('title', '?')} (MediaCMS ID: {mid})")
    
    if successful > 0:
        print(f"\n✅ Uploaded (new):")
        for result in results:
            if result['success'] and not result.get('skipped') and not result.get('enriched'):
                print(f"  {result.get('index', '?')}. {result.get('title', '?')}")
    
    if failed > 0:
        print(f"\n❌ Failed to process:")
        for result in results:
            if not result['success']:
                print(f"  {result['index']}. {result['url']}: {result['error']}")
    
    print("="*60)
    return results


def main():
    """Main function to handle command line interface."""
    parser = argparse.ArgumentParser(
        description='Download videos from any yt-dlp supported site and upload to MediaCMS',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Single YouTube video
    python youtube_to_mediacms.py --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \\
                                  --api-url "https://mediacms.example.com/api/v1" \\
                                  --api-token "your-token-here"
                                  
    # Batch processing from file
    python youtube_to_mediacms.py --input-file "urls.txt" \\
                                  --api-url "https://mediacms.example.com/api/v1" \\
                                  --api-token "your-token-here" \\
                                  --delay 10
                                  
    # Vimeo video
    python youtube_to_mediacms.py --url "https://vimeo.com/123456789" \\
                                  --api-url "https://mediacms.example.com/api/v1" \\
                                  --api-token "your-token-here"
                                  
    # TikTok video
    python youtube_to_mediacms.py --url "https://www.tiktok.com/@user/video/1234567890" \\
                                  --api-url "https://mediacms.example.com/api/v1" \\
                                  --api-token "your-token-here"
                                  
    # Playlist (any supported site)
    python youtube_to_mediacms.py --url "https://www.youtube.com/playlist?list=PLXExample" \\
                                  --api-url "https://mediacms.example.com/api/v1" \\
                                  --api-token "your-token-here" \\
                                  --delay 10 \\
                                  --max-videos 5
                                  
    # Video with quality options
    python youtube_to_mediacms.py --url "https://youtu.be/dQw4w9WgXcQ" \\
                                  --api-url "https://mediacms.example.com/api/v1" \\
                                  --api-token "your-token-here" \\
                                  --quality "worst" \\
                                  --no-cleanup
                                  
    # Find all media imported from Tubi and generate a report
    python youtube_to_mediacms.py --find-source tubi \\
                                  --api-url "https://mediacms.example.com/api/v1" \\
                                  --api-token "your-token-here"
                                  
    # Batch-enrich all Tubi media with outdated metadata
    python youtube_to_mediacms.py --enrich-source tubi \\
                                  --api-url "https://mediacms.example.com/api/v1" \\
                                  --api-token "your-token-here" \\
                                  --delay 10
        """
    )
    
    parser.add_argument(
        '--url', 
        help='Video or playlist URL from any yt-dlp supported site (YouTube, Vimeo, TikTok, etc.)'
    )
    parser.add_argument(
        '--input-file',
        help='Text file containing URLs (one per line) for batch processing'
    )
    parser.add_argument(
        '--api-url', 
        help='MediaCMS API base URL (e.g., https://mediacms.example.com/api/v1)'
    )
    parser.add_argument(
        '--api-token', 
        help='MediaCMS API authentication token'
    )
    parser.add_argument(
        '--quality', 
        default='best', 
        help='Video quality to download (best/worst/custom format, default: best)'
    )
    parser.add_argument(
        '--list-formats',
        action='store_true',
        help='List available formats for the given URL and exit'
    )
    parser.add_argument(
        '--download-dir', 
        help='Directory to download videos to (default: system temp)'
    )
    parser.add_argument(
        '--no-cleanup', 
        action='store_true', 
        help="Don't delete downloaded files after upload"
    )
    parser.add_argument(
        '--delay', 
        type=int,
        default=5, 
        help='Seconds to wait between playlist video uploads (default: 5)'
    )
    parser.add_argument(
        '--max-videos', 
        type=int,
        help='Maximum number of videos to process from playlist (default: all)'
    )
    parser.add_argument(
        '--find-source',
        metavar='SOURCE',
        help='Find all media imported from a source (e.g. tubi, youtube, vimeo) and generate a report'
    )
    parser.add_argument(
        '--enrich-source',
        metavar='SOURCE',
        help='Find all media from a source and batch-enrich any with outdated metadata'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force enrichment of ALL items from a source, even those that appear up to date (use with --enrich-source)'
    )
    parser.add_argument(
        '--cookies',
        help='Path to cookies file for authentication (e.g., cookies.txt exported from browser)'
    )
    
    args = parser.parse_args()
    
    # Validate that at least one mode of operation is provided
    has_source_op = args.find_source or args.enrich_source
    if not args.url and not args.input_file and not has_source_op:
        print("Error: One of --url, --input-file, --find-source, or --enrich-source must be provided", file=sys.stderr)
        sys.exit(1)
    
    # Ensure mutually exclusive modes
    mode_count = sum(bool(x) for x in [args.url, args.input_file, args.find_source, args.enrich_source])
    if mode_count > 1:
        print("Error: Only one of --url, --input-file, --find-source, or --enrich-source may be used at a time", file=sys.stderr)
        sys.exit(1)
    
    # ── Source discovery / enrichment modes ──────────────────────────
    if has_source_op:
        if not args.api_url or not args.api_token:
            print("Error: --api-url and --api-token are required for source operations", file=sys.stderr)
            sys.exit(1)
        if not args.api_url.startswith(('http://', 'https://')):
            print("Error: API URL must start with http:// or https://", file=sys.stderr)
            sys.exit(1)
        
        try:
            uploader = MediaDownloaderToMediaCMS(
                api_url=args.api_url,
                api_token=args.api_token,
                download_dir=args.download_dir,
                cookies_file=args.cookies
            )
            
            if args.find_source:
                media_list = uploader.find_media_by_source(args.find_source, verbose=True)
                uploader.print_source_media_report(args.find_source, media_list)
            else:
                uploader.enrich_media_by_source(
                    source=args.enrich_source,
                    delay_seconds=args.delay,
                    force=args.force
                )
        except Exception as e:
            print(f"Error: {str(e)}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    
    # ── Batch file mode ─────────────────────────────────────────────
    if args.input_file:
        # Validate required arguments for batch processing
        if not args.api_url or not args.api_token:
            print("Error: --api-url and --api-token are required for batch processing", file=sys.stderr)
            sys.exit(1)
            
        if not args.api_url.startswith(('http://', 'https://')):
            print("Error: API URL must start with http:// or https://", file=sys.stderr)
            sys.exit(1)
        
        try:
            results = process_batch_file(
                file_path=args.input_file,
                api_url=args.api_url,
                api_token=args.api_token,
                quality=args.quality,
                cleanup=not args.no_cleanup,
                delay_seconds=args.delay,
                download_dir=args.download_dir,
                cookies_file=args.cookies
            )
            
            # Exit with appropriate code
            failed_count = len([r for r in results if not r['success']])
            sys.exit(1 if failed_count > 0 else 0)
            
        except Exception as e:
            print(f"Error processing batch file: {str(e)}", file=sys.stderr)
            sys.exit(1)
    
    # Single URL mode (original behavior)
    # Clean the URL first - strip YouTube Mix/Radio playlist parameters
    # This must happen BEFORE any yt-dlp calls to prevent infinite hangs
    args.url = clean_youtube_url(args.url)
    
    # Handle list-formats option first
    if args.list_formats:
        print(f"Listing available formats for: {args.url}")
        try:
            ydl_opts = {
                'listformats': True,
                'quiet': False,
                'noplaylist': True,  # Prevent playlist enumeration
                'remote_components': ['ejs:github'],  # Enable JS challenge solver
            }
            if args.cookies:
                ydl_opts['cookiefile'] = args.cookies
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(args.url, download=False)
        except Exception as e:
            print(f"Error listing formats: {str(e)}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    # Validate required arguments for actual processing
    if not args.api_url or not args.api_token:
        print("Error: --api-url and --api-token are required for video processing", file=sys.stderr)
        sys.exit(1)
    
    # Validate inputs
    is_supported, error_msg = validate_supported_url(args.url)
    if not is_supported:
        print(f"Error: {error_msg}", file=sys.stderr)
        sys.exit(1)
        
    if not args.api_url.startswith(('http://', 'https://')):
        print("Error: API URL must start with http:// or https://", file=sys.stderr)
        sys.exit(1)
    
    try:
        # Initialize uploader
        uploader = MediaDownloaderToMediaCMS(
            api_url=args.api_url,
            api_token=args.api_token,
            download_dir=args.download_dir,
            cookies_file=args.cookies
        )
        
        # Check if URL is a playlist
        if uploader.is_playlist_url(args.url):
            print("Detected playlist URL")
            
            # Process the playlist
            results = uploader.process_playlist(
                url=args.url,
                quality=args.quality,
                cleanup=not args.no_cleanup,
                delay_seconds=args.delay,
                max_videos=args.max_videos
            )
            
            # Print summary
            uploaded = [r for r in results if r['success'] and not r.get('skipped') and not r.get('enriched')]
            enriched = [r for r in results if r.get('enriched')]
            skipped = [r for r in results if r.get('skipped')]
            failed = [r for r in results if not r['success']]
            
            print("\n" + "="*60)
            print("PLAYLIST PROCESSING COMPLETE!")
            print("="*60)
            print(f"Total videos: {len(results)}")
            print(f"Uploaded (new):      {len(uploaded)}")
            print(f"Enriched (updated):  {len(enriched)}")
            print(f"Skipped (unchanged): {len(skipped)}")
            print(f"Failed:              {len(failed)}")
            
            if uploaded:
                print(f"\n✅ Uploaded (new):")
                for result in uploaded:
                    media_id = result['mediacms_response'].get('friendly_token', 'Unknown')
                    print(f"  {result['playlist_index']}. {result['video_title']} (ID: {media_id})")
            
            if enriched:
                print(f"\n✨ Enriched (metadata updated):")
                for result in enriched:
                    token = result.get('existing_token', '?')
                    print(f"  {result['playlist_index']}. {result['video_title']} (ID: {token})")
                    for imp in result.get('improvements', []):
                        print(f"     • {imp}")
            
            if skipped:
                print(f"\n⏭️  Skipped (already up to date):")
                for result in skipped:
                    token = result.get('existing_token', '?')
                    print(f"  {result['playlist_index']}. {result['video_title']} (MediaCMS ID: {token})")
            
            if failed:
                print(f"\n❌ Failed uploads:")
                for result in failed:
                    print(f"  {result['playlist_index']}. {result['video_title']}: {result['error']}")
            
            print("="*60)
            
            # Final cleanup of any remaining temp files
            uploader.cleanup_download_directory(keep_final_files=args.no_cleanup)
            
        else:
            print("Detected single video URL")
            
            # Process the single video
            result = uploader.process_video(
                url=args.url,
                quality=args.quality,
                cleanup=not args.no_cleanup
            )
            
            # Print success information
            if result.get('already_exists'):
                if result.get('enriched'):
                    print("\n" + "="*50)
                    print("✨ EXISTING MEDIA — METADATA ENRICHED")
                    print("="*50)
                    print(f"Title: {result.get('title', 'Unknown')}")
                    print(f"MediaCMS ID: {result.get('friendly_token', 'Unknown')}")
                    for imp in result.get('improvements', []):
                        print(f"  • {imp}")
                    print("="*50)
                else:
                    print("\n" + "="*50)
                    print("⏭️  ALREADY IMPORTED — UP TO DATE")
                    print("="*50)
                    print(f"Title: {result.get('title', 'Unknown')}")
                    print(f"Existing MediaCMS ID: {result.get('friendly_token', 'Unknown')}")
                    print("="*50)
            else:
                print("\n" + "="*50)
                print("UPLOAD SUCCESSFUL!")
                print("="*50)
                print(f"Title: {result.get('title', 'Unknown')}")
                print(f"MediaCMS ID: {result.get('friendly_token', 'Unknown')}")
                if result.get('url'):
                    print(f"MediaCMS URL: {result['url']}")
                # Add manifest URL
                friendly_token = result.get('friendly_token')
                if friendly_token:
                    # Extract base URL from api_url (remove /api/v1 part)
                    base_url = args.api_url.replace('/api/v1', '')
                    manifest_url = f"{base_url}/api/v1/media/cytube/{friendly_token}.json?format=json"
                    print(f"Manifest URL: {manifest_url}")
                print("="*50)
        
        # Final cleanup of any remaining temp files
        uploader.cleanup_download_directory(keep_final_files=args.no_cleanup)
        
    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        
        # Cleanup on error
        try:
            if 'uploader' in locals():
                uploader.cleanup_download_directory()
        except:
            pass


# ── Headless entry point for the webqueue job runner ───────────────────────────

def run(params: dict, *, config, progress=None) -> dict:
    """Download a URL (video or playlist) and upload to MediaCMS headlessly.

    ``params`` keys: ``url`` (str, required), ``quality`` (best|good|medium),
    ``max_videos`` (int). MediaCMS creds + optional cookies come from ``config``
    (``mediacms_url``, ``mediacms_token``, ``fetch_cookies_path``). Returns
    ``{downloaded, uploaded, tokens, errors}`` for ``job_runs.detail``; the
    add-to-playlist wiring lives in the async job wrapper (it needs the DB).
    """
    url = (params.get("url") or "").strip()
    if not url:
        raise RuntimeError("fetch requires a 'url' parameter")
    quality = params.get("quality") or "medium"
    max_videos = params.get("max_videos")

    api_url = f"{config.mediacms_url.rstrip('/')}/api/v1"
    cookies = getattr(config, "fetch_cookies_path", "") or None
    download_dir = str(Path(config.image_dir).parent / "fetch-tmp") if getattr(config, "image_dir", None) else None

    def _emit(detail):
        if progress:
            progress(detail)

    url = clean_youtube_url(url)
    ok, msg = validate_supported_url(url)
    if not ok:
        raise RuntimeError(f"Unsupported URL: {msg}")

    uploader = MediaDownloaderToMediaCMS(
        api_url=api_url,
        api_token=config.mediacms_token,
        download_dir=download_dir,
        cookies_file=cookies,
    )

    tokens: list[str] = []
    errors: list[str] = []
    downloaded = 0
    uploaded = 0

    _emit({"phase": "starting", "url": url})

    if uploader.is_playlist_url(url):
        results = uploader.process_playlist(
            url=url, quality=quality, cleanup=True, delay_seconds=5, max_videos=max_videos,
        )
        for r in results:
            if not r.get("success"):
                errors.append(str(r.get("error") or "unknown error"))
                continue
            if r.get("skipped"):
                continue
            downloaded += 1
            resp = r.get("mediacms_response") or {}
            token = resp.get("friendly_token") or r.get("existing_token")
            if token:
                uploaded += 1
                tokens.append(token)
        uploader.cleanup_download_directory()
    else:
        result = uploader.process_video(url=url, quality=quality, cleanup=True)
        token = result.get("friendly_token")
        if result.get("already_exists"):
            if token:
                tokens.append(token)
        elif token:
            downloaded += 1
            uploaded += 1
            tokens.append(token)
        else:
            errors.append(result.get("error") or "no token returned")

    _emit({"phase": "done", "uploaded": uploaded, "tokens": tokens})
    return {"downloaded": downloaded, "uploaded": uploaded, "tokens": tokens, "errors": errors}


if __name__ == "__main__":
    main()
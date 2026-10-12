"""Compatibility import for Talk's daemon-owned local avatar cache.

The old public-HTTPS WhatsApp downloader is deliberately not reused: Talk
avatars require the authenticated API and are published by the sync daemon.
"""
from nctalk.avatars import cached_path

__all__ = ["cached_path"]

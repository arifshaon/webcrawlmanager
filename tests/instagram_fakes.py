"""A stand-in for Instagram, so the engine can be exercised without it.

The fake answers the same protocol the Instaloader adapter does, from data a
test lays out: profiles, their posts in the order Instagram would show them
(pinned first), comments with replies, media bytes, and the conditions
Instagram imposes -- a rate limit after so many calls, a login wall, a
checkpoint, a private profile.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterator, Optional

from webarc.instagram import (CheckpointRequired, InstagramComment,
                              InstagramPost, InstagramProfile, LoginRequired,
                              MediaItem, RateLimited, TargetUnavailable)


def post(shortcode: str, when: str, *, media_id: Optional[str] = None,
         kind: str = "image", caption: str = "", pinned: bool = False,
         comments_count: int = 0, media: Optional[list[MediaItem]] = None,
         owner: str = "qnl") -> InstagramPost:
    return InstagramPost(
        media_id=media_id or str(abs(hash(shortcode)) % 10**12),
        shortcode=shortcode, owner_username=owner, owner_id="1",
        kind=kind, created_time=when, caption=caption or f"Post {shortcode}",
        permalink_url=f"https://www.instagram.com/p/{shortcode}/",
        likes_count=3, comments_count=comments_count,
        media=media if media is not None else [
            MediaItem(url=f"https://cdn.example/{shortcode}.jpg", kind="image")],
        is_pinned=pinned, raw={"shortcode": shortcode, "fake": True})


def comment(comment_id: str, shortcode: str, text: str = "hi", *,
            parent: Optional[str] = None, when: str = "2026-01-01T00:00:00Z"
            ) -> InstagramComment:
    return InstagramComment(
        comment_id=comment_id, post_shortcode=shortcode,
        parent_comment_id=parent, author_id="9", author_username="someone",
        text=text, created_time=when, likes_count=0,
        depth=1 if parent else 0, raw={"id": comment_id, "text": text})


class _Listing:
    """A listing that can be asked again after Instagram pushed back.

    A generator that raised is finished; Instagram's real iterators are not,
    and the engine relies on asking again after a rate limit or a hold.
    """

    def __init__(self, fake: "FakeInstagram", what: str, items: list):
        self.fake, self.what, self.items, self.index = fake, what, items, 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.index >= len(self.items):
            raise StopIteration
        self.fake._tick(self.what)      # raises before advancing
        item = self.items[self.index]
        self.index += 1
        return item


class FakeInstagram:
    version = "fake"

    def __init__(self, *, signed_in: Optional[str] = "curator"):
        self.signed_in = signed_in
        self.profiles: dict[str, InstagramProfile] = {}
        self.posts_by_user: dict[str, list[InstagramPost]] = {}
        self.reels_by_user: dict[str, list[InstagramPost]] = {}
        self.posts_by_code: dict[str, InstagramPost] = {}
        self.comments_by_code: dict[str, list[InstagramComment]] = {}
        self.media: dict[str, bytes] = {}
        self.calls: Counter = Counter()
        # conditions
        self.rate_limit_after: Optional[int] = None
        self.rate_limit_once = True
        self._rate_limited = False
        self.login_wall = False
        self.checkpoint = False
        self.fail_media: set[str] = set()
        self.refreshed = 0

    # -- setting the scene --------------------------------------------------
    def add_profile(self, username: str, posts: list[InstagramPost],
                    reels: Optional[list[InstagramPost]] = None,
                    private: bool = False) -> None:
        self.profiles[username] = InstagramProfile(
            user_id="1", username=username, full_name=username.title(),
            is_private=private, posts_count=len(posts), raw={"username": username})
        self.posts_by_user[username] = list(posts)
        self.reels_by_user[username] = list(reels or [])
        for p in posts + list(reels or []):
            self.posts_by_code[p.shortcode] = p
            for m in p.media:
                self.media.setdefault(m.url, b"bytes-of-" + p.shortcode.encode())

    def add_comments(self, shortcode: str, comments: list[InstagramComment]) -> None:
        self.comments_by_code[shortcode] = list(comments)

    # -- conditions ---------------------------------------------------------
    def _tick(self, what: str) -> None:
        self.calls[what] += 1
        self.calls["total"] += 1
        if self.login_wall:
            raise LoginRequired("Login required.")
        if self.checkpoint:
            raise CheckpointRequired("checkpoint_required")
        if (self.rate_limit_after is not None
                and self.calls["total"] > self.rate_limit_after
                and not (self.rate_limit_once and self._rate_limited)):
            self._rate_limited = True
            raise RateLimited(60.0, "Please wait a few minutes.")

    def refresh(self) -> None:
        """What the engine calls after the curator resolved a hold."""
        self.refreshed += 1
        self.login_wall = False
        self.checkpoint = False

    # -- the protocol ---------------------------------------------------------
    def viewer(self) -> Optional[str]:
        self._tick("viewer")
        return self.signed_in

    def profile(self, username: str) -> InstagramProfile:
        self._tick("profile")
        if username not in self.profiles:
            raise TargetUnavailable("This profile does not exist.")
        return self.profiles[username]

    def profile_posts(self, username: str) -> Iterator[InstagramPost]:
        return _Listing(self, "post_list", self.posts_by_user.get(username, []))

    def profile_reels(self, username: str) -> Iterator[InstagramPost]:
        return _Listing(self, "reel_list", self.reels_by_user.get(username, []))

    def post(self, shortcode: str) -> InstagramPost:
        self._tick("post")
        if shortcode not in self.posts_by_code:
            raise TargetUnavailable("Instagram reports it does not exist.")
        return self.posts_by_code[shortcode]

    def comments(self, shortcode: str,
                 include_replies: bool) -> Iterator[InstagramComment]:
        self._tick("comments")
        for c in self.comments_by_code.get(shortcode, []):
            if c.depth and not include_replies:
                continue
            yield c

    def fetch(self, url: str) -> tuple[bytes, str]:
        self._tick("fetch")
        if url in self.fail_media:
            raise TargetUnavailable("HTTP 410")
        return self.media.get(url, b""), "image/jpeg"

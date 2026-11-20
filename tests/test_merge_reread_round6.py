"""Round 6: merge flush re-reads the discussion thread.

- ``_read_comment_replies(collect_all=True)`` must not stop early when one
  text repeats 5 times (the keyword-hint early exit would truncate media
  comments posted after the repeats).
- The merge/sync claim helpers must let exactly one side own a comment:
  register -> visible to the other side -> unregister removes it, and
  stale entries expire after the TTL.
"""
import asyncio
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/nb-repo")
sys.path.insert(0, "/home/hatch/workspace/nb-repo/vendor")

from nb.multitenant.bot_media import _read_comment_replies
from nb.multitenant.models import BotMediaSettings
from nb.multitenant.settings import Settings
from nb.multitenant.worker import (
    _COMMENT_CLAIM_TTL_SECONDS,
    TenantWorker,
    _comment_ident,
)


class FakeMessage:
    def __init__(self, mid, chat_id, text="", media=False):
        self.id = mid
        self.chat_id = chat_id
        self.text = text
        self.raw_text = text
        self.photo = object() if media else None
        self.video = None
        self.gif = None
        self.document = None
        self.web_page = None
        self.fwd_from = None


class FakeClient:
    def __init__(self, messages):
        self._messages = messages

    async def iter_messages(self, peer, reply_to=None, reverse=True, limit=80):
        for m in self._messages[:limit]:
            yield m


def test_read_comment_replies_collect_all():
    settings = BotMediaSettings(recent_limit=80)
    # 5 identical text comments, then a media comment.
    msgs = [FakeMessage(i, -100, text="谢谢") for i in range(1, 6)]
    msgs.append(FakeMessage(6, -100, text="", media=True))
    msgs.append(FakeMessage(7, -100, text="ok"))
    comments, hint = asyncio.run(
        _read_comment_replies(FakeClient(msgs), -100, 1, settings)
    )
    assert len(comments) == 5 and hint == "谢谢", (len(comments), hint)
    comments, hint = asyncio.run(
        _read_comment_replies(FakeClient(msgs), -100, 1, settings, collect_all=True)
    )
    assert len(comments) == 7 and hint is None, (len(comments), hint)
    assert comments[5].id == 6
    print("collect_all: OK (early-stop 5 vs full 7)")


def test_comment_claims():
    worker = TenantWorker(
        Settings(api_id=1, api_hash="x", session_encryption_key="y"),
        repo=None,
        tenant_id=2063514085,
        epoch=1,
    )
    key = ("task1", -1001, 42)
    assert _comment_ident(FakeMessage(7, -100)) == (-100, 7)
    assert worker._comment_claim_idents(worker._merge_claimed_comments, key) == set()
    worker._register_comment_claim(
        worker._merge_claimed_comments, key, [(-100, 7), (-100, 8)]
    )
    assert worker._comment_claim_idents(
        worker._merge_claimed_comments, key
    ) == {(-100, 7), (-100, 8)}
    # The other store is independent.
    assert worker._comment_claim_idents(worker._sync_claimed_comments, key) == set()
    worker._register_comment_claim(
        worker._sync_claimed_comments, key, [(-100, 9)]
    )
    assert worker._comment_claim_idents(
        worker._sync_claimed_comments, key
    ) == {(-100, 9)}
    worker._unregister_comment_claim(
        worker._merge_claimed_comments, key, [(-100, 7)]
    )
    assert worker._comment_claim_idents(
        worker._merge_claimed_comments, key
    ) == {(-100, 8)}
    # Stale entries expire.
    old = time.monotonic() - _COMMENT_CLAIM_TTL_SECONDS - 1
    worker._merge_claimed_comments[key] = (old, {(-100, 8)})
    worker._prune_comment_claims()
    assert worker._comment_claim_idents(worker._merge_claimed_comments, key) == set()
    print("comment claims: OK")


if __name__ == "__main__":
    test_read_comment_replies_collect_all()
    test_comment_claims()
    print("ROUND6 TESTS PASS")

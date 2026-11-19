"""Mock tests for the 2026-11-19 round-1 fixes (no network, no Mongo).

Covers, with fakes:

* mark watermark (image + gif): the RGBA watermark must NOT be passed
  as its own paste mask (alpha applied twice). Red-pixel count on the
  same fixture jumps from ~5 (old) to hundreds (fixed).
* MediaEmptyError is a file-reference error: predicate, plus the full
  _send_files_with_reference_retry chain (by-ref multi send fails,
  refetch-resend fails the same way, download + re-upload succeeds),
  and the re-uploaded bytes keep the source file name.
* schedule _collect_schedule_messages: a leading service message does
  not consume the fetch window or the daily budget (S1: limit=2 fetched
  [1 (service), 2], message 3 never ran); an album is never split.
* bot_media get_discussion_message: when the channel-peer query fails
  with MSG_ID_INVALID (455's source), the mirror is found via the
  linked discussion group and its replies read back.
* _refresh_runtime_if_revised: a mid-job task edit rebuilds the
  pipeline (spoiler fix) while GridFS-resolved mark asset paths are
  carried over; no revision change -> no rebuild.

Round 3 (2026-11-19 second retest, 2 failures):
* _file_for_download rewind: Telethon's download_media(file=BytesIO())
  leaves the cursor at the END; the returned buffer must be seek(0)
  before send_file, or Telegram rejects the upload with
  FilePartEmptyError. The full download -> re-upload chain is run
  against a client that reads from the current position, proving the
  re-uploaded bytes are complete; an empty download raises
  FileReferenceSendError instead.
* _save_task_offsets / _forward_and_save: a mid-send config edit
  (P6c replay: revision 2 -> 3, status RUNNING -> DRAFT) must still
  save the offset, must not cancel the job, and the next group must
  keep sending. Pause mid-send still stops the job, but the already
  sent group's progress is saved first; a deleted task still cancels;
  a second edit landing between re-read and save is retried once.

Run: python3 tests/test_nb_full_test_round1_fixes.py
"""
import asyncio
import io
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- local env shims: the dev machine lacks the container's full deps ---
import telethon.tl.types as _t

for _name in ("KeyboardButtonCallback", "KeyboardButtonSwitchInline", "KeyboardButtonUrl"):
    if not hasattr(_t, _name):
        setattr(_t, _name, type(_name, (), {}))


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


_stub("nb.multitenant.repository", LOG_BUFFER_MAX_LINES=100, MongoRepository=object)
_stub("nb.multitenant.settings", Settings=object)
_stub("nb.multitenant.security", SessionCipher=object, get_telegram_proxy=lambda *a, **k: None)
_stub("nb.plugins", PluginPipeline=object)

from telethon.errors import FilePartEmptyError, FileReferenceExpiredError, MediaEmptyError, MsgIdInvalidError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from telethon.tl.types import MessageService

import nb.multitenant.worker as worker_mod
from nb.multitenant.worker import (
    FileReferenceSendError,
    TaskStatus,
    TenantWorker,
    _is_file_reference_error,
)

from nb.multitenant.bot_media import (
    _collect_discussion_comments,
    get_discussion_message,
)


# ---------------------------------------------------------------- mark ---

def _load_mark_module():
    """Load nb/plugins/mark.py standalone (its package imports are stubbed)."""
    import importlib.util
    from unittest.mock import MagicMock

    sys.modules.setdefault("nb.multitenant.media_pool", types.ModuleType("nb.multitenant.media_pool"))
    sys.modules["nb.multitenant.media_pool"].run_cpu = MagicMock()
    plugins_stub = types.ModuleType("nb.plugins")
    plugins_stub.NbMessage = object
    plugins_stub.NbPlugin = object
    sys.modules["nb.plugins"] = plugins_stub
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "nb", "plugins", "mark.py",
    )
    spec = importlib.util.spec_from_file_location("mark_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _red_pixels(path):
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        px = list(im.getdata())
    return sum(1 for r, g, b in px if r > 200 and g < 120 and b < 120)


def _make_fixture(tmp):
    from PIL import Image, ImageDraw

    base = os.path.join(tmp, "base.png")
    with Image.new("RGB", (400, 300), (255, 255, 255)) as im:
        im.save(base)
    wm = os.path.join(tmp, "wm.png")
    with Image.new("RGBA", (200, 60), (0, 0, 0, 0)) as im:
        d = ImageDraw.Draw(im)
        # Semi-transparent red (alpha 160): the double-alpha bug squares
        # it to ~100 (too faint to count as red), the fix keeps 160.
        d.text((10, 10), "WATERMARK", fill=(255, 0, 0, 160))
        d.rectangle([10, 40, 190, 55], fill=(255, 0, 0, 160))
        im.save(wm)
    return base, wm


def test_mark_watermark(tmp):
    from PIL import Image

    mark = _load_mark_module()
    base, wm = _make_fixture(tmp)
    # Old behaviour, replicated inline: paste with the RGBA image as mask.
    with Image.open(base).convert("RGBA") as base_img, Image.open(wm).convert("RGBA") as wm_img:
        layer = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
        layer.paste(wm_img, (16, 16), wm_img)
        Image.alpha_composite(base_img, layer).save(os.path.join(tmp, "old.png"), format="PNG")
    old_count = _red_pixels(os.path.join(tmp, "old.png"))
    out = mark._apply_image_watermark(base, wm, "top_left")
    new_count = _red_pixels(out)
    print(f"mark image: old red px={old_count} fixed red px={new_count}")
    assert old_count < 50, old_count
    assert new_count > 300, new_count
    assert new_count > old_count * 10, (old_count, new_count)

    # GIF path: same double-alpha bug, same fix.
    gif = os.path.join(tmp, "anim.gif")
    frames = [
        Image.new("RGB", (400, 300), (255, 255, 255)),
        Image.new("RGB", (400, 300), (200, 220, 255)),
    ]
    frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    out_gif = mark._apply_gif_watermark(gif, wm, "top_left")
    assert out_gif and os.path.exists(out_gif), out_gif
    with Image.open(out_gif) as im:
        assert getattr(im, "n_frames", 1) == 2
        im.seek(0)
        rgb = im.convert("RGB")
        px = list(rgb.getdata())
    gif_red = sum(1 for r, g, b in px if r > 200 and g < 120 and b < 120)
    print(f"mark gif: red px={gif_red}")
    assert gif_red > 100, gif_red
    print("PASS mark watermark (image + gif)")


# --------------------------------------- MediaEmptyError / send fallback ---

class FakeMedia:
    spoiler = False

    def __init__(self, filename=None):
        if filename is not None:
            self.document = types.SimpleNamespace(
                attributes=[types.SimpleNamespace(file_name=filename)]
            )
        else:
            self.document = None


class FakeMsg:
    def __init__(self, id, peer_id=-1003147613741, dl="ok", media=True, filename=None):
        self.id = id
        self.peer_id = peer_id
        self.media = FakeMedia(filename) if media else None
        self.dl = dl


class FakeTemplate:
    def __init__(self, message, new_file=None):
        self.message = message
        self.new_file = new_file
        self.text = ""
        self.reply_markup = None


class FakeSent:
    def __init__(self, id):
        self.id = id


class FakeSendClient:
    """send_file raises MediaEmptyError on the first N calls, then works."""

    def __init__(self, fail_calls=2):
        self.send_calls = []
        self.fail_calls = fail_calls
        self.refetch_map = {}

    async def download_media(self, message, file=None):
        return io.BytesIO(b"bytes-%d" % message.id)

    async def get_messages(self, peer, ids=None):
        if isinstance(ids, list):
            return [FakeMsg(i, peer_id=peer, dl="ok", filename="clip.mp4") for i in ids]
        return FakeMsg(ids, peer_id=peer, dl="ok", filename="clip.mp4")

    async def send_file(self, destination, files, caption=None, reply_to=None, buttons=None):
        self.send_calls.append({"files": files, "caption": caption})
        if len(self.send_calls) <= self.fail_calls:
            raise MediaEmptyError(
                "The provided media object is invalid (caused by SendMultiMediaRequest)"
            )
        return [FakeSent(9000 + i) for i in range(len(files))]

    async def upload_file(self, path):
        return object()


def make_worker(client):
    w = object.__new__(TenantWorker)
    w.client = client
    return w


async def test_media_empty_error():
    assert _is_file_reference_error(
        MediaEmptyError("The provided media object is invalid (caused by SendMultiMediaRequest)")
    )
    chained = ValueError("outer")
    chained.__cause__ = MediaEmptyError("media empty")
    assert _is_file_reference_error(chained)
    assert not _is_file_reference_error(ValueError("boom"))
    print("PASS MediaEmptyError predicate")

    # Full chain: by-ref multi fails, refetch-resend fails the same way,
    # download + re-upload succeeds (probe V3/V5, 2026-11-19).
    c = FakeSendClient(fail_calls=2)
    w = make_worker(c)
    group = [FakeTemplate(FakeMsg(6, dl="ok", filename="clip.mp4"))]
    result = await w._send_files_with_reference_retry(
        -1003860181234, group, ["video caption"]
    )
    assert isinstance(result, list) and len(result) == 1 and result[0] is not None, result
    assert len(c.send_calls) == 3, c.send_calls
    reuploaded = c.send_calls[2]["files"][0]
    assert isinstance(reuploaded, io.BytesIO), type(reuploaded)
    assert getattr(reuploaded, "name", None) == "clip.mp4", getattr(reuploaded, "name", None)
    assert c.send_calls[2]["caption"] == ["video caption"]
    print("PASS MediaEmptyError fallback chain (refetch fails, download re-uploads)")


# ------------------------------------------------------------ schedule ---

class FakeScheduleMsg:
    def __init__(self, id, grouped_id=None):
        self.id = id
        self.grouped_id = grouped_id


class FakeServiceMsg(MessageService):
    def __init__(self, id):
        self.id = id
        self.grouped_id = None


class FakeIterClient:
    def __init__(self, messages):
        self._messages = messages

    async def iter_messages(self, peer, min_id=0, reverse=True):
        for m in self._messages:
            yield m


async def test_schedule_collect():
    w = object.__new__(TenantWorker)
    # S1 shape: service message 1, then text 2, 3, 4; daily_limit=2.
    w.client = FakeIterClient([
        FakeServiceMsg(1),
        FakeScheduleMsg(2),
        FakeScheduleMsg(3),
        FakeScheduleMsg(4),
    ])
    got = await w._collect_schedule_messages(-1004479616069, 0, 2)
    assert [m.id for m in got] == [2, 3], [m.id for m in got]
    print("PASS schedule: service message does not consume budget")

    # Album tail: budget 2, album of 3 must not split.
    w.client = FakeIterClient([
        FakeScheduleMsg(10, grouped_id=7),
        FakeScheduleMsg(11, grouped_id=7),
        FakeScheduleMsg(12, grouped_id=7),
        FakeScheduleMsg(13),
    ])
    got = await w._collect_schedule_messages(-1004479616069, 0, 2)
    assert [m.id for m in got] == [10, 11, 12], [m.id for m in got]
    print("PASS schedule: album never split")

    # Unlimited: service messages skipped, everything else collected.
    w.client = FakeIterClient([
        FakeServiceMsg(1),
        FakeScheduleMsg(2),
        FakeServiceMsg(3),
        FakeScheduleMsg(4),
    ])
    got = await w._collect_schedule_messages(-1004479616069, 0, 0)
    assert [m.id for m in got] == [2, 4], [m.id for m in got]
    print("PASS schedule: unlimited skips service messages")


# ------------------------------------------------------------ bot_media ---

class FakeFullChat:
    def __init__(self, linked):
        self.linked_chat_id = linked


class FakeDiscussionClient:
    """GetDiscussionMessage fails (455's source); the linked group answers."""

    def __init__(self):
        self.linked = 2244996707
        self.mirror_id = 5584832

    async def __call__(self, request):
        if isinstance(request, GetDiscussionMessageRequest):
            raise MsgIdInvalidError(
                "The message ID used in the peer was invalid (caused by GetDiscussionMessageRequest)"
            )
        if isinstance(request, GetFullChannelRequest):
            return types.SimpleNamespace(full_chat=FakeFullChat(self.linked))
        raise AssertionError(type(request).__name__)

    async def get_entity(self, peer):
        return peer

    async def iter_messages(self, peer, limit=None, reply_to=None, reverse=True):
        if reply_to is not None:
            # Replies to the mirror.
            for i in (5584901, 5584902):
                yield types.SimpleNamespace(
                    id=i, raw_text="keyword", text="keyword",
                    reply_to=types.SimpleNamespace(reply_to_msg_id=self.mirror_id),
                    chat_id=self.linked,
                )
            return
        # Linked-group scan: the mirror is the 3rd most recent message.
        for i, cp in ((5584900, None), (5584890, 19309), (self.mirror_id, 19310)):
            yield types.SimpleNamespace(
                id=i,
                fwd_from=types.SimpleNamespace(channel_post=cp) if cp else None,
                raw_text="", text="",
                reply_to=None,
                chat_id=self.linked,
            )


async def test_bot_media_mirror_fallback():
    client = FakeDiscussionClient()
    disc = await get_discussion_message(client, -1002112309458, 19310)
    assert disc is not None and disc.id == 5584832, disc
    print("PASS bot_media: mirror found via linked group after channel-peer failure")

    settings = types.SimpleNamespace(recent_limit=80)
    disc_msg, comments, _hint = await _collect_discussion_comments(
        client, -1002112309458, 19310, settings
    )
    assert disc_msg is not None and disc_msg.id == 5584832, disc_msg
    assert [c.id for c in comments] == [5584901, 5584902], comments
    print("PASS bot_media: comments read via linked-group mirror")


# ------------------------------------------- mid-job runtime refresh ---

class FakeMarkCfg:
    def __init__(self, image, spoiler_check=False):
        self.image = image
        self.ad_intro_path = ""
        self.ad_midroll_path = ""
        self.ad_outro_path = ""
        self.spoiler_check = spoiler_check


class FakePluginsCfg:
    def __init__(self, mark, spoiler_check=False):
        self.mark = mark
        self.spoiler = types.SimpleNamespace(check=spoiler_check)

    def model_copy(self, deep=False):
        import copy

        return copy.deepcopy(self)


class FakeTask:
    def __init__(self, revision, plugins):
        self.id = "task-1"
        self.revision = revision
        self.plugins = plugins


class FakePipeline:
    def __init__(self, config, download_dir=None):
        self.config = config
        self.download_dir = download_dir
        self.initialized = False

    async def initialize(self):
        self.initialized = True


class FakeRefreshRepo:
    def __init__(self, task):
        self._task = task

    def get_task(self, tenant_id, task_id):
        return self._task


async def test_refresh_runtime_if_revised():
    orig_pipeline = worker_mod.PluginPipeline
    worker_mod.PluginPipeline = FakePipeline
    try:
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp(prefix="nb-test-"))
        # (a) Same revision -> no rebuild, same task back.
        old_cfg = FakePluginsCfg(FakeMarkCfg("/tmp/old-watermark.png"), spoiler_check=False)
        runtime = types.SimpleNamespace(
            task=FakeTask(3, old_cfg),
            pipeline=FakePipeline(old_cfg, download_dir=str(tmp)),
            temp_dir=tmp,
        )
        runtime.pipeline.config = old_cfg
        w = object.__new__(TenantWorker)
        w.tenant_id = 1
        w.repo = FakeRefreshRepo(FakeTask(3, old_cfg))
        task = runtime.task
        out = await w._refresh_runtime_if_revised(runtime, task)
        assert out is task and runtime.pipeline.config is old_cfg
        print("PASS refresh: same revision -> no rebuild")

        # (b) Revised (spoiler enabled mid-job) -> pipeline rebuilt from
        # the fresh config, GridFS-resolved mark path carried over.
        fresh_cfg = FakePluginsCfg(FakeMarkCfg("image.png"), spoiler_check=True)
        fresh_task = FakeTask(4, fresh_cfg)
        w.repo = FakeRefreshRepo(fresh_task)
        out = await w._refresh_runtime_if_revised(runtime, task)
        assert out is fresh_task, out
        assert runtime.task is fresh_task
        assert runtime.pipeline.config.spoiler.check is True
        assert runtime.pipeline.config.mark.image == "/tmp/old-watermark.png"
        assert runtime.pipeline.initialized is True
        print("PASS refresh: revised -> pipeline rebuilt, mark path preserved")

        # (c) Task gone -> input task back, no crash.
        w.repo = FakeRefreshRepo(None)
        out = await w._refresh_runtime_if_revised(runtime, fresh_task)
        assert out is fresh_task
        print("PASS refresh: missing task -> no-op")
    finally:
        worker_mod.PluginPipeline = orig_pipeline


# --------------------------------------- round 3: download rewind ---

class RealisticDownloadClient:
    """download_media / send_file the way real Telethon behaves.

    download_media(file=BytesIO()) leaves the buffer's cursor at the
    END (round-2 probe: a 22,189-byte video came back with
    tell()==22189). send_file reads each file from its current
    position, like Telethon's _FileStream, and raises FilePartEmptyError
    when a file yields 0 bytes -- the exact production failure.
    """

    def __init__(self, fail_send_calls=0, empty=False):
        self.fail_send_calls = fail_send_calls
        self.empty = empty
        self.send_calls = []
        self.read_bytes = []

    async def download_media(self, message, file=None):
        if getattr(message, "dl", "ok") == "expired":
            raise FileReferenceExpiredError("FILE_REFERENCE_EXPIRED")
        payload = b"" if self.empty else b"bytes-%d" % message.id
        buf = io.BytesIO(payload)
        buf.seek(len(payload))  # cursor at end, as Telethon leaves it
        return buf

    async def get_messages(self, peer, ids=None):
        if isinstance(ids, list):
            return [FakeMsg(i, peer_id=peer, dl="ok", filename="clip.mp4") for i in ids]
        return FakeMsg(ids, peer_id=peer, dl="ok", filename="clip.mp4")

    async def send_file(self, destination, files, caption=None, reply_to=None, buttons=None):
        self.send_calls.append({"files": files, "caption": caption})
        if len(self.send_calls) <= self.fail_send_calls:
            raise MediaEmptyError(
                "The provided media object is invalid (caused by SendMultiMediaRequest)"
            )
        total = 0
        for f in files:
            if isinstance(f, io.BytesIO):
                chunk = f.read()
                total += len(chunk)
                if not chunk:
                    raise FilePartEmptyError(
                        "The provided file part is empty (caused by SaveFilePartRequest)"
                    )
        self.read_bytes.append(total)
        return [FakeSent(9000 + i) for i in range(len(files))]

    async def upload_file(self, path):
        return object()


async def test_file_for_download_rewinds():
    # Plain download: cursor at end in, rewound to 0 out, bytes intact.
    c = RealisticDownloadClient()
    w = make_worker(c)
    data = await w._file_for_download(
        FakeTemplate(FakeMsg(6, dl="ok", filename="clip.mp4")),
        FakeMsg(6, dl="ok", filename="clip.mp4"),
    )
    assert data.tell() == 0, data.tell()
    assert data.read() == b"bytes-6", data
    print("PASS download rewind: plain download returns BytesIO at 0")

    # Refetch branch: first download expired, refetch + retry download
    # also leaves the cursor at the end -- same rewind must apply.
    c = RealisticDownloadClient()
    w = make_worker(c)
    data = await w._file_for_download(
        FakeTemplate(FakeMsg(7, dl="expired", filename="clip.mp4")),
        FakeMsg(7, dl="expired", filename="clip.mp4"),
    )
    assert data.tell() == 0, data.tell()
    assert data.read() == b"bytes-7", data
    print("PASS download rewind: refetch-retry download also rewound")

    # Empty download: fail here, never reach Telegram as an empty part.
    c = RealisticDownloadClient(empty=True)
    w = make_worker(c)
    try:
        await w._file_for_download(
            FakeTemplate(FakeMsg(8, dl="ok")), FakeMsg(8, dl="ok")
        )
    except FileReferenceSendError:
        pass
    else:
        raise AssertionError("expected FileReferenceSendError (empty download)")
    print("PASS download rewind: empty download -> FileReferenceSendError")


async def test_download_reupload_no_file_part_empty():
    # Full chain, end to end: by-ref multi fails twice with
    # MediaEmptyError, refetch-resend fails the same way, then the
    # download -> re-upload leg runs. Before the fix the third send
    # read 0 bytes from the un-rewound buffer and died with
    # FilePartEmptyError; now the whole payload arrives.
    c = RealisticDownloadClient(fail_send_calls=2)
    w = make_worker(c)
    group = [FakeTemplate(FakeMsg(6, dl="ok", filename="clip.mp4"))]
    result = await w._send_files_with_reference_retry(
        -1003860181234, group, ["video caption"]
    )
    assert isinstance(result, list) and len(result) == 1 and result[0] is not None, result
    assert len(c.send_calls) == 3, c.send_calls
    assert c.read_bytes == [len(b"bytes-6")], c.read_bytes
    print("PASS download re-upload: full bytes sent, no FilePartEmptyError")


# --------------------------------- round 3: mid-job edit vs offsets ---

class FakeOffsetRepo:
    """get_task / update_task_offsets / cancel_job with real semantics:
    the offset write matches on revision only (the round-3 repository
    change: no ACTIVE/RUNNING status filter -- progress must survive a
    mid-job edit or pause)."""

    def __init__(self, revision, status, *, bump_on_first_save=False):
        self.revision = revision
        self.status = status  # None = task deleted
        self.deleted = False
        self.bump_on_first_save = bump_on_first_save
        self.save_calls = []
        self.saved_offsets = None
        self.cancelled = []
        self._save_attempts = 0

    def get_task(self, tenant_id, task_id):
        if self.deleted or self.status is None:
            return None
        return types.SimpleNamespace(
            id=task_id, revision=self.revision, status=self.status
        )

    def update_task_offsets(self, tenant_id, task_id, offsets, *, expected_revision, lease_epoch=None):
        self.save_calls.append(expected_revision)
        self._save_attempts += 1
        if self.bump_on_first_save and self._save_attempts == 1:
            self.revision += 1  # a second edit lands between re-read and save
            return False
        if expected_revision != self.revision:
            return False
        self.saved_offsets = dict(offsets)
        return True

    def cancel_job(self, job_id, *, worker_id, lease_epoch=None):
        self.cancelled.append(job_id)
        return True


def make_save_worker(repo):
    w = object.__new__(TenantWorker)
    w.tenant_id = 1
    w.repo = repo
    w.epoch = 1
    w.settings = types.SimpleNamespace(worker_id="worker-1")
    w.client = object()
    return w


def _save_task_and_runtime(stale_revision=2):
    task = types.SimpleNamespace(
        id="task-1",
        revision=stale_revision,
        status=TaskStatus.RUNNING,
        comments=types.SimpleNamespace(
            merge_comment_media=False,
            enabled=False,
            bot_media=types.SimpleNamespace(recent_limit=80),
        ),
    )
    runtime = types.SimpleNamespace(task=task, dest_without_discussion=False)
    return task, runtime


def _send_with_mutation(repo, *, revision=None, status=None, delete=False):
    async def _send(runtime, source_id, group, live=False):
        if delete:
            repo.deleted = True
        else:
            if revision is not None:
                repo.revision = revision
            if status is not None:
                repo.status = status
        return 1  # sent_count

    return _send


async def test_forward_and_save_mid_job_edit():
    source_id = -1004479616069
    group = [FakeMsg(10), FakeMsg(11)]
    job = {"_id": "job-1"}

    # (a) P6c replay: spoiler flipped mid-send (rev 2 -> 3, RUNNING ->
    # DRAFT, PUT-only edit). Offset must save, job must NOT cancel.
    repo = FakeOffsetRepo(2, TaskStatus.RUNNING)
    w = make_save_worker(repo)
    w._forward_messages = _send_with_mutation(
        repo, revision=3, status=TaskStatus.DRAFT
    )
    task, runtime = _save_task_and_runtime()
    offsets = {}
    ok = await w._forward_and_save(job, task, runtime, source_id, group, offsets)
    assert ok is True, ok
    assert repo.saved_offsets == {str(source_id): 11}, repo.saved_offsets
    assert offsets == {str(source_id): 11}, offsets
    assert repo.cancelled == [], repo.cancelled
    assert repo.save_calls == [3], repo.save_calls  # fresh revision, not stale 2
    print("PASS offset save: mid-job edit (rev 2->3, DRAFT) saves + job continues")

    # (b) No edit: unchanged revision, single save, no cancel.
    repo = FakeOffsetRepo(2, TaskStatus.RUNNING)
    w = make_save_worker(repo)
    w._forward_messages = _send_with_mutation(repo)
    task, runtime = _save_task_and_runtime()
    offsets = {}
    ok = await w._forward_and_save(job, task, runtime, source_id, group, offsets)
    assert ok is True and repo.saved_offsets == {str(source_id): 11}
    assert repo.save_calls == [2] and repo.cancelled == []
    print("PASS offset save: no edit -> one save with the same revision")

    # (c) Paused mid-send: job stops, BUT the already-sent group's
    # progress is saved first (no silent re-send on re-run).
    repo = FakeOffsetRepo(2, TaskStatus.RUNNING)
    w = make_save_worker(repo)
    w._forward_messages = _send_with_mutation(
        repo, revision=3, status=TaskStatus.PAUSED
    )
    task, runtime = _save_task_and_runtime()
    offsets = {}
    ok = await w._forward_and_save(job, task, runtime, source_id, group, offsets)
    assert ok is False, ok
    assert repo.saved_offsets == {str(source_id): 11}, repo.saved_offsets
    assert repo.cancelled == ["job-1"], repo.cancelled
    print("PASS offset save: mid-send pause stops the job, progress still saved")

    # (d) Task deleted mid-send: no save possible, job cancelled.
    repo = FakeOffsetRepo(2, TaskStatus.RUNNING)
    w = make_save_worker(repo)
    w._forward_messages = _send_with_mutation(repo, delete=True)
    task, runtime = _save_task_and_runtime()
    offsets = {}
    ok = await w._forward_and_save(job, task, runtime, source_id, group, offsets)
    assert ok is False, ok
    assert repo.saved_offsets is None and repo.cancelled == ["job-1"]
    print("PASS offset save: task deleted mid-send -> job cancelled")

    # (e) Revision churn: a second edit lands between the re-read and
    # the write (first save fails) -> one retry with the new revision
    # succeeds, job continues, no cancel.
    repo = FakeOffsetRepo(2, TaskStatus.RUNNING, bump_on_first_save=True)
    w = make_save_worker(repo)
    w._forward_messages = _send_with_mutation(repo)
    task, runtime = _save_task_and_runtime()
    offsets = {}
    ok = await w._forward_and_save(job, task, runtime, source_id, group, offsets)
    assert ok is True, ok
    assert repo.save_calls == [2, 3], repo.save_calls
    assert repo.saved_offsets == {str(source_id): 11}
    assert repo.cancelled == [], repo.cancelled
    print("PASS offset save: revision churn between re-read and save -> retried once")

    # (f) The stop predicate itself: only deleted / paused / error /
    # completed stop a job; DRAFT/VALIDATING (edit in flight) do not.
    assert TenantWorker._job_task_stopped(None) is True
    for stopped in (TaskStatus.PAUSED, TaskStatus.ERROR, TaskStatus.COMPLETED):
        assert TenantWorker._job_task_stopped(
            types.SimpleNamespace(status=stopped)
        ) is True, stopped
    for cont in (
        TaskStatus.RUNNING,
        TaskStatus.ACTIVE,
        TaskStatus.DRAFT,
        TaskStatus.VALIDATING,
    ):
        assert TenantWorker._job_task_stopped(
            types.SimpleNamespace(status=cont)
        ) is False, cont
    print("PASS offset save: stop predicate (DRAFT/VALIDATING continue)")


async def main():
    import tempfile

    with tempfile.TemporaryDirectory(prefix="nb-mark-test-") as tmp:
        test_mark_watermark(tmp)
    await test_media_empty_error()
    await test_schedule_collect()
    await test_bot_media_mirror_fallback()
    await test_refresh_runtime_if_revised()
    await test_file_for_download_rewinds()
    await test_download_reupload_no_file_part_empty()
    await test_forward_and_save_mid_job_edit()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

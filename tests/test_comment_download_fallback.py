"""Mock tests for the worker comment-sync download fallback.

Covers, with a fake Telegram client (no network, no Mongo):

* _is_file_reference_error: bare FileReferenceExpiredError, the indexed
  FILE_REFERENCE_n_EXPIRED form (plain error carrying the substring),
  and the download-path AttributeError chained to FileReferenceExpiredError.
* _file_for_download: refetch success (bare + chained AttributeError),
  refetch None -> FileReferenceSendError, retry still fails ->
  FileReferenceSendError, None message -> FileReferenceSendError (no
  AttributeError leak), non-reference error propagates, download
  returning None -> FileReferenceSendError, plain download ok.
* _send_files_with_reference_retry (download path): one bad item is
  skipped and the rest still send (aligned return: None for skipped);
  all items failing -> FileReferenceSendError; non-reference download
  error propagates. Also sanity: plain send and refetch-resend paths.

Run: python3 tests/test_comment_download_fallback.py
"""
import asyncio
import io
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- local env shims: the dev machine lacks the container's full deps ---
# telethon here has no KeyboardButtonCallback / SwitchInline / Url.
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

from telethon.errors import FileReferenceExpiredError

from nb.multitenant.worker import (
    FileReferenceSendError,
    TenantWorker,
    _is_file_reference_error,
)


class FakeMedia:
    spoiler = False


class FakeMsg:
    def __init__(self, id, peer_id=-1003147613741, dl="ok", media=True):
        self.id = id
        self.peer_id = peer_id
        self.media = FakeMedia() if media else None
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


class FakeClient:
    """Fake Telegram client. download behaviour is driven per message
    by its .dl flag; refetch behaviour per id via refetch_map."""

    def __init__(self):
        self.send_calls = []
        self.send_fail_first = False
        self.refetch_map = {}  # id -> "ok" | "none" | "nomedia" | "expired"
        self.refetch_group_ok = False

    async def download_media(self, message, file=None):
        if message is None:
            raise AttributeError("'NoneType' object has no attribute 'media'")
        dl = getattr(message, "dl", "ok")
        if dl == "ok":
            return io.BytesIO(b"bytes-%d" % message.id)
        if dl == "expired":
            raise FileReferenceExpiredError("FILE_REFERENCE_EXPIRED")
        if dl == "attr":
            err = AttributeError("'NoneType' object has no attribute 'media'")
            err.__context__ = FileReferenceExpiredError("FILE_REFERENCE_EXPIRED")
            raise err
        if dl == "none":
            return None
        if dl == "value":
            raise ValueError("boom")
        raise AssertionError(dl)

    async def get_messages(self, peer, ids=None):
        if isinstance(ids, int):
            mode = self.refetch_map.get(ids, "ok")
            if mode == "none":
                return None
            if mode == "nomedia":
                return FakeMsg(ids, peer_id=peer, dl="ok", media=False)
            if mode == "expired":
                return FakeMsg(ids, peer_id=peer, dl="expired")
            return FakeMsg(ids, peer_id=peer, dl="ok")
        if isinstance(ids, list):
            if self.refetch_group_ok:
                return [FakeMsg(i, peer_id=peer, dl="ok") for i in ids]
            return []
        return None

    async def send_file(self, destination, files, caption=None, reply_to=None, buttons=None):
        self.send_calls.append(
            {"destination": destination, "files": files, "caption": caption}
        )
        if self.send_fail_first and len(self.send_calls) == 1:
            raise FileReferenceExpiredError("FILE_REFERENCE_EXPIRED")
        return [FakeSent(9000 + i) for i in range(len(files))]

    async def upload_file(self, path):
        return object()


def make_worker(client):
    w = object.__new__(TenantWorker)
    w.client = client
    return w


async def test_is_file_reference_error():
    assert _is_file_reference_error(FileReferenceExpiredError("FILE_REFERENCE_EXPIRED"))
    assert _is_file_reference_error(Exception("FILE_REFERENCE_0_EXPIRED"))
    err = AttributeError("'NoneType' object has no attribute 'media'")
    err.__context__ = FileReferenceExpiredError("FILE_REFERENCE_EXPIRED")
    assert _is_file_reference_error(err)
    assert not _is_file_reference_error(AttributeError("something else"))
    assert not _is_file_reference_error(ValueError("boom"))
    print("PASS _is_file_reference_error")


async def test_file_for_download():
    # 1. refetch success (bare expired error)
    c = FakeClient()
    w = make_worker(c)
    data = await w._file_for_download(FakeTemplate(FakeMsg(1, dl="expired")), FakeMsg(1, dl="expired"))
    assert data.read() == b"bytes-1", data
    # 2. refetch success (chained AttributeError, the production shape)
    c = FakeClient()
    w = make_worker(c)
    data = await w._file_for_download(FakeTemplate(FakeMsg(2, dl="attr")), FakeMsg(2, dl="attr"))
    assert data.read() == b"bytes-2", data
    # 3. refetch None -> FileReferenceSendError
    c = FakeClient()
    c.refetch_map = {3: "none"}
    w = make_worker(c)
    try:
        await w._file_for_download(FakeTemplate(FakeMsg(3, dl="expired")), FakeMsg(3, dl="expired"))
    except FileReferenceSendError:
        pass
    else:
        raise AssertionError("expected FileReferenceSendError (refetch None)")
    # 4. retry still fails -> FileReferenceSendError
    c = FakeClient()
    c.refetch_map = {4: "expired"}
    w = make_worker(c)
    try:
        await w._file_for_download(FakeTemplate(FakeMsg(4, dl="expired")), FakeMsg(4, dl="expired"))
    except FileReferenceSendError:
        pass
    else:
        raise AssertionError("expected FileReferenceSendError (retry failed)")
    # 5. None message -> FileReferenceSendError, no AttributeError leak
    c = FakeClient()
    w = make_worker(c)
    try:
        await w._file_for_download(FakeTemplate(None), None)
    except FileReferenceSendError:
        pass
    except AttributeError:
        raise AssertionError("AttributeError leaked for None message")
    else:
        raise AssertionError("expected FileReferenceSendError (None message)")
    # 6. non-reference error propagates untouched
    c = FakeClient()
    w = make_worker(c)
    try:
        await w._file_for_download(FakeTemplate(FakeMsg(6, dl="value")), FakeMsg(6, dl="value"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError to propagate")
    # 7. download returning None -> FileReferenceSendError
    c = FakeClient()
    w = make_worker(c)
    try:
        await w._file_for_download(FakeTemplate(FakeMsg(7, dl="none")), FakeMsg(7, dl="none"))
    except FileReferenceSendError:
        pass
    else:
        raise AssertionError("expected FileReferenceSendError (download None)")
    # 8. plain download ok, no refetch
    c = FakeClient()
    w = make_worker(c)
    data = await w._file_for_download(FakeTemplate(FakeMsg(8, dl="ok")), FakeMsg(8, dl="ok"))
    assert data.read() == b"bytes-8", data
    print("PASS _file_for_download (8 cases)")


async def test_send_files_per_item():
    # One bad item skipped, rest sent, aligned return.
    c = FakeClient()
    c.send_fail_first = True
    c.refetch_map = {12: "none"}  # item 2 refetch fails -> skipped
    w = make_worker(c)
    group = [
        FakeTemplate(FakeMsg(11, dl="ok")),
        FakeTemplate(FakeMsg(12, dl="expired")),
        FakeTemplate(FakeMsg(13, dl="ok")),
    ]
    result = await w._send_files_with_reference_retry(
        -1003763110632, group, ["c1", "c2", "c3"]
    )
    assert isinstance(result, list) and len(result) == 3, result
    assert result[0] is not None and result[1] is None and result[2] is not None, result
    assert len(c.send_calls) == 2, c.send_calls  # first send + download-path send
    assert len(c.send_calls[1]["files"]) == 2, c.send_calls[1]
    assert c.send_calls[1]["caption"] == ["c1", "c3"], c.send_calls[1]["caption"]
    print("PASS _send_files per-item skip (aligned [sent, None, sent])")

    # All items fail -> FileReferenceSendError.
    c = FakeClient()
    c.send_fail_first = True
    c.refetch_map = {21: "none", 22: "none"}
    w = make_worker(c)
    group = [
        FakeTemplate(FakeMsg(21, dl="expired")),
        FakeTemplate(FakeMsg(22, dl="expired")),
    ]
    try:
        await w._send_files_with_reference_retry(-1003763110632, group, ["c1", "c2"])
    except FileReferenceSendError:
        pass
    else:
        raise AssertionError("expected FileReferenceSendError (all failed)")
    print("PASS _send_files all-fail -> FileReferenceSendError")

    # Non-reference download error propagates (not swallowed as skip).
    c = FakeClient()
    c.send_fail_first = True
    w = make_worker(c)
    group = [FakeTemplate(FakeMsg(31, dl="value"))]
    try:
        await w._send_files_with_reference_retry(-1003763110632, group, ["c1"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError to propagate")
    print("PASS _send_files non-reference error propagates")

    # Plain send ok (no fallback) and refetch-resend path still work.
    c = FakeClient()
    w = make_worker(c)
    group = [FakeTemplate(FakeMsg(41, dl="ok"))]
    result = await w._send_files_with_reference_retry(-1003763110632, group, ["c1"])
    assert len(result) == 1 and result[0] is not None, result
    c = FakeClient()
    c.send_fail_first = True
    c.refetch_group_ok = True
    w = make_worker(c)
    group = [FakeTemplate(FakeMsg(42, dl="ok"))]
    result = await w._send_files_with_reference_retry(-1003763110632, group, ["c1"])
    assert len(result) == 1 and result[0] is not None, result
    print("PASS _send_files plain + refetch-resend paths")


async def main():
    await test_is_file_reference_error()
    await test_file_for_download()
    await test_send_files_per_item()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

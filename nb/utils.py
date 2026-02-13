# ... [前面的代码保持不变] ...

def clean_session_files():
    for item in os.listdir():
        if item.endswith(".session") or item.endswith(".session-journal"):
            os.remove(item)


# === 新增函数 ===
import time
import nb.storage as st

async def wait_for_dest_post_id(src_channel_id: int, src_post_id: int, dest_channel_id: int, timeout=60) -> Optional[int]:
    """等待直到目标帖子ID映射建立，最多等待timeout秒"""
    start = time.time()
    while time.time() - start < timeout:
        dest_post_id = st.get_dest_post_id(src_channel_id, src_post_id, dest_channel_id)
        if dest_post_id is not None:
            return dest_post_id
        await asyncio.sleep(1)
    return None

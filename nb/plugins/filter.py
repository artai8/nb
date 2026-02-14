from nb.plugins import NbMessage, NbPlugin
from nb.utils import match


class NbFilter(NbPlugin):
    id_ = "filter"

    def __init__(self, data):
        self.filters = data
        textf = self.filters.text
        if not textf.case_sensitive and not textf.regex:
            textf.blacklist = [i.lower() for i in textf.blacklist]
            textf.whitelist = [i.lower() for i in textf.whitelist]

    def modify(self, tm):
        if self.users_safe(tm) and self.files_safe(tm) and self.text_safe(tm):
            return tm
        return None

    def modify_group(self, tms):
        """媒体组过滤 — 修复版：基于整组文本判断，不逐条过滤文本"""
        if not tms:
            return tms

        # 收集整个媒体组的合并文本用于文本过滤判断
        combined_text = " ".join(
            tm.text for tm in tms if tm.text and tm.text.strip()
        )

        # 用合并文本做一次文本安全检查
        group_text_safe = self._check_text_safe(combined_text)

        result = []
        for tm in tms:
            # 用户过滤和文件过滤仍然逐条检查
            if not self.users_safe(tm):
                continue
            if not self.files_safe(tm):
                continue
            # 文本过滤：使用整组的判断结果，而不是逐条判断
            # 这样没有文字的图片/视频不会被误删
            if not group_text_safe:
                continue
            result.append(tm)
        return result

    def _check_text_safe(self, text):
        """检查文本是否安全（用于整组判断）"""
        flist = self.filters.text
        if not flist.case_sensitive:
            text = text.lower() if text else ""

        # 没有文本且没有白名单 → 安全
        if not text and not flist.whitelist:
            return True
        # 没有文本但有白名单 → 看白名单是否要求有内容
        if not text and flist.whitelist:
            return False

        # 黑名单检查
        for f in flist.blacklist:
            if match(f, text, flist.regex):
                return False
        # 白名单检查
        if not flist.whitelist:
            return True
        return any(match(a, text, flist.regex) for a in flist.whitelist)

    def text_safe(self, tm):
        """单条消息文本安全检查 — 修复版：有媒体但无文本的消息直接放行"""
        flist = self.filters.text
        text = tm.text if flist.case_sensitive else tm.text.lower()

        # 关键修复：没有文本但有媒体文件 → 放行（不要因为缺少文本就删掉图片/视频）
        if not text:
            if tm.file_type != "nofile":
                return True
            if not flist.whitelist:
                return True
            return False

        for f in flist.blacklist:
            if match(f, text, flist.regex):
                return False
        if not flist.whitelist:
            return True
        return any(match(a, text, flist.regex) for a in flist.whitelist)

    def users_safe(self, tm):
        flist = self.filters.users
        sender = str(tm.sender_id)
        if sender in flist.blacklist:
            return False
        if not flist.whitelist:
            return True
        return sender in flist.whitelist

    def files_safe(self, tm):
        flist = self.filters.files
        ft = tm.file_type
        bl = [f.value if hasattr(f, 'value') else f for f in flist.blacklist]
        wl = [f.value if hasattr(f, 'value') else f for f in flist.whitelist]
        if ft in bl:
            return False
        if not wl:
            return True
        return ft in wl

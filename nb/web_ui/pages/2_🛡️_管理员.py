# nb/web_ui/pages/2_🛡️_管理员.py
"""Super-Admin Multi-Tenant Control Panel (Restricted to Admin Only)."""

import os
import streamlit as st
from nb.config import read_config, write_config
from nb.multitenant.repository import MongoRepository
from nb.web_ui.password import check_password
from nb.web_ui.utils import render_jelly_hero, switch_theme

CONFIG = read_config()

st.set_page_config(page_title="管理员后台", page_icon="🛡️", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    # 普通租户无权查看管理员页面
    if not st.session_state.get("is_admin", False):
        st.warning("当前账号无管理员权限。")
        st.stop()

    render_jelly_hero(
        st,
        "管理员总控后台",
        "管理所有租户账号、转发任务、8大插件配置及自定义管理员账号与入口路径",
        icon="shield",
    )

    tab_tenants, tab_tasks, tab_security = st.tabs([
        "1. 全部租户管理 (新增 / 启停 / 密码重置)",
        "2. 跨租户任务与插件配置管理",
        "3. 自定义管理员账号、密码与入口路径",
    ])

    mongo_uri = os.getenv("NB_MONGO_URI", "mongodb://localhost:27017")
    mongo_db = os.getenv("NB_MONGO_DB", "nb_multitenant")
    tenants_data = []
    repo = None
    try:
        repo = MongoRepository(mongo_uri, mongo_db)
        tenants_data = repo.list_all_tenants_detailed()
    except Exception:
        tenants_data = []

    with tab_tenants:
        with st.expander("＋ 新增租户账号", expanded=False):
            with st.form("admin_create_tenant_form"):
                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    new_u = st.text_input("登录用户名")
                with c2:
                    new_d = st.text_input("显示名称 / 备注")
                with c3:
                    new_tid = st.number_input("指定租户 ID (0=自动生成)", min_value=0, value=0)
                with c4:
                    new_p = st.text_input("初始登录密码", type="password")
                if st.form_submit_button("创建租户"):
                    if repo and new_u and new_p:
                        try:
                            repo.register_web_tenant(
                                new_u,
                                new_p,
                                display_name=new_d,
                                tenant_id=int(new_tid) if new_tid > 0 else None,
                            )
                            st.success(f"已创建租户 @{new_u}")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                    else:
                        users_db = CONFIG.setdefault("registered_users", {})
                        users_db[new_u] = {"password": new_p, "display_name": new_d, "status": "active"}
                        write_config(CONFIG)
                        st.success(f"已保存租户 @{new_u}")

        if tenants_data:
            for t in tenants_data:
                tid = t["tenant_id"]
                st.markdown(
                    f"""
                    <div class="glass-card">
                      <strong>租户 #{tid} · @{t['username']}</strong> ({t.get('display_name') or '无备注'})
                      — 状态: <code>{t.get('status', 'active')}</code>
                      · 任务总数: <strong>{t.get('task_count', 0)}</strong>
                      · 运行中: <strong>{t.get('active_task_count', 0)}</strong>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.json(CONFIG.get("registered_users", {}))

    with tab_tasks:
        st.subheader("选择任意租户查看并修改其转发任务与插件配置")
        if tenants_data and repo:
            tenant_options = {f"#{t['tenant_id']} @{t['username']}": t["tenant_id"] for t in tenants_data}
            selected_label = st.selectbox("目标租户", options=list(tenant_options.keys()))
            target_tid = tenant_options[selected_label]
            t_tasks = repo.list_tasks(int(target_tid))
            for tk in t_tasks:
                with st.expander(f"任务：{tk.name} ({tk.mode.value} · {tk.status.value})"):
                    st.json(tk.model_dump(mode="json"))
        else:
            st.json(CONFIG.get("connections", []))

    with tab_security:
        with st.form("custom_admin_settings_form"):
            cur_adm_user = str(CONFIG.get("admin_username") or "admin")
            cur_adm_path = str(CONFIG.get("admin_path") or "/admin")
            new_adm_user = st.text_input("管理员登录用户名 (自由更改，不再固定为 admin)", value=cur_adm_user)
            new_adm_path = st.text_input("管理后台专属访问路径 (如 /admin 或 /super-admin)", value=cur_adm_path)
            new_adm_pwd = st.text_input("新管理员密码 (留空则保持原密码)", type="password")
            if st.form_submit_button("保存管理员账号与入口路径设置"):
                CONFIG["admin_username"] = new_adm_user.strip() or "admin"
                CONFIG["admin_path"] = new_adm_path.strip() or "/admin"
                if new_adm_pwd:
                    CONFIG["password"] = new_adm_pwd
                write_config(CONFIG)
                if repo:
                    repo.update_admin_settings(
                        admin_username=CONFIG["admin_username"],
                        admin_password=new_adm_pwd or None,
                        admin_path=CONFIG["admin_path"],
                    )
                st.success(f"已更新：管理员用户名={CONFIG['admin_username']}，专属入口路径={CONFIG['admin_path']}")

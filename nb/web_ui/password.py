# nb/web_ui/password.py
"""Liquid Glass Login & User Registration Gate for Streamlit Web UI."""

from nb.config import PASSWORD, read_config, write_config
from nb.web_ui.utils import jelly_svg


def check_password(st) -> bool:
    """Returns `True` if the user logged in or registered an account."""
    if st.session_state.get("password_correct", False):
        return True

    cfg = read_config()
    admin_user_cfg = str(cfg.get("admin_username") or "admin").strip()
    expected_pwd = str(cfg.get("password") or PASSWORD)
    users_db: dict = cfg.setdefault("registered_users", {})

    st.markdown(
        f"""
        <div class="iridescent-card" style="max-width: 460px; margin: 36px auto 18px; text-align: center;">
          <div style="display:flex; justify-content:center; margin-bottom:12px;">
            {jelly_svg("shield", 52)}
          </div>
          <h2 style="margin:0 0 6px; font-size:1.35rem; font-weight:800; color:#1d1e2c;">账号登录与注册</h2>
          <p style="margin:0; font-size:0.86rem; color:#5a607a;">请输入账号密码登录，或注册新租户账号</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_l, col_m, col_r = st.columns([1, 1.5, 1])
    with col_m:
        tab_login, tab_reg = st.tabs(["账号登录", "新用户注册"])

        with tab_login:
            with st.form("st_gate_login_form"):
                login_user = st.text_input("用户名", value="")
                login_pwd = st.text_input("密码", type="password")
                submitted = st.form_submit_button("登录进入控制台")
                if submitted:
                    u = login_user.strip()
                    if u == admin_user_cfg and login_pwd == expected_pwd:
                        st.session_state["password_correct"] = True
                        st.session_state["current_user"] = u
                        st.session_state["is_admin"] = True
                        st.rerun()
                    elif u in users_db and users_db[u].get("password") == login_pwd:
                        st.session_state["password_correct"] = True
                        st.session_state["current_user"] = u
                        st.session_state["is_admin"] = bool(users_db[u].get("is_admin", False))
                        st.rerun()
                    else:
                        st.error("用户名或密码错误")

        with tab_reg:
            with st.form("st_gate_register_form"):
                reg_user = st.text_input("设置用户名")
                reg_display = st.text_input("显示名称 / 备注 (选填)")
                reg_tid = st.text_input("绑定 Telegram User ID (选填)")
                reg_pwd = st.text_input("设置密码", type="password")
                reg_pwd2 = st.text_input("确认密码", type="password")
                reg_submit = st.form_submit_button("立即注册并登录")
                if reg_submit:
                    u = reg_user.strip()
                    if not u or not reg_pwd:
                        st.error("请填写用户名和密码")
                    elif reg_pwd != reg_pwd2:
                        st.error("两次输入的密码不一致")
                    elif u.lower() == admin_user_cfg.lower() or u in users_db:
                        st.error("该用户名不可用或已存在")
                    else:
                        users_db[u] = {
                            "password": reg_pwd,
                            "display_name": reg_display.strip() or u,
                            "tenant_id": reg_tid.strip(),
                            "status": "active",
                            "is_admin": False,
                        }
                        cfg["registered_users"] = users_db
                        write_config(cfg)
                        st.session_state["password_correct"] = True
                        st.session_state["current_user"] = u
                        st.session_state["is_admin"] = False
                        st.rerun()

    return False

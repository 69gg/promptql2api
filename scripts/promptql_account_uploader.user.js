// ==UserScript==
// @name         PromptQL 账号上交器
// @namespace    https://github.com/promptql2api
// @version      0.2.1
// @description  在 prompt.ql.app 自动提取 hasura-lux cookie（auth.pro.ql.app 域 httpOnly）与 project 信息，上交到 promptql2api 的 /admin 端点。需 Beta 版 Tampermonkey 以支持 httpOnly；自动失败时引导 DevTools 手动粘贴。
// @author       Null
// @match        https://prompt.ql.app/*
// @match        https://auth.pro.ql.app/*
// @match        https://pro.ql.app/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        GM_notification
// @grant        GM_cookie
// @grant        GM_xmlhttpRequest
// @connect      data.pro.ql.app
// @connect      auth.pro.ql.app
// @connect      pro.ql.app
// @connect      *                 # 兜底：ADMIN_URL 可能是任意内网/本地地址（上传走 GM_xmlhttpRequest）
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';

    // ---------------------------- 配置 ----------------------------
    const CONFIG = {
        ADMIN_URL: GM_getValue('ADMIN_URL', 'http://localhost:8088'),
        ADMIN_AUTH_KEY: GM_getValue('ADMIN_AUTH_KEY', ''),
    };

    const CONSOLE_GQL_URL = 'https://data.pro.ql.app/v1/graphql';
    const UPLOADED_KEY = 'uploaded_project_ids';
    const MANUAL_LUX_KEY = 'manual_hasura_lux';

    // hasura-lux 是 host-only 于 auth.pro.ql.app 的 httpOnly cookie，
    // 当前页 prompt.ql.app 的 document.cookie 读不到，必须用 GM_cookie 跨域读。
    // 顺序回退：先 auth.pro.ql.app（实测 cookie 落点），再 pro.ql.app，最后当前页。
    const LUX_COOKIE_URLS = [
        'https://auth.pro.ql.app/',
        'https://pro.ql.app/',
        'https://prompt.ql.app/',
    ];

    // 仅在 prompt.ql.app 渲染主 UI（避免脚本注入 auth.pro.ql.app/pro.ql.app 时重复加按钮）
    function isMainHost() {
        return location.hostname.endsWith('prompt.ql.app');
    }

    // ---------------------------- 设置菜单 ----------------------------
    GM_registerMenuCommand('设置 ADMIN_URL', () => {
        const v = prompt('请输入 promptql2api 网关地址（默认 http://localhost:8088）:', CONFIG.ADMIN_URL);
        if (v !== null) {
            GM_setValue('ADMIN_URL', v.trim());
            CONFIG.ADMIN_URL = v.trim();
            toast('ADMIN_URL 已保存');
        }
    });

    GM_registerMenuCommand('设置 ADMIN_AUTH_KEY', () => {
        const v = prompt('请输入 /admin 管理端点的 auth key:', CONFIG.ADMIN_AUTH_KEY);
        if (v !== null) {
            GM_setValue('ADMIN_AUTH_KEY', v.trim());
            CONFIG.ADMIN_AUTH_KEY = v.trim();
            toast('ADMIN_AUTH_KEY 已保存');
        }
    });

    GM_registerMenuCommand('手动粘贴 hasura-lux', () => {
        const v = promptManualLux();
        if (v) toast('hasura-lux 已缓存，点击「上交账号」即可使用');
    });

    // ---------------------------- 工具函数 ----------------------------
    function toast(msg, type = 'info') {
        console.log('[PromptQL账号上交器]', msg);
        if (typeof GM_notification === 'function') {
            GM_notification({
                title: 'PromptQL 账号上交器',
                text: msg,
                timeout: 4000,
            });
        }
        if (!isMainHost()) return; // 非 prompt.ql.app 不渲染页面浮动提示
        const el = document.createElement('div');
        el.textContent = msg;
        el.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 999999;
            padding: 12px 16px;
            border-radius: 8px;
            color: #fff;
            font-size: 14px;
            font-family: system-ui, sans-serif;
            background: ${type === 'error' ? '#e53935' : '#43a047'};
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
            transition: opacity 0.5s ease;
        `;
        document.body.appendChild(el);
        setTimeout(() => {
            el.style.opacity = '0';
            setTimeout(() => el.remove(), 500);
        }, 3500);
    }

    /** 用 GM_cookie.list 从指定 url 读取单个 cookie（Promise 化）。Beta 版 Tampermonkey 才能读 httpOnly。 */
    function gmCookieGet(url, name) {
        return new Promise((resolve) => {
            if (typeof GM_cookie === 'undefined' || !GM_cookie || typeof GM_cookie.list !== 'function') {
                resolve(null);
                return;
            }
            try {
                // GM_cookie.list 是单 callback(cookies, error)：错误经第二参数报告
                GM_cookie.list({ url, name }, (cookies, error) => {
                    if (error) { // 跨域未授权 / 权限未开 / 非 Beta 读不到 httpOnly
                        console.warn(`GM_cookie.list(${url}) 失败:`, error);
                        resolve(null);
                        return;
                    }
                    const c = (cookies || []).find((x) => x.name === name);
                    resolve(c && c.value ? c : null);
                });
            } catch (e) { // 某些扩展在沙箱外抛同步异常
                console.warn('GM_cookie.list 抛异常:', e);
                resolve(null);
            }
        });
    }

    /** 读取 hasura-lux：GM_cookie 多候选 url 回退 → document.cookie。返回 {value, source}。 */
    async function getCookie(name) {
        for (const url of LUX_COOKIE_URLS) {
            const c = await gmCookieGet(url, name);
            if (c) return { value: c.value, source: `GM_cookie(${url})` };
        }
        const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
        if (m) return { value: decodeURIComponent(m[1]), source: 'document.cookie' };
        return { value: '', source: '' };
    }

    /** 自动读取失败时，引导用户从 DevTools 复制 hasura-lux 值并粘贴。 */
    function promptManualLux() {
        const cached = GM_getValue(MANUAL_LUX_KEY, '');
        const msg =
            '自动读取 hasura-lux 失败（httpOnly + 跨 auth.pro.ql.app 域，常见于非 Beta 版油猴）。\n\n' +
            '请手动获取并粘贴：\n' +
            '1) F12 打开开发者工具 → Application(应用) 面板\n' +
            '2) 左侧 Cookies → 选择 https://auth.pro.ql.app\n' +
            '3) 找到 hasura-lux，复制其 Value 列整段值\n' +
            '4) 粘贴到下方输入框\n\n' +
            '(仅缓存在本机，便于下次续期)';
        const v = prompt(msg, cached);
        if (v === null) return '';
        const trimmed = v.trim();
        if (trimmed) GM_setValue(MANUAL_LUX_KEY, trimmed);
        else GM_setValue(MANUAL_LUX_KEY, '');
        return trimmed;
    }

    function isoNow() {
        return new Date().toISOString().replace(/\.\d{3}Z$/, '');
    }

    function genName(email, projectName) {
        const base = email ? email.split('@')[0].replace(/[^a-zA-Z0-9_-]/g, '_') : projectName;
        return base || 'unknown';
    }

    function getUploadedIds() {
        try {
            return JSON.parse(GM_getValue(UPLOADED_KEY, '[]'));
        } catch {
            return [];
        }
    }

    function markUploaded(projectId) {
        const ids = getUploadedIds();
        if (!ids.includes(projectId)) {
            ids.push(projectId);
            GM_setValue(UPLOADED_KEY, JSON.stringify(ids));
        }
    }

    // ---------------------------- 核心流程 ----------------------------
    /** GM_xmlhttpRequest 发 graphql，显式带 Cookie 头（不依赖扩展自动注入 cookie）。 */
    function gqlViaXhr(query, hasuraLux) {
        const headers = { 'content-type': 'application/json', 'hasura-client-name': 'hasura-console' };
        if (hasuraLux) headers['Cookie'] = `hasura-lux=${hasuraLux}`;
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method: 'POST',
                url: CONSOLE_GQL_URL,
                headers,
                data: JSON.stringify({ query }),
                onload: (r) => {
                    try { resolve(JSON.parse(r.responseText)); }
                    catch (e) { reject(new Error(`解析 ddn_projects 响应失败: ${r.responseText.slice(0, 200)}`)); }
                },
                onerror: () => reject(new Error('查询 project 网络错误（GM_xmlhttpRequest）')),
                ontimeout: () => reject(new Error('查询 project 超时')),
            });
        });
    }

    async function fetchProjects(hasuraLux) {
        const query = '{ ddn_projects { id name } }';
        // 1) 普通 fetch（带 prompt.ql.app 同站 cookie），多数情况可用
        try {
            const resp = await fetch(CONSOLE_GQL_URL, {
                method: 'POST',
                headers: { 'content-type': 'application/json', 'hasura-client-name': 'hasura-console' },
                credentials: 'include',
                body: JSON.stringify({ query }),
            });
            if (resp.ok) {
                const data = await resp.json();
                const ps = (data && data.data && data.data.ddn_projects) || [];
                if (ps.length) return ps;
            }
        } catch (e) { /* 降级到 GM_xmlhttpRequest */ }
        // 2) GM_xmlhttpRequest 兜底（显式 Cookie 头，确保 ddn_projects 查询带认证）
        const data = await gqlViaXhr(query, hasuraLux);
        const ps = (data && data.data && data.data.ddn_projects) || [];
        if (!ps.length) {
            throw new Error('该账号暂无 ddn_projects，请先完成 onboarding 创建首个 project');
        }
        return ps;
    }

    /**
     * 通用跨源请求封装：GM_xmlhttpRequest 绕过页面 CORS 预检与 Mixed Content 限制。
     * 返回类 fetch 的 { ok, status, text }，便于平滑替换 fetch。
     */
    function gmFetch(url, { method = 'GET', headers = {}, body } = {}) {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method,
                url,
                headers,
                data: body,
                timeout: 30000,
                onload: (r) => resolve({
                    ok: r.status >= 200 && r.status < 300,
                    status: r.status,
                    text: () => Promise.resolve(r.responseText),
                }),
                onerror: () => reject(new Error(`网络错误：无法连接 ${url}（网关未启动 / 不可达？）`)),
                ontimeout: () => reject(new Error(`请求超时：${url}`)),
            });
        });
    }

    async function uploadAccount(payload) {
        const url = `${CONFIG.ADMIN_URL}/admin/accounts?auth_key=${encodeURIComponent(CONFIG.ADMIN_AUTH_KEY)}`;
        const resp = await gmFetch(url, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const text = await resp.text();
        if (!resp.ok) {
            throw new Error(`上传失败: ${resp.status} ${text}`);
        }
        return JSON.parse(text);
    }

    async function doUpload() {
        if (!isMainHost()) return;
        if (!CONFIG.ADMIN_URL || !CONFIG.ADMIN_AUTH_KEY) {
            toast('请先设置 ADMIN_URL 与 ADMIN_AUTH_KEY（油猴菜单）', 'error');
            return;
        }

        const { value: autoLux, source } = await getCookie('hasura-lux');
        let hasuraLux = autoLux;
        if (!hasuraLux) {
            // 自动失败 → 优先用已缓存的手动值，避免重复弹窗；无缓存才引导 DevTools 粘贴
            const cached = GM_getValue(MANUAL_LUX_KEY, '');
            if (cached) {
                hasuraLux = cached;
                console.log('[PromptQL账号上交器] 使用缓存的手动 hasura-lux');
            } else {
                toast('自动读取失败，已弹出 DevTools 手动粘贴指引', 'error');
                hasuraLux = promptManualLux();
                if (!hasuraLux) {
                    toast('未提供 hasura-lux，已取消上交', 'error');
                    return;
                }
            }
        } else {
            console.log('[PromptQL账号上交器] hasura-lux 读取来源:', source);
        }

        let projects;
        try {
            projects = await fetchProjects(hasuraLux);
        } catch (e) {
            toast(e.message, 'error');
            return;
        }

        const project = projects[0];
        const projectId = project.id;

        const uploadedIds = getUploadedIds();
        if (uploadedIds.includes(projectId)) {
            toast('该账号已上交过，跳过（可通过重置 uploaded_project_ids 重新上传）', 'info');
            return;
        }

        // 尝试从页面提取邮箱（登录后通常显示在右上角或设置页）
        const emailEl = document.querySelector('input[type="email"]');
        const sourceEmail = emailEl ? emailEl.value.trim() : '';

        const payload = {
            name: genName(sourceEmail, project.name),
            source_email: sourceEmail,
            hasura_lux: hasuraLux,
            project_id: projectId,
            project_name: project.name,
            created_at: isoNow(),
            disabled: false,
        };

        try {
            await uploadAccount(payload);
            markUploaded(projectId);
            toast(`账号 ${payload.name} 上交成功`);
        } catch (e) {
            toast(e.message, 'error');
        }
    }

    // ---------------------------- UI ----------------------------
    function addFloatingButton() {
        if (!isMainHost()) return; // 仅 prompt.ql.app 渲染按钮
        if (document.getElementById('promptql-uploader-btn')) return;
        const btn = document.createElement('button');
        btn.id = 'promptql-uploader-btn';
        btn.textContent = '上交账号';
        btn.title = '将当前 PromptQL 账号上传至 promptql2api';
        btn.style.cssText = `
            position: fixed;
            bottom: 24px;
            right: 24px;
            z-index: 999998;
            padding: 10px 18px;
            border: none;
            border-radius: 24px;
            background: #1a73e8;
            color: #fff;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(0,0,0,0.25);
            font-family: system-ui, sans-serif;
        `;
        btn.addEventListener('click', doUpload);
        document.body.appendChild(btn);
    }

    // ---------------------------- 启动 ----------------------------
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', addFloatingButton);
    } else {
        addFloatingButton();
    }
})();

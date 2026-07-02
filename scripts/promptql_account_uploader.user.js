// ==UserScript==
// @name         PromptQL 账号上交器
// @namespace    https://github.com/promptql2api
// @version      0.1.0
// @description  在 prompt.ql.app 自动提取 hasura-lux cookie 与 project 信息，并上交到 promptql2api 的 /admin 端点。
// @author       Null
// @match        https://prompt.ql.app/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        GM_notification
// @grant        GM_cookie
// @connect      data.pro.ql.app
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
        // 页面内浮动提示
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

    function getCookie(name) {
        // 优先使用 GM_cookie（可读取 httpOnly cookie）
        return new Promise((resolve) => {
            if (typeof GM_cookie === 'function') {
                GM_cookie.list({ name: name }, (cookies, error) => {
                    if (error) {
                        console.warn('GM_cookie.list failed:', error);
                        fallback();
                        return;
                    }
                    const c = (cookies || []).find((x) => x.name === name);
                    if (c && c.value) {
                        resolve(c.value);
                    } else {
                        fallback();
                    }
                });
            } else {
                fallback();
            }

            function fallback() {
                const match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
                resolve(match ? decodeURIComponent(match[1]) : '');
            }
        });
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
    async function fetchProjects(hasuraLux) {
        const resp = await fetch(CONSOLE_GQL_URL, {
            method: 'POST',
            headers: {
                'content-type': 'application/json',
                'hasura-client-name': 'hasura-console',
            },
            credentials: 'include', // 自动携带 hasura-lux cookie
            body: JSON.stringify({ query: '{ ddn_projects { id name } }' }),
        });
        if (!resp.ok) {
            throw new Error(`查询 project 失败: ${resp.status} ${await resp.text()}`);
        }
        const data = await resp.json();
        const projects = (data && data.data && data.data.ddn_projects) || [];
        if (!projects.length) {
            throw new Error('该账号暂无 ddn_projects，请先完成 onboarding 创建首个 project');
        }
        return projects;
    }

    async function uploadAccount(payload) {
        const url = `${CONFIG.ADMIN_URL}/admin/accounts?auth_key=${encodeURIComponent(CONFIG.ADMIN_AUTH_KEY)}`;
        const resp = await fetch(url, {
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
        if (!CONFIG.ADMIN_URL || !CONFIG.ADMIN_AUTH_KEY) {
            toast('请先设置 ADMIN_URL 与 ADMIN_AUTH_KEY（油猴菜单）', 'error');
            return;
        }

        const hasuraLux = await getCookie('hasura-lux');
        if (!hasuraLux) {
            toast('未读取到 hasura-lux cookie，请确认已登录 PromptQL（httpOnly cookie 需 GM_cookie 权限）', 'error');
            return;
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

(() => {
  const CONFIG_CACHE_KEY = '__owuiSsoConfigPromise';
  const TAB_ID = 'owui-sso-settings-tab';
  const IFRAME_ID = 'owui-sso-settings-frame';
  const STYLE_ID = 'owui-custom-loader-style';
  const SSO_PATH = '/admin/settings/sso';
  const SSO_UI_URL = '/api/v1/auths/admin/config/sso/ui';

  const WORKBENCH_UI_URL = '/api/v1/auths/workbench/ui';
  const WORKBENCH_OPEN_KEY = 'owui-workbench-open';
  const WORKBENCH_TRIGGER_SLOT_COMPACT = 'owui-workbench-slot-compact';
  const WORKBENCH_TRIGGER_SLOT_EXPANDED = 'owui-workbench-slot-expanded';
  const WORKBENCH_OVERLAY_ID = 'owui-workbench-overlay';
  const WORKBENCH_FRAME_ID = 'owui-workbench-frame';

  const getPathname = () => window.location.pathname.replace(/\/+$/, '') || '/';
  const getSearch = () => window.location.search || '';
  const hasAuthError = () => new URLSearchParams(getSearch()).has('error');
  const isAuthLanding = () => ['/', '/auth'].includes(getPathname());
  const isAdminSettings = () => getPathname().startsWith('/admin/settings');
  const isSsoSettings = () => getPathname() === SSO_PATH;

  const getSessionStorage = () => {
    try {
      return window.sessionStorage;
    } catch (error) {
      return null;
    }
  };

  const isWorkbenchOpen = () => getSessionStorage()?.getItem(WORKBENCH_OPEN_KEY) === 'true';

  const buildWorkbenchIconMarkup = () =>
    [
      '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.8" stroke="currentColor" class="size-4.5">',
      '<path stroke-linecap="round" stroke-linejoin="round" d="M3.75 6.75h16.5M3.75 12h10.5m-10.5 5.25h16.5M16.5 9.75l3 2.25-3 2.25" />',
      '</svg>',
    ].join('');

  const injectStyles = () => {
    if (document.getElementById(STYLE_ID)) {
      return;
    }

    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #${TAB_ID}[data-owui-sso-active="true"] {
        color: rgb(17 24 39) !important;
      }
      html.dark #${TAB_ID}[data-owui-sso-active="true"] {
        color: rgb(255 255 255) !important;
      }
      .owui-sso-host {
        overflow: visible !important;
        padding-right: 16px;
      }
      .owui-sso-frame-wrap {
        width: 100%;
        border-radius: 24px;
        overflow: hidden;
        background: transparent;
      }
      .owui-sso-frame {
        width: 100%;
        min-height: 960px;
        border: 0;
        display: block;
        overflow: hidden;
        background: transparent;
      }
      [data-owui-workbench-trigger="true"][data-owui-workbench-active="true"] {
        background: rgba(15, 118, 110, 0.12) !important;
        color: rgb(15 118 110) !important;
      }
      html.dark [data-owui-workbench-trigger="true"][data-owui-workbench-active="true"] {
        background: rgba(45, 212, 191, 0.15) !important;
        color: rgb(153 246 228) !important;
      }
      #${WORKBENCH_OVERLAY_ID} {
        position: fixed;
        top: 0;
        right: 0;
        bottom: 0;
        z-index: 60;
        padding: 16px 16px 16px 12px;
        pointer-events: none;
        opacity: 0;
        transition: opacity 160ms ease;
      }
      #${WORKBENCH_OVERLAY_ID}[data-owui-workbench-open="true"] {
        pointer-events: auto;
        opacity: 1;
      }
      .owui-workbench-shell {
        width: 100%;
        height: 100%;
        border-radius: 28px;
        overflow: hidden;
        background: rgba(248, 250, 252, 0.96);
        border: 1px solid rgba(148, 163, 184, 0.22);
        box-shadow: 0 24px 70px rgba(15, 23, 42, 0.18);
        display: grid;
        grid-template-rows: auto minmax(0, 1fr);
        backdrop-filter: blur(16px);
      }
      html.dark .owui-workbench-shell {
        background: rgba(2, 6, 23, 0.92);
        border-color: rgba(71, 85, 105, 0.45);
      }
      .owui-workbench-shell-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 14px 18px;
        border-bottom: 1px solid rgba(148, 163, 184, 0.18);
        background: rgba(255, 255, 255, 0.7);
      }
      html.dark .owui-workbench-shell-header {
        background: rgba(15, 23, 42, 0.72);
        border-bottom-color: rgba(71, 85, 105, 0.36);
      }
      .owui-workbench-shell-title {
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        font-size: 13px;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: rgb(15 118 110);
      }
      .owui-workbench-close {
        border: 0;
        border-radius: 999px;
        min-height: 36px;
        padding: 0 14px;
        cursor: pointer;
        font: inherit;
        font-weight: 700;
        color: rgb(15 23 42);
        background: rgba(226, 232, 240, 0.78);
      }
      html.dark .owui-workbench-close {
        color: rgb(241 245 249);
        background: rgba(30, 41, 59, 0.92);
      }
      .owui-workbench-frame {
        width: 100%;
        height: 100%;
        border: 0;
        display: block;
        background: transparent;
      }
      @media (max-width: 768px) {
        #${WORKBENCH_OVERLAY_ID} {
          left: 0 !important;
          padding: 8px;
        }
        .owui-workbench-shell {
          border-radius: 20px;
        }
      }
    `;
    document.head.appendChild(style);
  };

  const fetchJson = async (url, options = {}) => {
    const response = await fetch(url, {
      credentials: 'include',
      headers: {
        Accept: 'application/json',
        ...(options.headers || {}),
      },
      ...options,
    });

    if (!response.ok) {
      let detail = response.statusText;
      try {
        const payload = await response.json();
        detail = payload.detail || detail;
      } catch (error) {
        // Ignore JSON parsing failures here; the status text is enough.
      }
      throw new Error(detail || `HTTP ${response.status}`);
    }

    return response.json();
  };

  const getAppConfig = () => {
    if (!window[CONFIG_CACHE_KEY]) {
      window[CONFIG_CACHE_KEY] = fetchJson('/api/config').catch((error) => {
        window[CONFIG_CACHE_KEY] = null;
        throw error;
      });
    }
    return window[CONFIG_CACHE_KEY];
  };

  const restoreSessionFromCookie = async () => {
    if (window.localStorage.getItem('token')) {
      return false;
    }

    try {
      const session = await fetchJson('/api/v1/auths/');
      if (!session || !session.token) {
        return false;
      }

      window.localStorage.setItem('token', session.token);
      window.localStorage.setItem('user', JSON.stringify(session));
      window.location.replace(window.location.href);
      return true;
    } catch (error) {
      return false;
    }
  };

  const maybeRedirectToSoloProvider = async () => {
    if (!isAuthLanding() || hasAuthError()) {
      return;
    }

    if (window.localStorage.getItem('token')) {
      return;
    }

    if (await restoreSessionFromCookie()) {
      return;
    }

    try {
      const config = await getAppConfig();
      const providers = Object.keys(((config || {}).oauth || {}).providers || {});
      if (providers.length !== 1) {
        return;
      }

      const provider = providers[0];
      window.location.replace(`/oauth/${encodeURIComponent(provider)}/login`);
    } catch (error) {
      // If config discovery fails, leave the stock login page alone.
    }
  };

  const setTabState = () => {
    const ssoTab = document.getElementById(TAB_ID);
    if (!ssoTab) {
      return;
    }

    ssoTab.dataset.owuiSsoActive = isSsoSettings() ? 'true' : 'false';
    ssoTab.className = ssoTab.className.replace(/\s+owui-sso-active\b/g, '');
    if (isSsoSettings()) {
      ssoTab.className += ' owui-sso-active';
    }
  };

  const ensureSsoTab = () => {
    if (!isAdminSettings()) {
      return;
    }

    const container = document.getElementById('admin-settings-tabs-container');
    if (!container) {
      return;
    }

    let tab = document.getElementById(TAB_ID);
    if (!tab) {
      const templateTab =
        container.querySelector('a[href="/admin/settings/general"]') ||
        container.querySelector('a[href="/admin/settings"]') ||
        container.querySelector('a');

      tab = document.createElement('a');
      tab.id = TAB_ID;
      tab.href = SSO_PATH;
      tab.draggable = false;
      tab.textContent = 'SSO';
      tab.className = templateTab
        ? templateTab.className
        : 'px-0.5 py-1 min-w-fit rounded-lg flex-1 lg:flex-none flex text-right transition select-none';
      container.appendChild(tab);
    }

    setTabState();
  };

  const ensureSsoPanel = () => {
    if (!isSsoSettings()) {
      return;
    }

    const tabsContainer = document.getElementById('admin-settings-tabs-container');
    const contentPane = tabsContainer ? tabsContainer.nextElementSibling : null;
    if (!contentPane) {
      return;
    }

    contentPane.classList.add('owui-sso-host');

    let frame = document.getElementById(IFRAME_ID);
    if (!frame) {
      contentPane.innerHTML = '';

      const wrap = document.createElement('div');
      wrap.className = 'owui-sso-frame-wrap';

      frame = document.createElement('iframe');
      frame.id = IFRAME_ID;
      frame.className = 'owui-sso-frame';
      frame.src = SSO_UI_URL;
      frame.setAttribute('title', 'OWUI SSO Settings');

      wrap.appendChild(frame);
      contentPane.appendChild(wrap);
    }
  };

  const resizeSsoFrame = (height) => {
    const frame = document.getElementById(IFRAME_ID);
    if (!frame || !Number.isFinite(height) || height <= 0) {
      return;
    }
    frame.style.height = `${Math.ceil(height)}px`;
  };

  const getVisibleSidebars = () =>
    [...document.querySelectorAll('[id="sidebar"]')].filter((element) => {
      if (!(element instanceof HTMLElement)) {
        return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && window.getComputedStyle(element).display !== 'none';
    });

  const getSidebarRightOffset = () => {
    const sidebars = getVisibleSidebars();
    if (!sidebars.length) {
      return 0;
    }
    return Math.max(
      ...sidebars.map((element) => Math.max(0, Math.ceil(element.getBoundingClientRect().right))),
    );
  };

  const setWorkbenchTriggerState = () => {
    document.querySelectorAll('[data-owui-workbench-trigger="true"]').forEach((trigger) => {
      trigger.dataset.owuiWorkbenchActive = isWorkbenchOpen() ? 'true' : 'false';
    });
  };

  const setWorkbenchOpen = (open) => {
    const storage = getSessionStorage();
    if (storage) {
      storage.setItem(WORKBENCH_OPEN_KEY, open ? 'true' : 'false');
    }
    refreshUi();
  };

  const buildWorkbenchTriggerMarkup = (expanded) => {
    if (expanded) {
      return `
        <div class=" self-center flex items-center justify-center size-9">
          ${buildWorkbenchIconMarkup()}
        </div>
        <div class=" self-center text-sm font-primary">Workbench</div>
      `;
    }

    return `
      <div class=" self-center flex items-center justify-center size-9">
        ${buildWorkbenchIconMarkup()}
      </div>
    `;
  };

  const createWorkbenchTrigger = (templateLink, expanded) => {
    const trigger = document.createElement('a');
    trigger.href = '#';
    trigger.draggable = false;
    trigger.title = 'Workbench';
    trigger.setAttribute('aria-label', 'Workbench');
    trigger.dataset.owuiWorkbenchTrigger = 'true';
    trigger.className = templateLink.className;
    trigger.innerHTML = buildWorkbenchTriggerMarkup(expanded);
    trigger.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      setWorkbenchOpen(!isWorkbenchOpen());
    });
    return trigger;
  };

  const ensureWorkbenchButtons = () => {
    const workspaceLinks = [...document.querySelectorAll('a[href="/workspace"]')].filter((link) =>
      link.closest('[id="sidebar"]'),
    );

    workspaceLinks.forEach((workspaceLink) => {
      const linkWrapper = workspaceLink.parentElement;
      const parent = linkWrapper ? linkWrapper.parentElement : null;
      if (!linkWrapper || !parent) {
        return;
      }

      const expanded = workspaceLink.textContent.trim().length > 0;
      const slotId = expanded ? WORKBENCH_TRIGGER_SLOT_EXPANDED : WORKBENCH_TRIGGER_SLOT_COMPACT;
      let slot = parent.querySelector(`[data-owui-workbench-slot="${slotId}"]`);
      if (!slot) {
        slot = document.createElement('div');
        slot.dataset.owuiWorkbenchSlot = slotId;
        slot.appendChild(createWorkbenchTrigger(workspaceLink, expanded));
        parent.insertBefore(slot, linkWrapper.nextSibling);
      }
    });

    setWorkbenchTriggerState();
  };

  const ensureWorkbenchOverlay = () => {
    let overlay = document.getElementById(WORKBENCH_OVERLAY_ID);
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = WORKBENCH_OVERLAY_ID;

      const shell = document.createElement('div');
      shell.className = 'owui-workbench-shell';

      const header = document.createElement('div');
      header.className = 'owui-workbench-shell-header';

      const title = document.createElement('div');
      title.className = 'owui-workbench-shell-title';
      title.textContent = 'TWC Workbench';

      const closeButton = document.createElement('button');
      closeButton.className = 'owui-workbench-close';
      closeButton.type = 'button';
      closeButton.textContent = 'Close';
      closeButton.addEventListener('click', () => {
        setWorkbenchOpen(false);
      });

      header.appendChild(title);
      header.appendChild(closeButton);

      const frame = document.createElement('iframe');
      frame.id = WORKBENCH_FRAME_ID;
      frame.className = 'owui-workbench-frame';
      frame.src = WORKBENCH_UI_URL;
      frame.setAttribute('title', 'OWUI Workbench Bridge');

      shell.appendChild(header);
      shell.appendChild(frame);
      overlay.appendChild(shell);
      document.body.appendChild(overlay);
    }

    const shouldOpen = isWorkbenchOpen() && getVisibleSidebars().length > 0;
    overlay.dataset.owuiWorkbenchOpen = shouldOpen ? 'true' : 'false';
    overlay.style.left = `${getSidebarRightOffset()}px`;
  };

  const refreshAdminUi = () => {
    ensureSsoTab();
    ensureSsoPanel();
  };

  const refreshWorkbenchUi = () => {
    ensureWorkbenchButtons();
    ensureWorkbenchOverlay();
  };

  const refreshUi = () => {
    refreshAdminUi();
    refreshWorkbenchUi();
  };

  const init = async () => {
    injectStyles();
    await restoreSessionFromCookie();
    await maybeRedirectToSoloProvider();
    refreshUi();
    window.addEventListener('message', (event) => {
      const data = event && event.data ? event.data : null;
      if (!data || data.type !== 'owui-sso-height') {
        return;
      }
      resizeSsoFrame(Number(data.height));
    });
    window.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && isWorkbenchOpen()) {
        setWorkbenchOpen(false);
      }
    });
    window.setInterval(refreshUi, 750);
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    void init();
  }
})();

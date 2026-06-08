(() => {
  const CONFIG_CACHE_KEY = '__owuiSsoConfigPromise';
  const TAB_ID = 'owui-sso-settings-tab';
  const IFRAME_ID = 'owui-sso-settings-frame';
  const STYLE_ID = 'owui-sso-loader-style';
  const SSO_PATH = '/admin/settings/sso';
  const SSO_UI_URL = '/api/v1/auths/admin/config/sso/ui';

  const getPathname = () => window.location.pathname.replace(/\/+$/, '') || '/';
  const getSearch = () => window.location.search || '';
  const hasAuthError = () => new URLSearchParams(getSearch()).has('error');
  const isAuthLanding = () => ['/','/auth'].includes(getPathname());
  const isAdminSettings = () => getPathname().startsWith('/admin/settings');
  const isSsoSettings = () => getPathname() === SSO_PATH;

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

  const refreshAdminUi = () => {
    ensureSsoTab();
    ensureSsoPanel();
  };

  const init = async () => {
    injectStyles();
    await restoreSessionFromCookie();
    await maybeRedirectToSoloProvider();
    refreshAdminUi();
    window.addEventListener('message', (event) => {
      const data = event && event.data ? event.data : null;
      if (!data || data.type !== 'owui-sso-height') {
        return;
      }
      resizeSsoFrame(Number(data.height));
    });
    window.setInterval(refreshAdminUi, 750);
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    void init();
  }
})();

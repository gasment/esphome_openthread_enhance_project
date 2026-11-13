// Reuse HA's original Add button as the sole BR access-management entry.
// Never patch HA classes, global translations, or other integration pages.
const KEY = "__threadProxyIntegrationEntry";
window[KEY]?.stop();

const TARGET = "/config/integrations/integration/thread_proxy";
const PANEL = "/thread-proxy";
const MARKER = "data-thread-proxy-binding";
const observers = new Map();
let pending = false;
let stopped = false;
let current = null;

function onTargetPage() {
  return window.location.pathname.replace(/\/$/, "") === TARGET;
}

function cleanupButton() {
  if (!current) return;
  current.button.removeEventListener("click", openManagement, true);
  current.button.removeAttribute(MARKER);
  if (current.labelNode?.isConnected && current.labelNode.data === current.lastLabel) {
    current.labelNode.data = current.originalLabel;
  }
  current = null;
}

function openManagement(event) {
  if (!current || !onTargetPage() || current.page.domain !== "thread_proxy" || !current.page.hass?.user?.is_admin) return;
  // Capture before HA's original bubble handler opens a config flow. This
  // also covers keyboard activation of the existing accessible HA button.
  event.preventDefault();
  event.stopImmediatePropagation();
  const from = location.pathname + location.search + location.hash;
  history.pushState({from}, "", PANEL);
  window.dispatchEvent(new CustomEvent("location-changed", {detail: {replace: false}}));
}

function observe(roots) {
  const wanted = new Set(roots);
  for (const [root, observer] of observers) {
    if (!wanted.has(root)) { observer.disconnect(); observers.delete(root); }
  }
  for (const root of wanted) {
    if (observers.has(root)) continue;
    const observer = new MutationObserver(schedule);
    observer.observe(root, {childList: true, subtree: true, characterData: true});
    observers.set(root, observer);
  }
}

function findPage(root, chain, searched) {
  searched.push(root);
  const page = root.querySelector("ha-config-integration-page");
  if (page?.shadowRoot && page.domain === "thread_proxy") {
    return {page, roots: [...chain, root, page.shadowRoot]};
  }
  // querySelector does not cross HA's open shadow roots.
  for (const element of root.querySelectorAll("*")) {
    if (!element.shadowRoot) continue;
    const found = findPage(element.shadowRoot, [...chain, root], searched);
    if (found) return found;
  }
  return null;
}

function reconcile() {
  pending = false;
  if (stopped) return;
  if (!onTargetPage()) { observe([]); cleanupButton(); return; }
  const searched = [];
  const found = findPage(document, [], searched);
  observe(found ? found.roots : searched);
  if (!found) { cleanupButton(); return; }
  const {page} = found;
  if (!page.hass?.user?.is_admin) { cleanupButton(); return; }
  const actions = page.shadowRoot.querySelector(".header .actions");
  if (!actions) { cleanupButton(); return; }
  // No additional button: keep the original HA component and styling.
  const add = [...actions.querySelectorAll("ha-button")].find(button =>
    !button.hasAttribute("href") && !button.href && !button.flowType
  );
  if (!add) { cleanupButton(); return; }
  if (current && (current.page !== page || current.button !== add)) cleanupButton();
  if (!current) {
    current = {page, button: add};
    add.setAttribute(MARKER, "");
    add.addEventListener("click", openManagement, true);
  }
  const label = (page.hass.locale?.language || page.hass.language || "").startsWith("zh") ? "BR 接入管理" : "Manage BR access";
  // Preserve Lit's comment markers and text-node identity inside the button.
  const node = [...add.childNodes].find(item => item.nodeType === Node.TEXT_NODE && item.data.trim());
  if (node) {
    if (current.labelNode !== node || node.data !== current.lastLabel) current.originalLabel = node.data;
    current.labelNode = node;
    current.lastLabel = label;
    if (node.data !== label) node.data = label;
  }
}

function schedule() {
  if (pending || stopped) return;
  pending = true;
  queueMicrotask(reconcile);
}

function stop() {
  stopped = true;
  window.removeEventListener("location-changed", schedule);
  window.removeEventListener("popstate", schedule);
  observe([]);
  cleanupButton();
}

window[KEY] = {stop};
window.addEventListener("location-changed", schedule);
window.addEventListener("popstate", schedule);
customElements.whenDefined("ha-config-integration-page").then(schedule);
schedule();

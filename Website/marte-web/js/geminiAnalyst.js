// js/geminiAnalyst.js

let chatIsOpen = false;
let activeAnalog = null;
let activeMarsLoc = null;
let activeLang = 'en';
let isSending = false;
const chatHistories = new Map();

function ensureChatUI() {
  if (!document.getElementById('analog-chat-fab')) {
    const fab = document.createElement('button');
    fab.id = 'analog-chat-fab';
    fab.type = 'button';
    fab.className = 'analog-chat-fab hidden';
    fab.title = 'Explain why this site is a terrestrial analog (Geology Chat)';
    fab.innerHTML = `<img src="./assets/logo.png" alt="Analogia Geological Chat" />`;
    fab.addEventListener('click', toggleChatDrawer);
    document.body.appendChild(fab);
  }

  if (!document.getElementById('analog-chat-drawer')) {
    const drawer = document.createElement('aside');
    drawer.id = 'analog-chat-drawer';
    drawer.className = 'analog-chat-drawer hidden';
    drawer.innerHTML = `
      <div class="analog-chat-header">
        <div class="analog-chat-title-wrap">
          <img src="./assets/logo.png" alt="Logo" />
          <div>
            <div class="analog-chat-title" id="chat-header-title">ANALOG GEOLOGIST</div>
            <div class="analog-chat-subtitle" id="chat-header-coords">0.00°, 0.00°</div>
          </div>
        </div>
        <button type="button" id="btn-close-analog-chat" class="hud-close-btn" title="Close">✕</button>
      </div>
      <div class="analog-chat-messages" id="analog-chat-messages"></div>
      <form class="analog-chat-input-bar" id="analog-chat-form">
        <input type="text" id="analog-chat-input" placeholder="Ask about this analog site..." autocomplete="off" />
        <button type="submit" class="analog-chat-send-btn" id="analog-chat-send">SEND</button>
      </form>
    `;
    document.body.appendChild(drawer);

    document.getElementById('btn-close-analog-chat')?.addEventListener('click', () => {
      chatIsOpen = false;
      drawer.classList.add('hidden');
    });

    document.getElementById('analog-chat-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = document.getElementById('analog-chat-input');
      const text = input?.value.trim();
      if (!text || isSending || !activeAnalog) return;
      input.value = '';
      await sendChatMessage(text, false);
    });
  }

  const hudCard = document.getElementById('hud-current-location');
  if (hudCard && !hudCard.classList.contains('hidden')) {
    const rect = hudCard.getBoundingClientRect();
    if (rect.right > 0) {
      const leftPx = `${Math.round(rect.right + 14)}px`;
      const topFabPx = `${Math.round(rect.top + 8)}px`;
      const topDrawerPx = `${Math.round(rect.top + 66)}px`;

      const fab = document.getElementById('analog-chat-fab');
      const drawer = document.getElementById('analog-chat-drawer');
      if (fab) {
        fab.style.left = leftPx;
        fab.style.top = topFabPx;
      }
      if (drawer) {
        drawer.style.left = leftPx;
        drawer.style.top = topDrawerPx;
      }
    }
  }
}

export function syncChatTarget(analog, marsLocation, lang = 'es') {
  ensureChatUI();
  activeLang = lang;
  const fab = document.getElementById('analog-chat-fab');
  const drawer = document.getElementById('analog-chat-drawer');

  if (!analog) {
    activeAnalog = null;
    activeMarsLoc = null;
    chatIsOpen = false;
    fab?.classList.add('hidden');
    drawer?.classList.add('hidden');
    return;
  }

  activeAnalog = analog;
  activeMarsLoc = marsLocation || null;
  fab?.classList.remove('hidden');

  const coordsEl = document.getElementById('chat-header-coords');
  if (coordsEl) {
    coordsEl.textContent = `${Number(analog.latitude).toFixed(4)}°, ${Number(analog.longitude).toFixed(4)}° · #${analog.global_rank || 'N/A'}`;
  }

  renderChatMessages();
  if (chatIsOpen) {
    const key = String(activeAnalog.id);
    if (!chatHistories.has(key)) {
      sendChatMessage(
        activeLang === 'en'
          ? 'Explain why this terrestrial site is a valid analog for the selected Martian location based on its metrics.'
          : 'Explica por qué este sitio terrestre es un análogo válido para la ubicación marciana seleccionada según sus métricas.',
        true
      );
    }
  }
}

function toggleChatDrawer() {
  if (!activeAnalog) return;
  const drawer = document.getElementById('analog-chat-drawer');
  chatIsOpen = !chatIsOpen;

  if (chatIsOpen) {
    drawer?.classList.remove('hidden');
    renderChatMessages();
    const key = String(activeAnalog.id);
    if (!chatHistories.has(key)) {
      sendChatMessage(
        activeLang === 'en'
          ? 'Explain why this terrestrial site is a valid analog for the selected Martian location based on its metrics.'
          : 'Explica por qué este sitio terrestre es un análogo válido para la ubicación marciana seleccionada según sus métricas.',
        true
      );
    }
  } else {
    drawer?.classList.add('hidden');
  }
}

function renderChatMessages() {
  const container = document.getElementById('analog-chat-messages');
  if (!container || !activeAnalog) return;

  const key = String(activeAnalog.id);
  const messages = chatHistories.get(key) || [];

  container.innerHTML = messages
    .map((m) => `<div class="chat-bubble ${m.role === 'user' ? 'user' : 'bot'}">${m.text.replace(/\n/g, '<br>')}</div>`)
    .join('');

  if (isSending) {
    container.innerHTML += `<div class="chat-bubble bot">⏳ Analysing geomorphometric parameters...</div>`;
  }

  container.scrollTop = container.scrollHeight;
}

async function sendChatMessage(text, isInitialPrompt = false) {
  if (!activeAnalog || isSending) return;
  const key = String(activeAnalog.id);
  const history = chatHistories.get(key) || [];

  if (!isInitialPrompt) {
    history.push({ role: 'user', text });
  }
  chatHistories.set(key, history);

  isSending = true;
  renderChatMessages();

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        analog: activeAnalog,
        marsLocation: activeMarsLoc,
        history: isInitialPrompt ? [] : history.slice(0, -1),
        message: text,
        lang: activeLang
      })
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || `Error ${res.status}`);
    }

    history.push({ role: 'assistant', text: data.reply });
  } catch (err) {
    history.push({ role: 'assistant', text: `⚠️ ${err.message}` });
  } finally {
    isSending = false;
    chatHistories.set(key, history);
    renderChatMessages();
  }
}